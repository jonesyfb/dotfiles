"""
Action provenance: authoritative state for tool-executed actions.

Root cause this exists to fix (scripts/direct_chat_bench/results/20260903T080001Z,
"Add milk to my calendar tomorrow." / "Remember that my favorite color is
green."): the system had no authoritative record of what a tool call
actually did. A calendar-write request with no corresponding tool let the
model free-associate tool-call-shaped prose ("[tool] calendar_list
days=2") that was never a real structured tool call, just ordinary
content tokens shown to the user unfiltered. A successful `remember` call
returned a result string that omitted the actual stored value, so the
follow-up narration model reconstructed it from memory and drifted
("green" -> "Mint green"). Neither case is a false SUCCESS claim about
provenance exactly — the deeper problem is that nothing in the system
ever composed a final response FROM the authoritative result; both paths
always deferred to a second free-form model completion with no binding
between what was said and what actually happened.

Only `execute()` in this module may transition a record into SUCCEEDED
or FAILED. Model-authored text is never consulted for that transition —
callers pass it a tool name and validated arguments, and it returns an
ActionRecord whose `state` is the sole source of truth for what to tell
the user. Every real tool invocation, from both the Ollama and Claude/
cloud backends alike (daemon.py calls this from both the auto-tier loop
and the confirm-tier resume path), goes through here, so both obey
identical action-state semantics.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from tools import run_tool


class ActionState(Enum):
    PROPOSED = "proposed"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    DENIED = "denied"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


_TERMINAL_STATES = frozenset({
    ActionState.SUCCEEDED, ActionState.FAILED, ActionState.UNAVAILABLE,
    ActionState.DENIED, ActionState.CANCELLED, ActionState.TIMED_OUT,
})

# Tools that change durable state — only these get a deterministic final
# response composed straight from the action record. Read-only tools
# (calendar_list, recall, search_memory, web_search, get_weather,
# system_stats, read_file) are left to the model to narrate freely; there
# is no state-change claim about them that could be wrong the same way.
CONSEQUENTIAL_TOOLS = frozenset({
    "remember", "forget", "write_file", "queue_task", "notify", "shell", "claude_code",
})

# tools._shell / tools._claude_code already enforce their own internal
# timeouts (30s / 300s) — these outer values exist only to catch a tool
# with no internal timeout of its own hanging unexpectedly, so they're
# set comfortably above the known internal ones, not tuned as the primary
# bound.
_EXECUTE_TIMEOUT_SECONDS = {"claude_code": 310, "shell": 40}
_DEFAULT_EXECUTE_TIMEOUT_SECONDS = 30

_RESULT_SANITIZE_LENGTH = 500


@dataclass
class ActionRecord:
    action_id: str
    tool: str
    args: dict
    trust_tier: str
    state: ActionState
    confirmation_required: bool
    confirmed: "bool | None" = None
    result: "str | None" = None
    error: "str | None" = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def _touch(self) -> None:
        self.updated_at = time.time()


def propose(tool: str, args: dict, trust_tier: str) -> ActionRecord:
    """The starting record for any tool call, before it has run. `args`
    is the exact validated argument dict from the model's structured tool
    call — this is what confirmation later binds to; nothing re-parses
    arguments from the confirm request itself."""
    state = ActionState.AWAITING_CONFIRMATION if trust_tier == "confirm" else ActionState.PROPOSED
    return ActionRecord(
        action_id=str(uuid.uuid4()),
        tool=tool,
        args=dict(args),
        trust_tier=trust_tier,
        state=state,
        confirmation_required=(trust_tier == "confirm"),
    )


def deny(record: ActionRecord) -> ActionRecord:
    """A replayed/duplicate denial on an already-terminal record is a
    no-op — it cannot un-deny a record that already succeeded, failed, or
    was itself already denied."""
    if record.state in _TERMINAL_STATES:
        return record
    record.state = ActionState.DENIED
    record.confirmed = False
    record._touch()
    return record


def cancel(record: ActionRecord) -> ActionRecord:
    if record.state in _TERMINAL_STATES:
        return record
    record.state = ActionState.CANCELLED
    record._touch()
    return record


def _sanitize(text: str) -> str:
    """Single choke point for trimming tool output before it's stored or
    shown. No credential-shaped values are known to appear in current
    tool results, but if that ever changes, this is where redaction goes."""
    return text[:_RESULT_SANITIZE_LENGTH]


async def execute(record: ActionRecord) -> ActionRecord:
    """The ONLY function allowed to move a record into SUCCEEDED or
    FAILED. Idempotent against replay: calling this again on an
    already-terminal record returns it unchanged rather than running the
    tool a second time — this is what makes a duplicated confirm_id (or
    any other double-invocation) safe."""
    if record.state in _TERMINAL_STATES:
        return record

    if record.confirmation_required:
        record.confirmed = True
    record.state = ActionState.EXECUTING
    record._touch()

    timeout = _EXECUTE_TIMEOUT_SECONDS.get(record.tool, _DEFAULT_EXECUTE_TIMEOUT_SECONDS)
    try:
        raw = await asyncio.wait_for(run_tool(record.tool, record.args), timeout=timeout)
    except asyncio.TimeoutError:
        record.state = ActionState.TIMED_OUT
        record.error = "timed out before completion"
        record._touch()
        return record

    if raw.startswith("unknown tool:"):
        record.state = ActionState.UNAVAILABLE
        record.error = "no tool available for this action"
    elif raw.startswith("error: timed out"):
        record.state = ActionState.TIMED_OUT
        record.error = "timed out before completion"
    elif raw.startswith("error:"):
        record.state = ActionState.FAILED
        record.error = _sanitize(raw[len("error:"):].strip())
    else:
        record.state = ActionState.SUCCEEDED
        record.result = _sanitize(raw)
    record._touch()
    return record


def is_consequential(tool: str) -> bool:
    return tool in CONSEQUENTIAL_TOOLS


# ── Deterministic final-response composition for consequential tools ───────────
# A final response may say or imply that something was added, remembered,
# written, deleted, sent, scheduled, or completed ONLY when it's built
# from here — never from a second free-form model completion. Personality
# may still frame these further, within the existing semantic-safety
# validation in personality.py; this function's only job is making sure
# the CLAIM matches the authoritative state.

def compose_response(record: ActionRecord) -> str:
    if record.state == ActionState.SUCCEEDED:
        return _compose_success(record)
    if record.state in (ActionState.DENIED, ActionState.CANCELLED):
        return "I left it unchanged."
    if record.state == ActionState.UNAVAILABLE:
        return "I don't have a way to do that."
    if record.state == ActionState.FAILED:
        return f"That failed: {record.error or 'unknown error'}."
    if record.state == ActionState.TIMED_OUT:
        return "That didn't finish in time — I can't confirm whether it completed."
    return "I don't have a status for that yet."


def _compose_success(record: ActionRecord) -> str:
    tool, args = record.tool, record.args
    if tool == "remember":
        return f"Remembered: {args.get('key', 'that')} is {args.get('value', '')}."
    if tool == "forget":
        return f"Forgotten: {args.get('key', 'that')}."
    if tool == "notify":
        return "Sent."
    if tool == "queue_task":
        return f"Queued: {args.get('label', 'the task')}."
    if tool == "write_file":
        return f"Saved to {args.get('path', 'the file')}."
    if tool == "claude_code":
        return "Claude Code finished."
    if tool == "shell":
        return "Done."
    return "Done."


def compose_calendar_write_unavailable() -> str:
    """No calendar-write tool exists at all — used upstream, before any
    ActionRecord is even proposed, for a request that plainly wants one
    (see daemon._requests_calendar_write). Kept here so every "the system
    can't do this" phrasing lives in one place."""
    return "The calendar is unavailable, so I didn't add it."
