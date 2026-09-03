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

Exact numbers, paths, commands, and other protected values are validated
mechanically after generation, not trusted from the prompt alone — an LLM
follows instructions probabilistically, not by contract.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

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


@dataclass(frozen=True)
class PersonalityRequest:
    purpose: str                                   # event/opportunity type, e.g. "periodic_observation", "task_complete", "procrastination_nudge"
    facts: dict = field(default_factory=dict)       # authoritative facts as flat key -> string value, shown to the model verbatim
    severity: str = "info"                          # "info" | "notice" | "critical" — reuses ambient.SEVERITIES vocabulary
    interruption_reason: str = ""                   # why speaking now was already approved (tone context only, not re-litigated here)
    max_length: int = 200                            # character cap, validated after generation
    prohibited_additions: tuple = ()                 # things the model must not introduce, e.g. ("diagnosis", "urgency")
    protected_values: tuple = ()                     # exact substrings that MUST appear verbatim if the render references them at all
    reasoner_conclusion: "str | None" = None         # optional stronger-model output to phrase, preserving its uncertainty exactly
    interaction_mode: str = "ambient"                # tone hint only — not a permission signal, that's already been checked upstream


@dataclass(frozen=True)
class RenderResult:
    ok: bool
    text: "str | None"
    reason: str  # "rendered" | "coordinator_denied:<denial>" | "error" | "validation_failed"


def _build_user_prompt(request: PersonalityRequest) -> str:
    lines = [f"Event type: {request.purpose}", f"Severity: {request.severity}"]
    if request.interruption_reason:
        lines.append(f"Why this is being said now (already decided, do not re-justify it): {request.interruption_reason}")
    if request.facts:
        lines.append("Authoritative facts — do not invent, alter, omit the meaning of, or contradict these:")
        for key, value in request.facts.items():
            lines.append(f"  - {key}: {value}")
    if request.reasoner_conclusion:
        lines.append(f"A more capable model already concluded: {request.reasoner_conclusion}")
        lines.append("Preserve its uncertainty exactly — if it hedged, your line must hedge too.")
    if request.prohibited_additions:
        lines.append("Do not add any of: " + ", ".join(request.prohibited_additions))
    if request.protected_values:
        lines.append(
            "These exact values MUST appear character-for-character if you reference "
            "them at all: " + ", ".join(request.protected_values)
        )
    lines.append(f"Current interaction mode: {request.interaction_mode} (a tone hint only)")
    lines.append(f"Maximum length: {request.max_length} characters.")
    lines.append(
        "Respond with ONLY the line Huginn would say — no preamble, no quotes, "
        "no explanation, no label. If there's nothing worth saying, respond with "
        "an empty line."
    )
    return "\n".join(lines)


def _validate(text: str, request: PersonalityRequest) -> "str | None":
    """Returns None if the rendered text is acceptable, else a short
    machine-readable problem code — never the text itself in the reason,
    callers may log this code freely."""
    if not text.strip():
        return "empty"
    if len(text) > request.max_length:
        return "too_long"
    if any(marker in text for marker in _THEATRICAL_MARKERS):
        return "theatrical_formatting"
    if text.strip().lower().startswith("huginn:"):
        return "self_prefixed"
    for value in request.protected_values:
        if value not in text:
            return "missing_protected_value"
    return None


async def render(
    request: PersonalityRequest,
    *,
    purpose: Purpose = Purpose.AMBIENT,
    deadline_seconds: "float | None" = None,
) -> RenderResult:
    """Renders `request` via qwen3.5:4b. Never raises for expected failure
    modes (coordinator denial, validation failure, empty output) — those
    all come back as RenderResult(ok=False, ...) with a reason the caller
    can act on (silence for disposable ambient content, a deterministic
    fallback message for anything more consequential). An unexpected
    exception is caught too; this module must never take the daemon down."""
    deadline = deadline_seconds if deadline_seconds is not None else AMBIENT_RENDER_DEADLINE_SECONDS
    base_prompt = _build_user_prompt(request)

    for attempt in range(PERSONALITY_RENDER_MAX_RETRIES + 1):
        prompt = base_prompt
        if attempt > 0:
            prompt += (
                "\n\nYour previous attempt was invalid (too long, missing a required "
                "exact value, or contained formatting like asterisks/brackets). "
                "Be stricter this time: plain prose only, no formatting, no missing values."
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
            return RenderResult(False, None, f"coordinator_denied:{e.denial.value}")
        except Exception as e:
            log.info(
                "personality render error: purpose=%s severity=%s attempt=%d error_type=%s",
                request.purpose, request.severity, attempt, type(e).__name__,
            )
            return RenderResult(False, None, "error")

        text = raw.strip()
        problem = _validate(text, request)
        if problem is None:
            log.info(
                "personality render ok: purpose=%s severity=%s attempt=%d chars=%d",
                request.purpose, request.severity, attempt, len(text),
            )
            return RenderResult(True, text, "rendered")
        log.info(
            "personality render validation failed: purpose=%s severity=%s attempt=%d problem=%s",
            request.purpose, request.severity, attempt, problem,
        )

    return RenderResult(False, None, "validation_failed")
