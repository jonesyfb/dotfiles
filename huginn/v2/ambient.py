"""
Ambient interruption policy.

Decides whether Huginn may say something unprompted, given a Runtime
Context Engine snapshot (context.py). This is deterministic policy code —
no model input reaches decide() and nothing here can be talked out of a
denial. A model may generate the *content* of an opportunity (chime text);
it never sees or influences the gate itself.

decide() answers three separable questions, in order:
  A. Eligibility  — is Huginn currently allowed to interrupt at all
                     (interaction mode, attention, explicit snooze)?
  B. Worthiness   — is there a deterministic, positive reason to say this
                     particular thing? Default-deny: an opportunity with no
                     attached Worthiness signal is silence, not a maybe.
  C. Presentation — cooldown/budget/dedup shape HOW OFTEN an eligible,
                     worthy opportunity may actually surface.

Two-phase usage by design: callers (e.g. daemon.py's random_chime_worker)
are expected to call decide() once *before* generating any text (to avoid
wasting a model call when policy already denies it — candidate_text is
None, so dedup is skipped since there's nothing to compare yet) and again
*after* generation with candidate_text set, so the dedup check can run
against the actual rendered text. Both calls go through the same function;
nothing is duplicated by hand.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import context as ctx
import memory
from config import AMBIENT_COOLDOWN_SECONDS, AMBIENT_DAILY_BUDGET, AMBIENT_DEDUP_WINDOW, PERSONALITY_MODEL_KEY

SEVERITIES = ("info", "notice", "critical")

# Normalized worthiness reasons a caller may claim. Anything else (typo,
# freeform prose, missing) is treated as no signal at all — the LLM never
# supplies this, only deterministic caller code that already knows why an
# opportunity exists.
WORTHY_REASONS = frozenset({
    "deviation_from_baseline",
    "state_transition",
    "novelty",
    "user_relevance",
    "requested_follow_up",
    "actionable_failure",
    "actionable_success",
    "approved_procrastination_evidence",
})


@dataclass(frozen=True)
class Worthiness:
    reason: str
    confidence: float = 1.0


@dataclass(frozen=True)
class AmbientOpportunity:
    kind: str
    severity: str = "info"
    candidate_text: str | None = None
    evidence: tuple[str, ...] = ()
    worthiness: "Worthiness | None" = None  # None = no positive signal = default-deny


@dataclass(frozen=True)
class AmbientDecision:
    allowed: bool
    reason: str
    severity: str
    cooldown_seconds: float
    capability: str | None          # rendering capability sanctioned if allowed, else None
    basis: str                      # "observed" | "inferred" | "manual_override"
    evidence: tuple[str, ...] = ()


def _capability(snapshot: ctx.RuntimeContext) -> str:
    """Which model capability is available to render this, if allowed.
    Informational only in this slice — the caller is not required to
    switch its model call to match (SYSTEM_PROMPT / general chat routing
    are explicitly out of scope here)."""
    personality = snapshot.models.get(PERSONALITY_MODEL_KEY)
    if personality is not None and personality.available:
        return PERSONALITY_MODEL_KEY
    return "fast"


def _dedup_hit(kind: str, text: str) -> tuple[bool, tuple[str, ...]]:
    for row in recent_ambient_events(kind, within_seconds=AMBIENT_DEDUP_WINDOW):
        if row["text"].strip() == text.strip():
            return True, (f"matches a {kind} event from within the dedup window",)
    return False, ()


def recent_ambient_events(kind: str, within_seconds: int):
    return memory.recent_ambient_events(kind, within_seconds)


def last_ambient_event(kind: str):
    return memory.last_ambient_event(kind)


def _is_worthy(w: "Worthiness | None") -> bool:
    """Malformed or insufficient worthiness (missing, unknown reason,
    non-numeric/zero/negative/over-1.0 confidence) defaults to False —
    silence, never a maybe."""
    if w is None:
        return False
    if w.reason not in WORTHY_REASONS:
        return False
    try:
        confidence = float(w.confidence)
    except (TypeError, ValueError):
        return False
    return 0.0 < confidence <= 1.0


def global_snooze_scope() -> str:
    return "global"


def kind_snooze_scope(kind: str) -> str:
    return f"kind:{kind}"


def _active_snooze(scope: str, *, now: float | None = None) -> dict | None:
    """[start, expiry) semantics: active while now < expires_at, expired
    (and therefore inert) the instant now == expires_at."""
    now = now if now is not None else time.time()
    row = memory.get_snooze(scope)
    if row is None:
        return None
    if now >= row["expires_at"]:
        return None
    return row


def set_snooze(
    scope: str, duration_seconds: float, *, origin: str = "manual", reason: str | None = None,
    now: float | None = None,
) -> dict:
    """Explicit snooze/dismissal, distinct from the automatic cooldown below.
    Duration is a plain number of seconds decided by the caller (the
    command/input layer) — this function never parses natural language and
    never infers a duration itself."""
    now = now if now is not None else time.time()
    expires_at = now + duration_seconds
    memory.set_snooze(scope, expires_at, origin=origin, reason=reason)
    return {"scope": scope, "expires_at": expires_at, "origin": origin, "reason": reason}


def clear_snooze(scope: str) -> None:
    memory.clear_snooze(scope)


def evaluate_periodic_worthiness(
    kind: str, numeric_facts: dict[str, float], *, threshold_ratio: float = 0.25,
) -> "Worthiness | None":
    """Deterministic baseline-deviation check for periodic/observational
    opportunities that have no other worthiness signal available. Compares
    each numeric fact against its last-seen value for this kind; a
    >=threshold_ratio relative change on any single metric counts as a
    meaningful transition. The baseline is updated to the latest values on
    every call regardless of outcome, so the next call always compares
    against what actually happened this time — an unchanging value never
    accumulates false worthiness. The very first observation of a metric
    has nothing to compare against and is never itself worthy — recording
    a baseline is not news."""
    worthy = False
    strongest = 0.0
    for metric, value in numeric_facts.items():
        prev = memory.get_baseline(kind, metric)
        memory.set_baseline(kind, metric, value)
        if prev is None or prev == 0:
            continue
        change = abs(value - prev) / abs(prev)
        if change >= threshold_ratio:
            worthy = True
            strongest = max(strongest, change)
    if not worthy:
        return None
    return Worthiness(reason="deviation_from_baseline", confidence=min(1.0, strongest))


def decide(
    opportunity: AmbientOpportunity, snapshot: ctx.RuntimeContext, *, now: float | None = None,
) -> AmbientDecision:
    """Pure function (aside from reading persisted cooldown/snooze/dedup
    state): never raises, always returns a decision with a reason. No LLM
    output is consulted anywhere in this function."""
    now = now if now is not None else time.time()
    severity = opportunity.severity if opportunity.severity in SEVERITIES else "info"
    interaction = snapshot.interaction
    manual = interaction.source.startswith("manual")

    # ── A. Eligibility ──────────────────────────────────────────────────────
    # Interaction-mode suppression (game/quiet/focus/meeting/sleep — whatever
    # currently sets interruptions_allowed=False). Critical severity is the
    # only thing allowed to bypass this, per explicit policy.
    if not interaction.interruptions_allowed:
        if severity == "critical":
            return AmbientDecision(
                True, f"critical severity bypasses {interaction.mode!r} suppression",
                severity, cooldown_seconds=0, capability=_capability(snapshot),
                basis="manual_override" if manual else "inferred",
                evidence=interaction.evidence,
            )
        return AmbientDecision(
            False, f"suppressed: interaction mode is {interaction.mode!r}",
            severity, cooldown_seconds=0, capability=None,
            basis="manual_override" if manual else "inferred",
            evidence=interaction.evidence,
        )

    # Attention can independently say do-not-disturb (e.g. an active call)
    # even when the interaction mode itself allows interruptions. Critical
    # severity bypasses this too, same as above.
    if snapshot.attention.level == "do_not_disturb":
        if severity == "critical":
            return AmbientDecision(
                True, "critical severity bypasses do_not_disturb attention",
                severity, cooldown_seconds=0, capability=_capability(snapshot),
                basis="observed", evidence=snapshot.attention.evidence,
            )
        return AmbientDecision(
            False, "suppressed: attention is do_not_disturb", severity,
            cooldown_seconds=0, capability=None, basis="observed",
            evidence=snapshot.attention.evidence,
        )

    # Explicit manual snooze/dismissal — distinct from automatic cooldown
    # below. Global outranks per-kind (checked first); neither suppresses a
    # critical warning.
    global_snooze = _active_snooze(global_snooze_scope(), now=now)
    if global_snooze is not None and severity != "critical":
        remaining = global_snooze["expires_at"] - now
        return AmbientDecision(
            False, f"suppressed: global snooze active ({remaining:.0f}s remaining)", severity,
            cooldown_seconds=remaining, capability=None,
            basis="manual_override" if global_snooze["origin"] == "manual" else "inferred",
            evidence=(f"snooze reason: {global_snooze['reason']}",) if global_snooze["reason"] else (),
        )

    kind_snooze = _active_snooze(kind_snooze_scope(opportunity.kind), now=now)
    if kind_snooze is not None and severity != "critical":
        remaining = kind_snooze["expires_at"] - now
        return AmbientDecision(
            False, f"suppressed: {opportunity.kind!r} snooze active ({remaining:.0f}s remaining)", severity,
            cooldown_seconds=remaining, capability=None,
            basis="manual_override" if kind_snooze["origin"] == "manual" else "inferred",
            evidence=(f"snooze reason: {kind_snooze['reason']}",) if kind_snooze["reason"] else (),
        )

    # ── B. Worthiness ───────────────────────────────────────────────────────
    # Critical severity is inherently worthy (a critical condition is never
    # "nothing worth saying"); everything else needs a positive, deterministic
    # signal the caller attached — never derived from the model.
    if severity != "critical" and not _is_worthy(opportunity.worthiness):
        return AmbientDecision(
            False, "not worthwhile: no positive worthiness signal attached", severity,
            cooldown_seconds=0, capability=None, basis="observed",
            evidence=(f"worthiness={opportunity.worthiness!r}",),
        )

    # ── C. Presentation-adjacent throttling (unchanged) ─────────────────────
    last = last_ambient_event(opportunity.kind)
    if last is not None:
        elapsed = now - last["ts"]
        if elapsed < AMBIENT_COOLDOWN_SECONDS:
            remaining = AMBIENT_COOLDOWN_SECONDS - elapsed
            return AmbientDecision(
                False, f"cooldown active ({remaining:.0f}s remaining)", severity,
                cooldown_seconds=remaining, capability=None, basis="observed",
                evidence=(f"last {opportunity.kind!r} event {elapsed:.0f}s ago",),
            )

    recent_count = len(recent_ambient_events(opportunity.kind, within_seconds=86400))
    if recent_count >= AMBIENT_DAILY_BUDGET:
        return AmbientDecision(
            False, f"daily interruption budget exhausted ({recent_count}/{AMBIENT_DAILY_BUDGET})",
            severity, cooldown_seconds=AMBIENT_COOLDOWN_SECONDS, capability=None,
            basis="observed", evidence=(f"{recent_count} {opportunity.kind!r} event(s) in last 24h",),
        )

    if opportunity.candidate_text:
        hit, evidence = _dedup_hit(opportunity.kind, opportunity.candidate_text)
        if hit:
            return AmbientDecision(
                False, "suppressed: near-duplicate of a recent observation", severity,
                cooldown_seconds=AMBIENT_COOLDOWN_SECONDS, capability=None,
                basis="observed", evidence=evidence,
            )

    return AmbientDecision(
        True, "allowed", severity, cooldown_seconds=AMBIENT_COOLDOWN_SECONDS,
        capability=_capability(snapshot), basis="observed",
    )
