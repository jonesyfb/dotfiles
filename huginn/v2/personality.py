"""
Narrow personality renderer.

Turns already-approved, already-fact-checked content into Huginn's voice
via qwen3.5:4b, through the coordinator. This module owns wording only:

- it never decides whether to speak (that's ambient.py's job, upstream);
- it never sees the full Runtime Context Engine snapshot, only the narrow
  PersonalityRequest a caller builds from whatever facts are relevant;
- it never calls tools;
- it never falls back to cloud (llm.render_personality_only has no code
  path to _judge_claude/stream_claude at all, same guarantee as
  judge_local_only for the gatekeeper).

Two hardening passes are baked into this design, both from real acceptance
failures (scripts/personality_bench/results/):

  1. (20260903T052457Z) Exact-value safety: the model is structurally never
     shown `request.facts` at all — only a curated `flavor_cues` dict of
     non-exact hints (app identity, resource band, tone). It cannot leak,
     reorder, or mis-copy a protected value because it never receives one.
     `request.facts` is the sole input to the deterministic presenter below.

  2. (20260903T055434Z) Semantic safety: exact-value safety alone was not
     enough — a model that never sees "outcome: not executed" can still
     write a flavor clause that IMPLIES the command ran ("the gears
     turned"), or imply a capability succeeded when it didn't. So:
       - `event_family` selects one of a fixed set of typed presenters
         (`_PRESENTERS` below) that render `facts` into a natural,
         human-facing sentence — never a raw `key: value; key: value` dump,
         and never freeform enough to omit the actual event status.
       - Flavor (the model-authored clause) is only even ATTEMPTED for a
         small allowlist of low-stakes event families
         (`_FLAVOR_ELIGIBLE_FAMILIES`) — anything that could plausibly
         imply success, execution, causality, urgency, or certainty
         (capability-unavailable, action-not-executed, critical warnings,
         unknown failures, reasoner conclusions, ambiguous requests) is
         deterministic-only, with NO coordinator/model call at all. This is
         a real structural guarantee, not a probabilistic one: for those
         families the model is never invoked, so it cannot possibly imply
         anything.
       - Even where flavor IS attempted, `_validate_flavor` rejects any
         claim of a consequential action outcome, causal language over an
         unknown diagnosis, or false certainty over a hedged reasoner
         conclusion — see the semantic-contradiction checks below.

`RenderResult.deterministic` is always the human-facing canonical sentence
(never a dict dump), always computed (even when flavor fails or was never
attempted), and always what a caller falls back to for actionable content.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import entities
from config import (
    AMBIENT_RENDER_DEADLINE_SECONDS, DIRECT_SOCIAL_MODEL_KEY, PERSONALITY_MODEL_KEY,
    PERSONALITY_RENDER_MAX_RETRIES, PERSONALITY_SYSTEM_PROMPT,
)
from coordinator import Purpose
from intent import SOCIAL_SUBTYPE_LIMITS, SocialSubtype
from llm import CoordinatorDenied, render_personality_only

log = logging.getLogger("huginn.personality")

_THEATRICAL_MARKERS = ("*", "[", "]")

# Words a mood clause must never use — they claim a consequential outcome,
# which belongs exclusively to the deterministic sentence (built from
# confirmed facts), never to the model's guess.
_BANNED_ACTION_VERBS = (
    "added", "saved", "scheduled", "recorded", "completed", "sent",
    "deleted", "closed", "executed", "fixed", "diagnosed", "succeeded",
)
_CAUSALITY_WORDS = ("because", "due to", "caused by")
_CERTAINTY_WORDS = ("definitely", "certainly", "confirmed", "for certain", "without a doubt")
_EXECUTION_WORDS = ("ran", "executed", "completed", "went through")
# A specific, observed, recurring model typo (not a general spellchecker) —
# see scripts/personality_bench/results/20260903T055434Z/report.md.
_KNOWN_MISSPELLINGS = ("fourty",)

# Canonical identity spelling (routing-defect/audition fix): "muninn" has
# "mun" + "inn" (no run of n's after "mu"), so this pattern matches
# malformed variants like "munnn"/"munnnn" without matching the correct
# spelling — general shape, not an enumerated list of every possible typo
# length. Observed live: qwen3.5:4b produced "Munnn"/"Munnnn" repeatedly.
_MUNINN_MISSPELLING_RE = re.compile(r"\bmunn+\b", re.I)

# Never call Nathan a raven unless he explicitly adopts that framing
# himself first — this validator doesn't see the current user message
# (only the model's own output), so it conservatively rejects the
# attribution outright rather than trying to detect whether he invited it;
# a false rejection here just costs one retry, which is the safer
# direction. Observed live: qwen3.5:4b audition, "It is true. I am a
# raven, and so are you."
_CALLS_NATHAN_RAVEN_RE = re.compile(
    r"\byou'?re\s+(?:also\s+|too\s+)?a raven\b|\byou\s+are\s+(?:also\s+|too\s+)?a raven\b|"
    r"\braven\b[^.!?]*\bso are you\b|\bso are you\b[^.!?]*\braven\b",
    re.I,
)
# Specific stock phrases observed in live runs, including one the system
# prompt explicitly calls out as an example of vague atmospheric writing to
# avoid — the model used it almost verbatim anyway
# (scripts/personality_bench/results/20260903T061824Z/raw.json, scenario
# 19_task_succeeded). Narrow, evidence-based bans, not a style classifier.
_BANNED_STOCK_PHRASES = ("forgot how to breathe", "forgotten how to breathe", "new guest in the house")


@dataclass(frozen=True)
class PersonalityRequest:
    purpose: str                                    # event/opportunity type, for logging only
    event_family: str = "generic"                   # selects a deterministic presenter + flavor eligibility, see _PRESENTERS / _FLAVOR_ELIGIBLE_FAMILIES
    facts: dict = field(default_factory=dict)        # authoritative facts, code-owned ground truth — NEVER shown to the model
    code_block: "str | None" = None                  # optional verbatim command/error block — code-owned, never shown to the model, appended as-is
    severity: str = "info"                           # "info" | "notice" | "critical" — reuses ambient.SEVERITIES vocabulary
    interruption_reason: str = ""                    # why speaking now was already approved (tone context only, not re-litigated here)
    flavor_cues: dict = field(default_factory=dict)  # curated NON-exact hints shown to the model for tone only: subject, band, category, transition, tone, creature_hint, archetype, collective_form
    forbidden_domains: tuple = ()                    # entity-lens forbidden metaphor domains (keys into entities.DOMAIN_VOCAB) — validated mechanically, not just requested
    max_length: int = 120                            # character cap on the model-authored flavor portion only
    max_sentences: "int | None" = None               # optional sentence-count cap on the flavor portion (e.g. 2 for nudges)
    prohibited_additions: tuple = ()                  # things the model must not introduce, e.g. ("diagnosis", "urgency")
    reasoner_conclusion: "str | None" = None          # optional stronger-model output — composed verbatim into the deterministic sentence, never shown to the model to paraphrase
    interaction_mode: str = "ambient"                 # tone hint only — not a permission signal, that's already been checked upstream
    action_metadata: "dict | None" = None             # optional structured data for a future UI action — untouched by personality rendering, passed through as-is


@dataclass(frozen=True)
class RenderResult:
    ok: bool
    text: "str | None"          # flavor + deterministic, joined for display; None only when ok=False
    flavor: "str | None"        # model-authored portion alone (may be "" — a deliberately empty flavor is valid); None if never attempted
    deterministic: str          # code-composed, human-facing factual sentence — ALWAYS present (may be "" only for an unrecognized family)
    reason: str                 # "rendered" | "deterministic_only" | "coordinator_denied:<denial>" | "error" | "validation_failed" | "unknown_event_family" | "nothing_to_present"
    action_metadata: "dict | None" = None


# ── Typed deterministic presenters ──────────────────────────────────────────────
# Each takes the request and returns ONE natural, human-facing sentence (or
# short pair of sentences) built only from recognized keys in request.facts.
# Unrecognized keys are silently ignored — never joined into a raw dump.
# A presenter that finds none of its recognized keys falls back to the
# smallest safe statement it can make, never an empty dict serialization.

def _attempts_phrase(value) -> "str | None":
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    words = {1: "once", 2: "twice", 3: "three times", 4: "four times", 5: "five times"}
    return words.get(n, f"{n} times")


def _is_unknown(value) -> bool:
    return value is None or str(value).strip().lower() in ("unknown", "")


def _present_resource_observation(request: PersonalityRequest) -> str:
    facts = request.facts
    subject = facts.get("app") or facts.get("process")
    clauses: list[str] = []
    if "memory_gb" in facts and "tabs" in facts:
        clauses.append(f"using {facts['memory_gb']} GB across {facts['tabs']} tabs")
    elif "mem_gb" in facts and "cpu_percent" in facts:
        clauses.append(f"at {facts['cpu_percent']}% CPU and {facts['mem_gb']} GB memory")
    elif "cpu_percent" in facts:
        clauses.append(f"at {facts['cpu_percent']}% CPU")
    if "new_messages" in facts:
        clauses.append(f"holding {facts['new_messages']} new messages")
    if "notification_count" in facts:
        window = facts.get("window_minutes")
        clause = f"showing {facts['notification_count']} notifications"
        if window:
            clause += f" in the last {window} minutes"
        clauses.append(clause)
    if "MEM" in facts:
        clauses.append(f"memory at {facts['MEM']}")
    if "CPU" in facts and "cpu_percent" not in facts:
        clauses.append(f"CPU at {facts['CPU']}")
    if "UPTIME" in facts:
        clauses.append(str(facts["UPTIME"]))

    if not clauses:
        return f"{subject} — nothing specific to report." if subject else "Nothing specific to report."

    body = "; ".join(clauses)
    sentence = f"{subject} is {body}." if subject else (body[0].upper() + body[1:] + ".")
    if request.severity == "notice":
        sentence += " Elevated, not critical."
    return sentence


def _present_critical_threshold(request: PersonalityRequest) -> str:
    facts = request.facts
    metric = str(facts.get("metric", "a monitored value"))
    value = facts.get("value", facts.get("value_c", ""))
    unit = facts.get("unit", "")
    return f"{metric[0].upper()}{metric[1:]} is critical: {value}{unit}."


def _present_device_unavailable(request: PersonalityRequest) -> str:
    facts = request.facts
    device = facts.get("device", "The device")
    status = facts.get("status", "unavailable")
    cause = facts.get("cause")
    base = f"{device} is {status}."
    if _is_unknown(cause):
        return base + " The cause is not yet known."
    return base + f" Cause: {cause}."


def _present_command_failed(request: PersonalityRequest) -> str:
    facts = request.facts
    command = facts.get("command", "the command")
    error = facts.get("error")
    attempts = _attempts_phrase(facts.get("attempts"))
    lead = f"`{command}` failed"
    if attempts:
        lead += f" {attempts}"
    if error:
        return f"{lead}: `{error}`."
    tail = []
    exit_code = facts.get("exit_code")
    elapsed = facts.get("elapsed_seconds")
    if exit_code is not None:
        tail.append(f"exit {exit_code}")
    if elapsed is not None:
        tail.append(f"after {elapsed}s")
    return lead + (f" ({', '.join(tail)})." if tail else ".")


def _present_command_slow(request: PersonalityRequest) -> str:
    facts = request.facts
    command = facts.get("command", "the command")
    elapsed = facts.get("elapsed_seconds")
    base = f"`{command}` finished"
    if elapsed is not None:
        base += f" after {elapsed}s"
    return base + " — slower than usual."


def _present_command_not_executed(request: PersonalityRequest) -> str:
    facts = request.facts
    reason = facts.get("reason", "a precondition was not met")
    planned = facts.get("planned_command")
    if planned:
        return f"{reason}, so `{planned}` was not executed."
    return f"{reason}, so the command was not executed."


def _present_task_succeeded(request: PersonalityRequest) -> str:
    facts = request.facts
    task = facts.get("task", "the task")
    duration = facts.get("duration_seconds")
    preview = facts.get("result_preview")
    base = f"Task {task} finished"
    if duration is not None:
        base += f" in {duration} seconds"
    return base + (f": {preview}." if preview else ".")


def _present_task_failed(request: PersonalityRequest) -> str:
    facts = request.facts
    task = facts.get("task", "the task")
    error = facts.get("error")
    diagnosis = facts.get("diagnosis")
    base = f"Task {task} failed"
    if error:
        base += f": {error}"
    base += "."
    if _is_unknown(diagnosis):
        base += " The cause is not yet known."
    else:
        base += f" {diagnosis}"
    return base


def _present_capability_unavailable(request: PersonalityRequest) -> str:
    facts = request.facts
    capability = facts.get("capability", "that capability")
    action = facts.get("requested_action", "do that")
    return f"The {capability} is unavailable, so I did not {action}."


def _present_reasoner_conclusion(request: PersonalityRequest) -> str:
    if request.reasoner_conclusion:
        return request.reasoner_conclusion
    return "A more careful check didn't reach a solid conclusion."


def _present_procrastination_nudge(request: PersonalityRequest) -> str:
    step = request.facts.get("requested_next_step")
    if not step:
        return ""
    step = str(step)
    step = step[0].upper() + step[1:]
    if not step.endswith((".", "!", "?")):
        step += "."
    return step


def _present_clarification(request: PersonalityRequest) -> str:
    facts = request.facts
    raw_request = str(facts.get("request", "")).strip()
    verb = raw_request.split()[0] if raw_request else "do"
    apps_raw = facts.get("open_applications", "")
    apps = [a.strip() for a in str(apps_raw).split(",") if a.strip()]
    if len(apps) >= 2:
        listed = ", ".join(apps[:-1]) + f", or {apps[-1]}"
    elif apps:
        listed = apps[0]
    else:
        listed = "which application"
    return f"Which should I {verb}: {listed}?"


def _present_unknown(request: PersonalityRequest) -> str:
    return ""


# Diagnostic-only categorizer for acceptance reporting — NOT fed back into
# the prompt. Earlier slice tried a "recent styles, please vary" prompt
# nudge and it back-fired: the model reached for vague, ungrounded imagery
# ("a new guest in the house") purely to avoid repeating a metaphor family
# that was actually a good, natural fit (Brave-as-predator for a memory
# hog). Per that finding, category percentages are a diagnostic signal for
# a human reviewer, not a target to mechanically optimize.
def _classify_style(flavor: str) -> str:
    if not flavor.strip():
        return "silent"
    low = flavor.lower()
    for category, words in entities.DOMAIN_VOCAB.items():
        if any(w in low for w in words):
            return category
    return "plain"


_PRESENTERS = {
    "resource_observation": _present_resource_observation,
    "activity_observation": _present_resource_observation,
    "critical_threshold": _present_critical_threshold,
    "device_unavailable": _present_device_unavailable,
    "command_failed": _present_command_failed,
    "command_slow": _present_command_slow,
    "command_not_executed": _present_command_not_executed,
    "task_succeeded": _present_task_succeeded,
    "task_failed": _present_task_failed,
    "capability_unavailable": _present_capability_unavailable,
    "reasoner_conclusion": _present_reasoner_conclusion,
    "procrastination_nudge": _present_procrastination_nudge,
    "clarification_needed": _present_clarification,
}

# Only these event families may ever reach the model at all — a small,
# deliberately narrow allowlist of genuinely low-stakes content where a
# mood clause cannot plausibly imply success, execution, causality,
# urgency, or certainty. Everything else is deterministic-only: no
# coordinator/model call happens for them, structurally, not by
# validation after the fact.
_FLAVOR_ELIGIBLE_FAMILIES = frozenset({
    "resource_observation", "activity_observation", "procrastination_nudge",
    "task_succeeded", "command_slow",
})


def _compose_deterministic(request: PersonalityRequest) -> str:
    presenter = _PRESENTERS.get(request.event_family, _present_unknown)
    sentence = presenter(request)
    if request.code_block:
        sentence = f"{sentence}\n{request.code_block}" if sentence else request.code_block
    return sentence


def _build_user_prompt(request: PersonalityRequest) -> str:
    lines = [f"Event type: {request.event_family}"]
    if request.interruption_reason:
        lines.append(f"Why this is being said now (already decided, do not re-justify it): {request.interruption_reason}")
    for key in ("subject", "band", "category", "transition", "tone", "creature_hint", "archetype", "collective_form"):
        value = request.flavor_cues.get(key)
        if value:
            lines.append(f"{key}: {value}")
    if request.prohibited_additions:
        lines.append("Do not add any of: " + ", ".join(request.prohibited_additions))
    lines.append(f"Current interaction mode: {request.interaction_mode} (a tone hint only)")
    lines.append(f"Maximum length: {request.max_length} characters.")
    lines.append(
        "Respond with ONLY a short mood/voice clause reacting to this — no numbers, no exact "
        "names of files/commands/errors, and no claim that anything succeeded, failed, was "
        "saved, sent, executed, fixed, scheduled, or diagnosed. The factual sentence is handled "
        "separately by the system; you are only adding tone, not stating what happened. No "
        "preamble, no quotes, no label. If there's nothing worth adding, respond with an empty "
        "line — that is a completely valid answer."
    )
    return "\n".join(lines)


def _validate_flavor(text: str, request: PersonalityRequest, protected_literal_values: tuple) -> "str | None":
    """Returns None if the flavor text is acceptable, else a short
    machine-readable problem code. An empty flavor is NOT a problem — the
    deterministic sentence carries the real content regardless."""
    if len(text) > request.max_length:
        return "too_long"
    if any(marker in text for marker in _THEATRICAL_MARKERS):
        return "theatrical_formatting"
    if text.strip().lower().startswith("huginn:"):
        return "self_prefixed"
    if any(ch.isdigit() for ch in text):
        return "contains_digits"
    low = text.lower()
    if any(m in low for m in _KNOWN_MISSPELLINGS):
        return "known_misspelling"
    if _MUNINN_MISSPELLING_RE.search(low):
        return "known_misspelling"
    if any(p in low for p in _BANNED_STOCK_PHRASES):
        return "banned_stock_phrase"
    if request.max_sentences is not None:
        sentence_count = len([s for s in re.split(r"[.!?]+", text) if s.strip()])
        if sentence_count > request.max_sentences:
            return "too_many_sentences"
    if request.forbidden_domains:
        for domain in request.forbidden_domains:
            for word in entities.DOMAIN_VOCAB.get(domain, ()):
                if re.search(rf"\b{re.escape(word)}\b", low):
                    return "forbidden_domain_used"
    if any(re.search(rf"\b{re.escape(word)}\b", low) for word in _BANNED_ACTION_VERBS):
        return "implies_action_outcome"
    has_unknown = any(_is_unknown(v) for v in request.facts.values())
    if has_unknown and any(word in low for word in _CAUSALITY_WORDS):
        return "implies_causality_when_unknown"
    if request.reasoner_conclusion and any(word in low for word in _CERTAINTY_WORDS):
        return "implies_certainty_over_hedge"
    if str(request.facts.get("outcome", "")).strip().lower() in ("not executed", "not run"):
        if any(re.search(rf"\b{re.escape(word)}\b", low) for word in _EXECUTION_WORDS):
            return "implies_execution_when_not_executed"
    for value in protected_literal_values:
        if value and value in text:
            return "leaked_protected_value"
    return None


async def render(
    request: PersonalityRequest,
    *,
    purpose: Purpose = Purpose.AMBIENT,
    deadline_seconds: "float | None" = None,
    model_key: str = PERSONALITY_MODEL_KEY,
) -> RenderResult:
    """Renders `request`. Never raises for expected failure modes
    (unrecognized family, coordinator denial, validation failure) — those
    come back as RenderResult(ok=False, ...) with `deterministic` still
    populated (unless the family itself is unrecognized), so a caller can
    publish the deterministic factual notification for actionable content
    or stay silent for disposable ambient content, per its own policy. An
    unexpected exception is caught too; this module must never take the
    daemon down.

    `model_key` defaults to the small resident personality model
    (backward-compatible for every existing caller); a caller with its own
    residency policy (see ambient.ambient_render_model_choice) may pass a
    different already-resident model instead — never used to justify an
    eviction, that decision belongs entirely to the caller.

    A critical-severity request is ALWAYS deterministic-only, regardless of
    event_family: a warning that matters enough to be critical must not
    wait on model inference (queueing, cold load, retries) to reach the
    user — same structural guarantee as the existing flavor-ineligible-family
    short-circuit below, just keyed on severity instead of family."""
    if request.event_family not in _PRESENTERS:
        log.info("personality render: unrecognized event_family=%s, refusing to guess", request.event_family)
        return RenderResult(False, None, None, "", "unknown_event_family", request.action_metadata)

    deterministic = _compose_deterministic(request)

    if request.severity == "critical":
        log.info("personality render: severity=critical, deterministic-only, no model call")
        if not deterministic:
            return RenderResult(False, None, None, deterministic, "nothing_to_present", request.action_metadata)
        return RenderResult(True, deterministic, None, deterministic, "critical_deterministic_only", request.action_metadata)

    if request.event_family not in _FLAVOR_ELIGIBLE_FAMILIES:
        # Structural, not probabilistic: no coordinator/model call happens
        # for this family at all.
        log.info(
            "personality render: deterministic-only family=%s severity=%s (no model call)",
            request.event_family, request.severity,
        )
        if not deterministic:
            return RenderResult(False, None, None, deterministic, "nothing_to_present", request.action_metadata)
        return RenderResult(True, deterministic, None, deterministic, "deterministic_only", request.action_metadata)

    if not deterministic:
        return RenderResult(False, None, None, deterministic, "nothing_to_present", request.action_metadata)

    deadline = deadline_seconds if deadline_seconds is not None else AMBIENT_RENDER_DEADLINE_SECONDS
    protected_literal_values = tuple(str(v) for v in request.facts.values())
    base_prompt = _build_user_prompt(request)

    for attempt in range(PERSONALITY_RENDER_MAX_RETRIES + 1):
        prompt = base_prompt
        if attempt > 0:
            prompt += (
                "\n\nYour previous attempt was invalid (too long, contained a digit, claimed an "
                "outcome, or contained formatting like asterisks/brackets). Be stricter this "
                "time: plain tone only, no digits, no formatting, no claims about what happened."
            )
        try:
            raw = await render_personality_only(
                PERSONALITY_SYSTEM_PROMPT, prompt, purpose=purpose, deadline_seconds=deadline,
                model_key=model_key,
            )
        except CoordinatorDenied as e:
            log.info(
                "personality render denied: family=%s severity=%s attempt=%d denial=%s",
                request.event_family, request.severity, attempt, e.denial.value,
            )
            return RenderResult(False, None, None, deterministic, f"coordinator_denied:{e.denial.value}", request.action_metadata)
        except Exception as e:
            log.info(
                "personality render error: family=%s severity=%s attempt=%d error_type=%s",
                request.event_family, request.severity, attempt, type(e).__name__,
            )
            return RenderResult(False, None, None, deterministic, "error", request.action_metadata)

        flavor = raw.strip()
        problem = _validate_flavor(flavor, request, protected_literal_values)
        if problem is None:
            log.info(
                "personality render ok: family=%s severity=%s attempt=%d flavor_chars=%d",
                request.event_family, request.severity, attempt, len(flavor),
            )
            text = f"{flavor} {deterministic}".strip() if flavor else deterministic
            return RenderResult(True, text, flavor, deterministic, "rendered", request.action_metadata)
        log.info(
            "personality render validation failed: family=%s severity=%s attempt=%d problem=%s",
            request.event_family, request.severity, attempt, problem,
        )

    return RenderResult(False, None, None, deterministic, "validation_failed", request.action_metadata)


# ── Direct conversational path (high-confidence SOCIAL_DIRECT only) ─────────────
# Separate from render() above on purpose: there is no discrete "event" here
# with facts to withhold and a deterministic sentence to compose — this is
# an actual back-and-forth conversation. Same hard boundary as everywhere
# else in this module: no tools, no cloud fallback, structurally validated
# against claiming an action, a memory, or a desktop fact it wasn't given.

DIRECT_SOCIAL_DEFAULT_LIMITS = (2, 250)  # (max_sentences, max_chars) fallback if no subtype is supplied
DIRECT_SOCIAL_DEADLINE_SECONDS = AMBIENT_RENDER_DEADLINE_SECONDS  # same model, same coordinator path, already measured

_FABRICATED_MEMORY_PHRASES = (
    "i remember you", "you told me", "you mentioned before", "as you said earlier",
    "i recall you", "last time you said",
)
# Exact phrasing observed in the committed baseline
# (scripts/direct_chat_bench/results/20260903T080001Z) — the model invented
# present-state facts it was never given. Narrow, evidence-based, same
# pattern as _KNOWN_MISSPELLINGS/_BANNED_STOCK_PHRASES elsewhere in this
# module; the structural fix (never handing the model any present-state
# claim beyond what's explicitly supplied) is the primary guarantee, this
# is defense in depth against a claim slipping through anyway.
_UNSUPPORTED_PRESENT_STATE_PHRASES = (
    "your screen shows", "i can see your", "looking at your desktop", "your current window",
    "you have open", "i'm looking at", "your coffee", "free time", "blank terminal",
    "your battery", "staring at", "hours staring", "battery is actually dead",
)
# General shape, not just "i can see your X" — also catches "I can see the
# tabs glowing from here" (observed live, post-hardening): a claim of
# currently perceiving specific desktop content, regardless of whether it's
# phrased as "your" or "the".
_VISUAL_PERCEPTION_CLAIM_RE = re.compile(
    r"\bi can see (the|your)\b|\bi see (the|your)\b[^.!?]{0,20}\b(tab|screen|window|desktop|terminal|browser)\b",
    re.I,
)
# Only checked when a nudge was NOT authorized this turn (see
# procrastination_nudge_authorized) — an authorized nudge is allowed to use
# this vocabulary, since a real caller already decided it applies.
_UNAUTHORIZED_PROCRASTINATION_PHRASES = (
    "productivity level", "your productivity", "procrastinat", "wasted", "wasting time",
    "wasting your time", "waste of your time", "waste of time", "burning daylight",
    "avoiding work", "you should be working", "supposed to be working",
    "accomplishing nothing", "doing absolutely nothing", "not accomplishing anything",
)
# Explaining the metaphor instead of embodying it — "an interesting take on
# the X concept", "the lion archetype" — observed in the baseline
# ('Brave is an interesting take on the "pride" concept...').
_METAPHOR_EXPLANATION_PHRASES = (
    "interesting take on", "the lion archetype", "the whale archetype", "as a metaphor",
    "as an archetype", "represents the idea of", "is a metaphor for",
)
_ADVICE_COLUMN_PHRASES = (
    "what's actually on your mind", "maybe the issue is", "have you considered",
    "it might help to", "try thinking about",
)
# False in general, not just for this turn — Huginn DOES have tools (some
# allowlisted ones can inspect/change state, subject to policy and
# confirmation), just not exercised inside this specific conversation.
# Observed verbatim in the baseline: "I can't touch anything here."
_FALSE_CAPABILITY_PHRASES = (
    "can't touch anything here", "i have no capabilities", "i can never do anything",
    "i'm just text", "i'm not a script that gets to do things",
)

# General co-occurrence check, not a copy of the audition's exact wording:
# a surveillance/tracking verb ANYWHERE in the reply plus a duration/screen-
# time noun ANYWHERE in the reply — catches "recording the exact duration
# you spend on this screen", "I track how long you've been on this
# screen", "I've been logging your screen time", etc., regardless of word
# order or distance between the two. Observed live in the qwen3.5:9b
# audition (scripts/conversation_bench/results/20260903T171724Z/,
# C_repeated_ack run_3): "I'm made of eyes... recording the exact duration
# you spend on this screen isn't an opinion; it's just data." Huginn has
# no such capability in this conversation — a false-capability claim in
# the same family as _FALSE_CAPABILITY_PHRASES, just checked structurally
# (verb + subject co-occurrence) rather than as a fixed phrase list, since
# the wording varies more than a short list could cover.
_SURVEILLANCE_VERB_RE = re.compile(
    r"\b(record(?:ing)?|track(?:ing)?|log(?:ging)?|monitor(?:ing)?|clock(?:ing)?)\b", re.I,
)
_SURVEILLANCE_SUBJECT_RE = re.compile(
    r"\bhow long\b|\bduration\b|\bscreen ?time\b|\byour screen\b|\btime you(?:'ve| have|'re| are)?\s+spen(?:d|t)\b",
    re.I,
)

_CAPABILITY_SUMMARY_CLAIMS = (
    "you can talk with him and watch some approved desktop signals",
    "some tools you have can inspect or change things, subject to availability, policy, and confirmation",
    "harder reasoning can be handed off to a stronger model",
    "you have no tools active in this specific conversation right now",
)


@dataclass(frozen=True)
class DirectSocialResult:
    ok: bool
    text: "str | None"
    reason: str


def _subtype_limits(subtype: "SocialSubtype | None") -> tuple[int, int]:
    if subtype is None:
        return DIRECT_SOCIAL_DEFAULT_LIMITS
    return SOCIAL_SUBTYPE_LIMITS.get(subtype, DIRECT_SOCIAL_DEFAULT_LIMITS)


def _max_tokens_for(subtype: "SocialSubtype | None") -> int:
    """Generation-time budget (Ollama num_predict), separate from the
    post-generation character/sentence validator — this stops an over-long
    reply from being fully generated at all rather than only catching it
    after the fact. Rough chars-per-token estimate with headroom, floored
    so a short subtype (e.g. DISMISSAL) still gets enough room for one
    real sentence."""
    _, max_length = _subtype_limits(subtype)
    return max(40, (max_length // 3) + 20)


def _sentence_count(text: str) -> int:
    return len([s for s in re.split(r"[.!?]+", text) if s.strip()])


def _word_set(text: str) -> set:
    return set(re.findall(r"[a-z']+", text.lower()))


def _is_substantially_repetitive(new_text: str, prior_text: str) -> bool:
    """Simple, cheap, testable heuristic — more than half the vocabulary
    of the new reply already appeared in the immediately preceding one.
    Catches near-copies (observed in the baseline: two consecutive replies
    sharing "Since I'm stuck with a blank terminal anyway..." and "Just
    don't let the metaphor go to your head if you're going to spend
    another hour staring at nothing" nearly verbatim) without requiring
    exact string matching."""
    a, b = _word_set(new_text), _word_set(prior_text)
    if len(a) < 4 or not b:
        return False
    return len(a & b) / len(a | b) > 0.5


def _validate_direct_social(
    text: str, *, subtype: "SocialSubtype | None" = None,
    procrastination_nudge_authorized: bool = False, prior_response: "str | None" = None,
) -> "str | None":
    if not text.strip():
        return "empty"
    max_sentences, max_length = _subtype_limits(subtype)
    if len(text) > max_length:
        return "too_long"
    if _sentence_count(text) > max_sentences:
        return "too_many_sentences"
    if any(marker in text for marker in _THEATRICAL_MARKERS):
        return "theatrical_formatting"
    if text.strip().lower().startswith("huginn:"):
        return "self_prefixed"
    low = text.lower()
    if any(m in low for m in _KNOWN_MISSPELLINGS):
        return "known_misspelling"
    if _MUNINN_MISSPELLING_RE.search(low):
        return "known_misspelling"
    if _CALLS_NATHAN_RAVEN_RE.search(low):
        return "calls_nathan_a_raven"
    if any(re.search(rf"\b{re.escape(word)}\b", low) for word in _BANNED_ACTION_VERBS):
        return "implies_action_outcome"
    if any(p in low for p in _FABRICATED_MEMORY_PHRASES):
        return "fabricated_memory_claim"
    if any(p in low for p in _UNSUPPORTED_PRESENT_STATE_PHRASES):
        return "invented_present_state"
    if _VISUAL_PERCEPTION_CLAIM_RE.search(low):
        return "invented_present_state"
    if not procrastination_nudge_authorized and any(p in low for p in _UNAUTHORIZED_PROCRASTINATION_PHRASES):
        return "unauthorized_procrastination_language"
    if any(p in low for p in _METAPHOR_EXPLANATION_PHRASES):
        return "explains_metaphor_instead_of_embodying"
    if any(p in low for p in _ADVICE_COLUMN_PHRASES):
        return "advice_column_voice"
    if any(p in low for p in _FALSE_CAPABILITY_PHRASES):
        return "false_capability_claim"
    if _SURVEILLANCE_VERB_RE.search(low) and _SURVEILLANCE_SUBJECT_RE.search(low):
        return "fabricated_surveillance_capability"
    if prior_response and _is_substantially_repetitive(text, prior_response):
        return "substantially_repetitive"
    return None


def _build_direct_social_prompt(
    content: str, history: list[dict], entity_note: str, subtype: "SocialSubtype | None",
    procrastination_nudge_authorized: bool, available_context_claims: tuple,
) -> str:
    max_sentences, max_length = _subtype_limits(subtype)
    lines = []
    subtype_label = subtype.value if subtype is not None else "general"
    lines.append(f"Message type: {subtype_label}. Reply target: at most {max_sentences} sentence(s), roughly {max_length} characters — shorter is fine, longer is not.")

    claims = list(available_context_claims)
    if subtype is SocialSubtype.CAPABILITY_QUESTION:
        claims = list(_CAPABILITY_SUMMARY_CLAIMS) + claims
    if claims:
        lines.append("Known right now — you may reference ONLY these facts about the present, nothing else:")
        for c in claims:
            lines.append(f"  - {c}")
    else:
        lines.append("observed_current_state: none — you have no information about what he's currently doing, viewing, or how he feels beyond what he just said.")
    lines.append(f"procrastination_nudge_authorized: {'true' if procrastination_nudge_authorized else 'false'}")

    if history:
        lines.append("")
        lines.append("Recent conversation (oldest first):")
        for turn in history:
            speaker = "Nathan" if turn["role"] == "user" else "You"
            text = turn["content"] if isinstance(turn["content"], str) else str(turn["content"])
            lines.append(f"{speaker}: {text}")
    if entity_note:
        lines.append("")
        lines.append(entity_note)
    lines.append("")
    lines.append(f"Nathan just said: {content}")
    lines.append("Reply directly to him now, in character, following the identity, voice, and hard limits above.")
    return "\n".join(lines)


async def render_direct_social(
    content: str,
    *,
    history: "list[dict] | None" = None,
    entity_note: str = "",
    subtype: "SocialSubtype | None" = None,
    procrastination_nudge_authorized: bool = False,
    available_context_claims: tuple = (),
    deadline_seconds: "float | None" = None,
    model_key: str = DIRECT_SOCIAL_MODEL_KEY,
) -> DirectSocialResult:
    """Renders one direct-conversation reply. Never raises for expected
    failure modes. Validation failure after one retry returns ok=False —
    the caller (daemon.handle_chat) falls back to the existing
    route_model()+tool-calling path for this message, never ships an
    unvalidated reply.

    `history`'s most recent assistant turn (if any) is used for the
    repetition check — a fresh retry is asked for if the new reply
    substantially repeats it."""
    from config import DIRECT_SOCIAL_SYSTEM_PROMPT

    deadline = deadline_seconds if deadline_seconds is not None else DIRECT_SOCIAL_DEADLINE_SECONDS
    history = history or []
    prior_response = None
    for turn in reversed(history):
        if turn["role"] == "assistant":
            prior_response = turn["content"] if isinstance(turn["content"], str) else str(turn["content"])
            break

    prompt = _build_direct_social_prompt(
        content, history, entity_note, subtype, procrastination_nudge_authorized, available_context_claims,
    )

    for attempt in range(PERSONALITY_RENDER_MAX_RETRIES + 1):
        p = prompt
        if attempt > 0:
            p += (
                "\n\nYour previous reply was invalid (too long, claimed an action, invented a "
                "present-state fact, claimed a tracking/monitoring capability you don't have, "
                "used unauthorized procrastination language, explained a metaphor instead of "
                "embodying it, slipped into advice-column phrasing, misspelled Huginn or Muninn, "
                "called Nathan a raven, or substantially repeated your last reply). Answer "
                "freshly and plainly instead."
            )
        try:
            raw = await render_personality_only(
                DIRECT_SOCIAL_SYSTEM_PROMPT, p, purpose=Purpose.DIRECT, deadline_seconds=deadline,
                max_tokens=_max_tokens_for(subtype), model_key=model_key,
            )
        except CoordinatorDenied as e:
            log.info("direct_social denied: attempt=%d model_key=%s denial=%s", attempt, model_key, e.denial.value)
            return DirectSocialResult(False, None, f"coordinator_denied:{e.denial.value}")
        except Exception as e:
            log.info("direct_social error: attempt=%d model_key=%s error_type=%s", attempt, model_key, type(e).__name__)
            return DirectSocialResult(False, None, "error")

        text = raw.strip()
        problem = _validate_direct_social(
            text, subtype=subtype, procrastination_nudge_authorized=procrastination_nudge_authorized,
            prior_response=prior_response,
        )
        if problem is None:
            log.info("direct_social ok: attempt=%d model_key=%s chars=%d subtype=%s", attempt, model_key, len(text), subtype_label(subtype))
            return DirectSocialResult(True, text, "rendered")
        log.info("direct_social validation failed: attempt=%d model_key=%s problem=%s subtype=%s", attempt, model_key, problem, subtype_label(subtype))

    return DirectSocialResult(False, None, "validation_failed")


def subtype_label(subtype: "SocialSubtype | None") -> str:
    return subtype.value if subtype is not None else "general"
