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

Presentation structure (hardened after the 2026-09-03 acceptance baseline —
see scripts/personality_bench/results/20260903T052457Z/report.md, Finding 1):
a small model asked to reproduce exact numbers/paths/commands/identifiers
character-for-character loses that contest often enough to matter (4/6
protected-value scenarios failed in the baseline — spelled-out numbers,
re-cased proper nouns, paraphrased error strings). So it is no longer asked
to. Every render() call now produces two genuinely separate things:

  - `flavor`        — short, model-authored mood/voice text. Never contains
                       a protected value; validated to contain no digits at
                       all, and rejected if it accidentally leaks one anyway.
  - `deterministic` — a plain-English factual sentence assembled by CODE
                       from request.facts/severity/reasoner_conclusion/
                       code_block, verbatim, with no model involved. Always
                       computed, even when flavor generation fails — this is
                       what a caller publishes instead of the flavor on
                       failure, and what makes a critical warning legible
                       with all personality text stripped away.

`text` is the two joined for display, wherever a caller only wants one
string (existing chime/notification consumers all just want a body string).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import memory
from config import (
    AMBIENT_RENDER_DEADLINE_SECONDS, PERSONALITY_RENDER_MAX_RETRIES,
    PERSONALITY_SYSTEM_PROMPT,
)
from coordinator import Purpose
from llm import CoordinatorDenied, render_personality_only

log = logging.getLogger("huginn.personality")

# Crude but effective tells for stage directions / roleplay leakage the
# system prompt asks the model not to produce — checked mechanically
# because a system prompt is a strong steer, not a guarantee.
_THEATRICAL_MARKERS = ("*", "[", "]")

# Stylistic fingerprints only — categories, never message content — fed
# back into the next prompt so the renderer can nudge itself away from
# repeating the same metaphor family back-to-back. See memory.log_style /
# memory.recent_styles.
_STYLE_CATEGORIES = {
    "predator_consumption": ("lion", "pride", "cub", "hunt", "prey", "eat", "feast", "hoard", "den", "roar", "claw", "mouth", "devour"),
    "weather_omen": ("storm", "omen", "sky", "thunder", "cloud", "wind", "weather", "portent"),
    "machinery_noise": ("gear", "engine", "hum", "grind", "clank", "machine", "noise", "buzz", "whirr", "static"),
    "territory_navigation": ("realm", "territory", "map", "border", "path", "route", "navigate", "compass", "shore"),
    "messages_bureaucracy": ("inbox", "mail", "form", "queue", "paperwork", "ledger", "office", "memo", "clerk"),
    "sleep_memory_ritual": ("sleep", "dream", "ritual", "rest", "wake", "vigil", "remember", "forget", "muninn"),
    "mischief_rivalry": ("mischief", "trick", "rival", "sneak", "prank", "gossip"),
}


@dataclass(frozen=True)
class PersonalityRequest:
    purpose: str                                   # event/opportunity type, e.g. "periodic_observation", "task_complete", "procrastination_nudge"
    facts: dict = field(default_factory=dict)       # authoritative facts, code-owned ground truth
    protected_keys: tuple = ()                      # keys in `facts` whose exact values the model is never shown and never asked to reproduce
    code_block: "str | None" = None                 # optional verbatim command/error block — code-owned, never shown to the model, appended as-is
    severity: str = "info"                          # "info" | "notice" | "critical" — reuses ambient.SEVERITIES vocabulary
    interruption_reason: str = ""                   # why speaking now was already approved (tone context only, not re-litigated here)
    max_length: int = 120                           # character cap on the model-authored flavor portion only
    prohibited_additions: tuple = ()                 # things the model must not introduce, e.g. ("diagnosis", "urgency")
    reasoner_conclusion: "str | None" = None         # optional stronger-model output — composed verbatim into the deterministic sentence, never shown to the model to paraphrase
    interaction_mode: str = "ambient"                # tone hint only — not a permission signal, that's already been checked upstream
    action_metadata: "dict | None" = None            # optional structured data for a future UI action — untouched by personality rendering, passed through as-is


@dataclass(frozen=True)
class RenderResult:
    ok: bool
    text: "str | None"          # flavor + deterministic, joined for display; None only when ok=False
    flavor: "str | None"        # model-authored portion alone (may be "" — a deliberately empty flavor is valid)
    deterministic: str          # code-composed factual sentence — ALWAYS present, even on failure
    reason: str                 # "rendered" | "coordinator_denied:<denial>" | "error" | "validation_failed"
    action_metadata: "dict | None" = None


def _compose_deterministic(request: PersonalityRequest) -> str:
    """Pure, code-owned. No model involved — this is what stays true (and
    legible) if all personality text is stripped away."""
    bits = []
    if request.severity == "critical":
        bits.append("CRITICAL:")
    fact_bits = [f"{key.replace('_', ' ')}: {value}" for key, value in request.facts.items()]
    if fact_bits:
        bits.append("; ".join(fact_bits))
    if request.reasoner_conclusion:
        bits.append(request.reasoner_conclusion)
    sentence = " ".join(b for b in bits if b).strip()
    if request.code_block:
        sentence = f"{sentence}\n{request.code_block}" if sentence else request.code_block
    return sentence


