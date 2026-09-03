"""Tests for action provenance (v2/actions.py).

Root cause this exists to fix (scripts/direct_chat_bench/results/
20260903T080001Z): the system had no authoritative record of what a tool
call actually did, letting a follow-up completion narrate — and
sometimes drift from — the real outcome. These tests prove the state
machine itself: only execute() can reach SUCCEEDED/FAILED, replay is
inert, and compose_response() never claims more than the state supports.

All mocked at tools.run_tool — no real subprocess/network/sqlite call in
this file.
"""
import asyncio

import pytest

import actions
from actions import ActionState


def test_propose_auto_tier_starts_proposed():
    record = actions.propose("remember", {"key": "x", "value": "y"}, "auto")
    assert record.state == ActionState.PROPOSED
    assert record.confirmation_required is False


def test_propose_confirm_tier_starts_awaiting_confirmation():
    record = actions.propose("shell", {"command": "rm -rf /"}, "confirm")
    assert record.state == ActionState.AWAITING_CONFIRMATION
    assert record.confirmation_required is True


def test_propose_stores_exact_validated_args():
    args = {"key": "favorite_color", "value": "green"}
    record = actions.propose("remember", args, "auto")
    assert record.args == args
    args["key"] = "mutated"  # mutating the caller's dict afterward must not affect the record
    assert record.args["key"] == "favorite_color"


# ── execute(): the only path to SUCCEEDED/FAILED ────────────────────────────

def test_execute_success(monkeypatch):
    async def fake_run_tool(name, args):
        return "remembered: favorite_color=green"

    monkeypatch.setattr(actions, "run_tool", fake_run_tool)

    record = actions.propose("remember", {"key": "favorite_color", "value": "green"}, "auto")
    record = asyncio.run(actions.execute(record))

    assert record.state == ActionState.SUCCEEDED
    assert record.result == "remembered: favorite_color=green"
    assert record.error is None


def test_execute_unknown_tool_is_unavailable(monkeypatch):
    async def fake_run_tool(name, args):
        return f"unknown tool: {name}"

    monkeypatch.setattr(actions, "run_tool", fake_run_tool)

    record = actions.propose("not_a_real_tool", {}, "auto")
    record = asyncio.run(actions.execute(record))

    assert record.state == ActionState.UNAVAILABLE


def test_execute_error_string_is_failed(monkeypatch):
    async def fake_run_tool(name, args):
        return "error: something broke"

    monkeypatch.setattr(actions, "run_tool", fake_run_tool)

    record = actions.propose("shell", {"command": "false"}, "auto")
    record = asyncio.run(actions.execute(record))

    assert record.state == ActionState.FAILED
    assert record.error == "something broke"


def test_execute_timeout_from_run_tool_sentinel(monkeypatch):
    async def fake_run_tool(name, args):
        return "error: timed out"

    monkeypatch.setattr(actions, "run_tool", fake_run_tool)

    record = actions.propose("shell", {"command": "sleep 999"}, "auto")
    record = asyncio.run(actions.execute(record))

    assert record.state == ActionState.TIMED_OUT
    assert record.state != ActionState.FAILED  # a timeout is not a failure claim


def test_execute_outer_wait_for_timeout(monkeypatch):
    async def hangs_forever(name, args):
        await asyncio.sleep(999)

    monkeypatch.setattr(actions, "run_tool", hangs_forever)
    monkeypatch.setattr(actions, "_DEFAULT_EXECUTE_TIMEOUT_SECONDS", 0.05)

    record = actions.propose("remember", {"key": "x", "value": "y"}, "auto")
    record = asyncio.run(actions.execute(record))

    assert record.state == ActionState.TIMED_OUT


def test_execute_is_idempotent_against_replay(monkeypatch):
    calls = {"n": 0}

    async def fake_run_tool(name, args):
        calls["n"] += 1
        return "remembered: x=y"

    monkeypatch.setattr(actions, "run_tool", fake_run_tool)

    record = actions.propose("remember", {"key": "x", "value": "y"}, "auto")
    record = asyncio.run(actions.execute(record))
    record = asyncio.run(actions.execute(record))  # replay

    assert calls["n"] == 1
    assert record.state == ActionState.SUCCEEDED


def test_execute_never_reruns_after_denial(monkeypatch):
    record = actions.propose("remember", {"key": "x", "value": "y"}, "confirm")
    record = actions.deny(record)

    calls = {"n": 0}

    async def fake_run_tool(name, args):
        calls["n"] += 1
        return "remembered: x=y"

    monkeypatch.setattr(actions, "run_tool", fake_run_tool)
    result = asyncio.run(actions.execute(record))

    assert calls["n"] == 0
    assert result.state == ActionState.DENIED


# ── deny()/cancel(): terminal, replay-safe ──────────────────────────────────

