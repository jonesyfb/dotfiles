"""Daemon-level tests for tool-result integrity and action provenance:
the calendar-write short-circuit, deterministic composition for
consequential/failed tool calls, pseudo-tool-transcript detection, and
confirmation binding/replay safety.

All mocked at stream_chat/actions.execute — no real Ollama/Claude call
anywhere in this file.
"""
import asyncio
import json

import actions
import daemon
from actions import ActionRecord, ActionState


def _use_temp_db(tmp_path, monkeypatch):
    import memory
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")


class _CaptureWriter:
    def __init__(self):
        self.events = []

    def write(self, data):
        for line in data.decode().splitlines():
            if line.strip():
                self.events.append(json.loads(line))

    async def drain(self):
        pass

    def full_text(self):
        return "".join(e.get("content", "") for e in self.events if e["type"] == "token")


# ── Calendar-write short-circuit (root cause #1) ────────────────────────────

def test_calendar_write_never_calls_the_model(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def spy_stream_chat(*a, **kw):
        calls["n"] += 1
        return
        yield  # pragma: no cover

    monkeypatch.setattr(daemon, "stream_chat", spy_stream_chat)

    writer = _CaptureWriter()
    asyncio.run(daemon._handle_chat_via_existing_route(writer, "Add milk to my calendar tomorrow."))

    assert calls["n"] == 0
    assert writer.full_text() == "The calendar is unavailable, so I didn't add it."


def test_calendar_read_request_still_uses_normal_flow(tmp_path, monkeypatch):
    """Only a WRITE-shaped calendar request is short-circuited — a read
    request must still reach the model/tool flow normally."""
    _use_temp_db(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def spy_stream_chat(*a, **kw):
        calls["n"] += 1
        yield {"type": "token", "content": "Nothing on your calendar today."}
        yield {"type": "done"}

    monkeypatch.setattr(daemon, "stream_chat", spy_stream_chat)

    writer = _CaptureWriter()
    asyncio.run(daemon._handle_chat_via_existing_route(writer, "What's on my calendar today?"))

    assert calls["n"] == 1


# ── Deterministic composition for consequential tools (root cause #2) ──────

def test_remember_tool_call_produces_deterministic_response_not_narration(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)

    call_count = {"n": 0}

    async def spy_stream_chat(messages, model_key, tools=None):
        call_count["n"] += 1
        yield {"type": "tool_call", "tool": "remember", "args": {"key": "favorite_color", "value": "green"}}
        yield {"type": "done"}

    async def fake_execute(record):
        record.state = ActionState.SUCCEEDED
        record.result = "remembered: favorite_color=green"
        return record

    monkeypatch.setattr(daemon, "stream_chat", spy_stream_chat)
    monkeypatch.setattr(actions, "execute", fake_execute)

    writer = _CaptureWriter()
    asyncio.run(daemon._handle_chat_via_existing_route(writer, "Remember that my favorite color is green."))

    # Exactly one stream_chat call — no second free-form narration pass.
    assert call_count["n"] == 1
    assert writer.full_text() == "Remembered: favorite_color is green."


def test_remember_tool_failure_states_the_error_not_success(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)

    async def spy_stream_chat(messages, model_key, tools=None):
        yield {"type": "tool_call", "tool": "remember", "args": {"key": "x", "value": "y"}}
        yield {"type": "done"}

    async def fake_execute(record):
        record.state = ActionState.FAILED
        record.error = "disk full"
        return record

    monkeypatch.setattr(daemon, "stream_chat", spy_stream_chat)
    monkeypatch.setattr(actions, "execute", fake_execute)

    writer = _CaptureWriter()
    asyncio.run(daemon._handle_chat_via_existing_route(writer, "Remember that x is y."))

    text = writer.full_text()
    assert "remembered" not in text.lower()
    assert "disk full" in text


def test_hallucinated_tool_name_defaults_to_confirm_tier(tmp_path, monkeypatch):
    """An unrecognized tool name isn't in TOOL_TRUST, so it defaults to
    "confirm" — it cannot auto-execute unguarded. This IS the fail-closed
    behavior for a malformed/hallucinated tool call: nothing runs without
    an explicit confirmation step first."""
    _use_temp_db(tmp_path, monkeypatch)

    async def spy_stream_chat(messages, model_key, tools=None):
        yield {"type": "tool_call", "tool": "add_calendar_event", "args": {"what": "milk"}}
        yield {"type": "done"}

    monkeypatch.setattr(daemon, "stream_chat", spy_stream_chat)

    writer = _CaptureWriter()
    asyncio.run(daemon._handle_chat_via_existing_route(writer, "Add milk to the fridge list tool please."))

    confirm_events = [e for e in writer.events if e["type"] == "confirm_required"]
    assert len(confirm_events) == 1
    assert confirm_events[0]["tool"] == "add_calendar_event"


def test_hallucinated_tool_name_approved_still_fails_closed_deterministically(tmp_path, monkeypatch):
    """If a hallucinated tool name is (mistakenly) approved anyway,
    execute() marks it UNAVAILABLE (run_tool's real "unknown tool:"
    branch) and the response is composed deterministically — never
    narrated as if it were a real result."""
    _use_temp_db(tmp_path, monkeypatch)

    record = actions.propose("add_calendar_event", {"what": "milk"}, "confirm")
    confirm_id = record.action_id
    daemon._pending_confirms[confirm_id] = {
        "record": record, "writer": None, "tool_calls": [], "history": [], "model_key": "fast",
    }

    writer = _CaptureWriter()
    asyncio.run(daemon.handle_confirm(writer, confirm_id, True))

    assert writer.full_text() == "I don't have a way to do that."


def test_readonly_tool_success_still_gets_model_narration(tmp_path, monkeypatch):
    """The one case free narration remains: a read-only tool that
    actually succeeded — nothing state-changing to get wrong."""
    _use_temp_db(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def spy_stream_chat(messages, model_key, tools=None):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {"type": "tool_call", "tool": "calendar_list", "args": {"days": 2}}
            yield {"type": "done"}
        else:
            yield {"type": "token", "content": "Nothing urgent coming up."}
            yield {"type": "done"}

    async def fake_execute(record):
        record.state = ActionState.SUCCEEDED
        record.result = "no events"
        return record

    monkeypatch.setattr(daemon, "stream_chat", spy_stream_chat)
    monkeypatch.setattr(actions, "execute", fake_execute)

    writer = _CaptureWriter()
    asyncio.run(daemon._handle_chat_via_existing_route(writer, "What's on my calendar?"))

    assert calls["n"] == 2  # tool call, then a real narration pass
    assert writer.full_text() == "Nothing urgent coming up."


# ── Pseudo-tool-transcript detection (root cause #1, general case) ─────────

def test_fake_tool_markup_in_plain_prose_is_corrected_not_shown_as_receipt(tmp_path, monkeypatch):
    async def spy_stream_chat(messages, model_key, tools=None):
        yield {"type": "token", "content": "[tool] calendar_add days=1"}
        yield {"type": "done"}

    monkeypatch.setattr(daemon, "stream_chat", spy_stream_chat)

    writer = _CaptureWriter()
    asyncio.run(daemon._handle_chat_via_existing_route(writer, "put a thing on my agenda system for me"))

    # The fabricated line was already streamed (can't unsend), but the
    # correction must follow in the same turn and be what's stored.
    assert "No tool actually ran" in writer.full_text()


def test_real_tool_call_is_not_flagged_as_fake_markup(tmp_path, monkeypatch):
    """A genuine structured tool_call must never trip the prose-markup
    defense — that check only runs when tool_calls_made is empty."""
    _use_temp_db(tmp_path, monkeypatch)

    async def spy_stream_chat(messages, model_key, tools=None):
        yield {"type": "tool_call", "tool": "remember", "args": {"key": "x", "value": "y"}}
        yield {"type": "done"}

    async def fake_execute(record):
        record.state = ActionState.SUCCEEDED
        record.result = "remembered: x=y"
        return record

    monkeypatch.setattr(daemon, "stream_chat", spy_stream_chat)
    monkeypatch.setattr(actions, "execute", fake_execute)

    writer = _CaptureWriter()
    asyncio.run(daemon._handle_chat_via_existing_route(writer, "Remember that x is y."))

    assert "No tool actually ran" not in writer.full_text()


# ── Confirmation binding and replay safety ──────────────────────────────────

def test_confirm_denial_produces_i_left_it_unchanged(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    record = actions.propose("shell", {"command": "rm -rf build/"}, "confirm")
    confirm_id = record.action_id
    daemon._pending_confirms[confirm_id] = {
        "record": record, "writer": None, "tool_calls": [], "history": [], "model_key": "fast",
    }

    writer = _CaptureWriter()
    asyncio.run(daemon.handle_confirm(writer, confirm_id, False))

    assert writer.full_text() == "I left it unchanged."
    assert confirm_id not in daemon._pending_confirms


def test_duplicate_confirmation_cannot_execute_twice(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def fake_run_tool(name, args):
        calls["n"] += 1
        return "remembered: x=y"

    monkeypatch.setattr(actions, "run_tool", fake_run_tool)

    record = actions.propose("remember", {"key": "x", "value": "y"}, "confirm")
    confirm_id = record.action_id
    daemon._pending_confirms[confirm_id] = {
        "record": record, "writer": None, "tool_calls": [], "history": [], "model_key": "fast",
    }

    writer1 = _CaptureWriter()
    asyncio.run(daemon.handle_confirm(writer1, confirm_id, True))

    # Replay with the same id — already popped, so this must be inert.
    writer2 = _CaptureWriter()
    asyncio.run(daemon.handle_confirm(writer2, confirm_id, True))

    assert calls["n"] == 1
    assert writer2.events[0] == {"type": "confirm_ack", "approved": True}
    assert len(writer2.events) == 1  # no tool_call/tool_result/token replay


def test_cloud_backend_tool_call_gets_identical_provenance_treatment(tmp_path, monkeypatch):
    """daemon.py's tool-execution/provenance logic operates only on the
    {"type": "tool_call", "tool": ..., "args": ...} event shape that BOTH
    stream_ollama and stream_claude yield (llm.stream_chat dispatches to
    stream_claude for any non-Ollama backend, same event shape either
    way) — it never branches on which backend produced the event. This
    test simulates a cloud-origin tool_call the same way a real one would
    arrive through stream_chat, and confirms identical deterministic
    composition to the local-backend test above."""
    _use_temp_db(tmp_path, monkeypatch)

    async def spy_stream_chat(messages, model_key, tools=None):
        # Same event shape stream_claude yields (v2/llm.py stream_claude,
        # "tool_call" event, cloud-origin call_id included but irrelevant
        # to daemon.py's handling).
        yield {"type": "tool_call", "tool": "remember", "args": {"key": "favorite_color", "value": "green"}, "call_id": "toolu_abc123"}
        yield {"type": "done"}

    async def fake_execute(record):
        record.state = ActionState.SUCCEEDED
        record.result = "remembered: favorite_color=green"
        return record

    monkeypatch.setattr(daemon, "stream_chat", spy_stream_chat)
    monkeypatch.setattr(daemon, "route_model", lambda *a, **kw: "cloud")
    monkeypatch.setattr(actions, "execute", fake_execute)

    writer = _CaptureWriter()
    asyncio.run(daemon._handle_chat_via_existing_route(writer, "Remember that my favorite color is green."))

    assert writer.full_text() == "Remembered: favorite_color is green."


def test_confirmation_binds_to_exact_proposed_args(tmp_path, monkeypatch):
    """The client can only approve/deny — it cannot supply new arguments
    at confirm time. Verify execute() runs with the args captured at
    proposal time."""
    _use_temp_db(tmp_path, monkeypatch)
    seen_args = {}

    async def fake_run_tool(name, args):
        seen_args.update(args)
        return "remembered: x=y"

    monkeypatch.setattr(actions, "run_tool", fake_run_tool)

    record = actions.propose("remember", {"key": "x", "value": "y"}, "confirm")
    confirm_id = record.action_id
    daemon._pending_confirms[confirm_id] = {
        "record": record, "writer": None, "tool_calls": [], "history": [], "model_key": "fast",
    }

    writer = _CaptureWriter()
    asyncio.run(daemon.handle_confirm(writer, confirm_id, True))

    assert seen_args == {"key": "x", "value": "y"}
