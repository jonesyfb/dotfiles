"""
Ambient interruption policy.

Decides whether Huginn may say something unprompted, given a Runtime
Context Engine snapshot (context.py). This is deterministic policy code —
no model input reaches decide() and nothing here can be talked out of a
denial. A model may generate the *content* of an opportunity (chime text);
it never sees or influences the gate itself.

Two-phase usage by design: callers (e.g. daemon.py's random_chime_worker)
are expected to call decide() once *before* generating any text (to avoid
wasting a model call when cooldown/budget/suppression already deny it —
candidate_text is None, so dedup is skipped since there's nothing to
compare yet) and again *after* generation with candidate_text set, so the
dedup check can run against the actual rendered text. Both calls go through
the same function; nothing is duplicated by hand.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import context as ctx
from config import AMBIENT_COOLDOWN_SECONDS, AMBIENT_DAILY_BUDGET, AMBIENT_DEDUP_WINDOW, PERSONALITY_MODEL_KEY
from memory import last_ambient_event, recent_ambient_events

SEVERITIES = ("info", "notice", "critical")


@dataclass(frozen=True)
class AmbientOpportunity:
    kind: str
    severity: str = "info"
    candidate_text: str | None = None
    evidence: tuple[str, ...] = ()


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


def decide(opportunity: AmbientOpportunity, snapshot: ctx.RuntimeContext) -> AmbientDecision:
    """Pure function: never raises, always returns a decision with a reason.
    Deterministic — the same opportunity + snapshot always produces the
    same decision. No LLM output is consulted anywhere in this function."""
    severity = opportunity.severity if opportunity.severity in SEVERITIES else "info"
    interaction = snapshot.interaction
    manual = interaction.source.startswith("manual")

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

    last = last_ambient_event(opportunity.kind)
    if last is not None:
        elapsed = time.time() - last["ts"]
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