def _classify_style(flavor: str) -> str:
    if not flavor.strip():
        return "silent"
    low = flavor.lower()
    for category, words in _STYLE_CATEGORIES.items():
        if any(w in low for w in words):
            return category
    return "plain"


def _build_user_prompt(request: PersonalityRequest, recent_styles: tuple[str, ...] = ()) -> str:
    lines = [f"Event type: {request.purpose}", f"Severity: {request.severity}"]
    if request.interruption_reason:
        lines.append(f"Why this is being said now (already decided, do not re-justify it): {request.interruption_reason}")
    visible_facts = {k: v for k, v in request.facts.items() if k not in request.protected_keys}
    if visible_facts:
        lines.append("Context, for tone only — do not restate these as exact values:")
        for key, value in visible_facts.items():
            lines.append(f"  - {key}: {value}")
    if request.protected_keys:
        lines.append(
            "Some exact values for this event (numbers, names, paths, commands, errors) are "
            "withheld from you on purpose — the system will show them to the user separately, "
            "verbatim. Do not guess, restate, or invent them."
        )
    if request.prohibited_additions:
        lines.append("Do not add any of: " + ", ".join(request.prohibited_additions))
    if recent_styles:
        lines.append(
            "Your last few lines leaned on these styles: " + ", ".join(recent_styles) +
            ". Vary your approach this time if you reasonably can."
        )
    lines.append(f"Current interaction mode: {request.interaction_mode} (a tone hint only)")
    lines.append(f"Maximum length: {request.max_length} characters.")
    lines.append(
        "Respond with ONLY a short mood/voice reaction — no numbers, no exact names, no paths, "
        "no commands, no error text, no dates, no identifiers; those are shown separately by the "
        "system. No preamble, no quotes, no label. If there's nothing worth adding, respond with "
        "an empty line — that is a completely valid answer."
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
    for value in protected_literal_values:
        if value and value in text:
            return "leaked_protected_value"
    return None


async def render(
    request: PersonalityRequest,
    *,
    purpose: Purpose = Purpose.AMBIENT,
    deadline_seconds: "float | None" = None,
) -> RenderResult:
    """Renders `request` via qwen3.5:4b. Never raises for expected failure
    modes (coordinator denial, validation failure) — those come back as
    RenderResult(ok=False, ...) with `deterministic` still populated, so a
    caller can publish the deterministic factual notification for
    actionable content or stay silent for disposable ambient content, per
    its own policy. An unexpected exception is caught too; this module must
    never take the daemon down."""
    deadline = deadline_seconds if deadline_seconds is not None else AMBIENT_RENDER_DEADLINE_SECONDS
    deterministic = _compose_deterministic(request)
    protected_literal_values = tuple(
        str(request.facts[k]) for k in request.protected_keys if k in request.facts
    )
    recent_styles = tuple(memory.recent_styles(request.purpose, limit=3))
    base_prompt = _build_user_prompt(request, recent_styles)

    for attempt in range(PERSONALITY_RENDER_MAX_RETRIES + 1):
        prompt = base_prompt
        if attempt > 0:
            prompt += (
                "\n\nYour previous attempt was invalid (too long, contained a digit, contained "
                "formatting like asterisks/brackets, or repeated a withheld exact value). Be "
                "stricter this time: plain prose only, no digits, no formatting."
            )
        try:
            raw = await render_personality_only(
                PERSONALITY_SYSTEM_PROMPT, prompt, purpose=purpose, deadline_seconds=deadline,
            )
        except CoordinatorDenied as e:
            log.info(
                "personality render denied: purpose=%s severity=%s attempt=%d denial=%s",
                request.purpose, request.severity, attempt, e.denial.value,
            )
            return RenderResult(False, None, None, deterministic, f"coordinator_denied:{e.denial.value}", request.action_metadata)
        except Exception as e:
            log.info(
                "personality render error: purpose=%s severity=%s attempt=%d error_type=%s",
                request.purpose, request.severity, attempt, type(e).__name__,
            )
            return RenderResult(False, None, None, deterministic, "error", request.action_metadata)

        flavor = raw.strip()
        problem = _validate_flavor(flavor, request, protected_literal_values)
        if problem is None:
            log.info(
                "personality render ok: purpose=%s severity=%s attempt=%d flavor_chars=%d",
                request.purpose, request.severity, attempt, len(flavor),
            )
            memory.log_style(request.purpose, _classify_style(flavor))
            text = f"{flavor} {deterministic}".strip() if flavor else deterministic
            return RenderResult(True, text, flavor, deterministic, "rendered", request.action_metadata)
        log.info(
            "personality render validation failed: purpose=%s severity=%s attempt=%d problem=%s",
            request.purpose, request.severity, attempt, problem,
        )

    return RenderResult(False, None, None, deterministic, "validation_failed", request.action_metadata)