def test_deny_transitions_to_denied():
    record = actions.propose("shell", {"command": "rm x"}, "confirm")
    record = actions.deny(record)
    assert record.state == ActionState.DENIED
    assert record.confirmed is False


def test_deny_replay_on_terminal_record_is_noop():
    record = actions.propose("shell", {"command": "rm x"}, "confirm")
    record = actions.deny(record)
    record2 = actions.deny(record)  # replayed denial
    assert record2.state == ActionState.DENIED


def test_deny_cannot_undo_a_success(monkeypatch):
    async def fake_run_tool(name, args):
        return "remembered: x=y"

    monkeypatch.setattr(actions, "run_tool", fake_run_tool)

    record = actions.propose("remember", {"key": "x", "value": "y"}, "auto")
    record = asyncio.run(actions.execute(record))
    record = actions.deny(record)  # nonsensical replay after success

    assert record.state == ActionState.SUCCEEDED  # cannot be overwritten


def test_cancel_transitions_to_cancelled():
    record = actions.propose("shell", {"command": "rm x"}, "confirm")
    record = actions.cancel(record)
    assert record.state == ActionState.CANCELLED


# ── compose_response(): claims never exceed the authoritative state ─────────

def test_compose_response_remember_success():
    record = actions.propose("remember", {"key": "favorite_color", "value": "green"}, "auto")
    record.state = ActionState.SUCCEEDED
    assert actions.compose_response(record) == "Remembered: favorite_color is green."


def test_compose_response_forget_success():
    record = actions.propose("forget", {"key": "favorite_color"}, "auto")
    record.state = ActionState.SUCCEEDED
    assert actions.compose_response(record) == "Forgotten: favorite_color."


def test_compose_response_denied():
    record = actions.propose("shell", {"command": "rm x"}, "confirm")
    record = actions.deny(record)
    assert actions.compose_response(record) == "I left it unchanged."


def test_compose_response_cancelled():
    record = actions.propose("shell", {"command": "rm x"}, "confirm")
    record = actions.cancel(record)
    assert actions.compose_response(record) == "I left it unchanged."


def test_compose_response_unavailable():
    record = actions.propose("not_a_real_tool", {}, "auto")
    record.state = ActionState.UNAVAILABLE
    assert actions.compose_response(record) == "I don't have a way to do that."


def test_compose_response_failed_states_exact_error():
    record = actions.propose("shell", {"command": "false"}, "auto")
    record.state = ActionState.FAILED
    record.error = "exit code 1"
    assert actions.compose_response(record) == "That failed: exit code 1."


def test_compose_response_timed_out_is_not_success_or_failure_claim():
    record = actions.propose("shell", {"command": "sleep 999"}, "auto")
    record.state = ActionState.TIMED_OUT
    text = actions.compose_response(record)
    assert "failed" not in text.lower()
    assert "succeeded" not in text.lower()
    assert "unconfirmed" in text.lower() or "can't confirm" in text.lower()


def test_compose_calendar_write_unavailable_exact_wording():
    assert actions.compose_calendar_write_unavailable() == "The calendar is unavailable, so I didn't add it."


@pytest.mark.parametrize("state", [
    ActionState.DENIED, ActionState.CANCELLED, ActionState.UNAVAILABLE,
    ActionState.FAILED, ActionState.TIMED_OUT, ActionState.PROPOSED,
    ActionState.AWAITING_CONFIRMATION, ActionState.EXECUTING,
])
def test_no_success_claim_without_succeeded_state(state):
    """The core guarantee: compose_response() never says or implies
    something was added/remembered/written/deleted/sent/scheduled/
    completed unless state is SUCCEEDED."""
    record = actions.propose("remember", {"key": "x", "value": "y"}, "auto")
    record.state = state
    record.error = "some error"
    text = actions.compose_response(record).lower()
    banned = ("remembered:", "forgotten:", "sent.", "queued:", "saved to", "done.", "finished.")
    assert not any(b in text for b in banned), f"state={state} produced a success-shaped claim: {text!r}"


# ── is_consequential() ───────────────────────────────────────────────────────

@pytest.mark.parametrize("tool", ["remember", "forget", "write_file", "queue_task", "notify", "shell", "claude_code"])
def test_is_consequential_true(tool):
    assert actions.is_consequential(tool) is True


@pytest.mark.parametrize("tool", ["calendar_list", "recall", "search_memory", "web_search", "get_weather", "system_stats", "read_file"])
def test_is_consequential_false(tool):
    assert actions.is_consequential(tool) is False


# ── sanitization ─────────────────────────────────────────────────────────────

def test_execute_sanitizes_long_result(monkeypatch):
    async def fake_run_tool(name, args):
        return "x" * 10000

    monkeypatch.setattr(actions, "run_tool", fake_run_tool)

    record = actions.propose("remember", {"key": "x", "value": "y"}, "auto")
    record = asyncio.run(actions.execute(record))

    assert len(record.result) <= actions._RESULT_SANITIZE_LENGTH
