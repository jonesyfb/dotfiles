"""Integration tests for the user-visible-output boundary added on top of
action provenance: for TOOL_OR_ACTION/AMBIGUOUS requests, model prose is
buffered until the outcome is structurally known, so a fabricated tool
transcript is never visible even once — not "shown then corrected".

Every test here records the COMPLETE sequence of socket events a mock
client would receive and asserts the entire sequence is truthful at every
point, not just the final stored string. This is the acceptance test for:
"the fabricated transcript is never visible — not that it is corrected
afterward."
"""
import asyncio
import json

import actions
import daemon
import intent
import memory
import personality
from actions import ActionState


def _use_temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")


class _RecordingWriter:
    """Captures every event exactly as the real client would receive it,
    in order, with full content."""

    def __init__(self, fail_on_write_number=None, fail_with=ConnectionResetError):
        self.events = []
        self._write_count = 0
        self._fail_on = fail_on_write_number
        self._fail_with = fail_with

    def write(self, data):
        self._write_count += 1
        if self._fail_on is not None and self._write_count == self._fail_on:
            raise self._fail_with("client gone")
        for line in data.decode().splitlines():
            if line.strip():
                self.events.append(json.loads(line))

    async def drain(self):
        pass

    def tokens(self):
        return [e["content"] for e in self.events if e["type"] == "token"]

    def joined_text(self):
        return "".join(self.tokens())

    def types(self):
        return [e["type"] for e in self.events]


_SUSPICIOUS_SUBSTRINGS = (
    "[tool", "assistant to=", "<tool_call>", "<function_call>",
    "done.", "created", "saved", "remembered:", "sent.", "queued:",
)


def _assert_no_premature_success_language(writer, allowed_final_text):
    """No token event before the final authoritative one may contain
    success/creation language or tool-transcript-shaped markup. The one
    exception is the final, authoritative token itself (which is allowed
    to legitimately say "Remembered: ..." because it IS the real receipt)."""
    tokens = writer.tokens()
    for i, tok in enumerate(tokens):
        is_final_authoritative = (i == len(tokens) - 1 and tok == allowed_final_text)
        if is_final_authoritative:
            continue
        low = tok.lower()
        for bad in _SUSPICIOUS_SUBSTRINGS:
            assert bad not in low, f"premature success/markup language in token[{i}]={tok!r}"


# ── Fake transcript scenarios: must never be visible, not even once ────────

def test_fake_touch_transcript_never_visible(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)

    async def spy_stream_chat(messages, model_key, tools=None):
        yield {"type": "thinking", "content": "I'll just run this directly."}
        yield {"type": "token", "content": "[tool]\n```\ntouch /tmp/x\n```\n\nDone. Marker created."}
        yield {"type": "done"}

    monkeypatch.setattr(daemon, "stream_chat", spy_stream_chat)

    writer = _RecordingWriter()
    asyncio.run(daemon._handle_chat_via_existing_route(
        writer, "Use the shell tool to run: touch /tmp/x",
        intent.classify("Use the shell tool to run: touch /tmp/x"),
    ))

    # The fabricated transcript must not appear as ANY token, at any point.
    for tok in writer.tokens():
        assert "[tool]" not in tok
        assert "Marker created" not in tok
    assert writer.joined_text() == "No command ran — tell me exactly what you'd like done."
    # Only one token event total — buffered, not streamed-then-corrected.
    assert writer.types().count("token") == 1


def test_prose_claiming_file_created_without_markup_also_suppressed(tmp_path, monkeypatch):
    """Proves suppression does not depend on recognizing tool-transcript
    SYNTAX — plain prose making a false claim, with no markup at all, is
    suppressed the same way, because nothing about the prose is
    inspected in the buffered path."""
    _use_temp_db(tmp_path, monkeypatch)

    async def spy_stream_chat(messages, model_key, tools=None):
        yield {"type": "token", "content": "Sure thing! I went ahead and created that file for you just now."}
        yield {"type": "done"}

    monkeypatch.setattr(daemon, "stream_chat", spy_stream_chat)

    content = "Please run a command to create a marker file for me."
    writer = _RecordingWriter()
    asyncio.run(daemon._handle_chat_via_existing_route(writer, content, intent.classify(content)))

    for tok in writer.tokens():
        assert "created that file" not in tok
    assert writer.joined_text() == "No command ran — tell me exactly what you'd like done."
    assert writer.types().count("token") == 1


def test_ambiguous_close_it_gets_clarification_not_model_prose(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)

    async def spy_stream_chat(messages, model_key, tools=None):
        yield {"type": "token", "content": "Closing your browser now, all done!"}
        yield {"type": "done"}

    monkeypatch.setattr(daemon, "stream_chat", spy_stream_chat)

    writer = _RecordingWriter()
    asyncio.run(daemon._handle_chat_via_existing_route(writer, "Close it.", intent.classify("Close it.")))

    assert "Closing your browser" not in writer.joined_text()
    assert writer.joined_text() == "Which one did you mean?"


# ── Real, successful actions: exactly one truthful token, nothing before ───

def test_real_memory_write_stream_is_truthful_at_every_point(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)

    async def spy_stream_chat(messages, model_key, tools=None):
        yield {"type": "thinking", "content": "I'll remember that."}
        yield {"type": "tool_call", "tool": "remember", "args": {"key": "favorite_color", "value": "green"}}
        yield {"type": "done"}

    async def fake_execute(record):
        record.state = ActionState.SUCCEEDED
        record.result = "remembered: favorite_color=green"
        return record

    monkeypatch.setattr(daemon, "stream_chat", spy_stream_chat)
    monkeypatch.setattr(actions, "execute", fake_execute)

    content = "Remember that my favorite color is green."
    writer = _RecordingWriter()
    asyncio.run(daemon._handle_chat_via_existing_route(writer, content, intent.classify(content)))

    expected = "Remembered: favorite_color is green."
    _assert_no_premature_success_language(writer, expected)
    assert writer.joined_text() == expected
    assert writer.types().count("token") == 1
    # Structural events (tool_call/tool_result) are allowed and expected —
    # they're the authoritative channel, not unvalidated prose.
    assert "tool_call" in writer.types()
    assert "tool_result" in writer.types()


def test_tool_failure_stream_states_failure_only(tmp_path, monkeypatch):
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

    content = "Remember that x is y."
    writer = _RecordingWriter()
    asyncio.run(daemon._handle_chat_via_existing_route(writer, content, intent.classify(content)))

    expected = "That failed: disk full."
    assert writer.joined_text() == expected
    assert writer.types().count("token") == 1
    for tok in writer.tokens()[:-1]:
        assert "remembered" not in tok.lower()


def test_calendar_unavailable_stream_has_no_model_events_at_all(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    calls = {"n": 0}

    async def spy_stream_chat(*a, **kw):
        calls["n"] += 1
        return
        yield  # pragma: no cover

    monkeypatch.setattr(daemon, "stream_chat", spy_stream_chat)

    content = "Add milk to my calendar tomorrow."
    writer = _RecordingWriter()
    asyncio.run(daemon._handle_chat_via_existing_route(writer, content, intent.classify(content)))

    assert calls["n"] == 0  # never even reaches the model
    assert writer.types() == ["token", "done"]
    assert writer.joined_text() == "The calendar is unavailable, so I didn't add it."


def test_denied_confirmation_stream_is_truthful(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    record = actions.propose("shell", {"command": "rm -rf build/"}, "confirm")
    confirm_id = record.action_id
    daemon._pending_confirms[confirm_id] = {
        "record": record, "writer": None, "tool_calls": [], "history": [], "model_key": "fast",
    }

    writer = _RecordingWriter()
    asyncio.run(daemon.handle_confirm(writer, confirm_id, False))

    assert writer.types() == ["confirm_ack", "token", "done"]
    assert writer.joined_text() == "I left it unchanged."


def test_duplicate_confirmation_second_call_gets_no_extra_events(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)

    async def fake_run_tool(name, args):
        return "remembered: x=y"

    monkeypatch.setattr(actions, "run_tool", fake_run_tool)

    record = actions.propose("remember", {"key": "x", "value": "y"}, "confirm")
    confirm_id = record.action_id
    daemon._pending_confirms[confirm_id] = {
        "record": record, "writer": None, "tool_calls": [], "history": [], "model_key": "fast",
    }

    writer1 = _RecordingWriter()
    asyncio.run(daemon.handle_confirm(writer1, confirm_id, True))
    assert writer1.joined_text() == "Remembered: x is y."

    writer2 = _RecordingWriter()
    asyncio.run(daemon.handle_confirm(writer2, confirm_id, True))
    assert writer2.types() == ["confirm_ack"]  # nothing else — no replay, no re-execution


def test_malformed_hallucinated_tool_call_never_produces_success_language(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    record = actions.propose("add_calendar_event", {"what": "milk"}, "confirm")
    confirm_id = record.action_id
    daemon._pending_confirms[confirm_id] = {
        "record": record, "writer": None, "tool_calls": [], "history": [], "model_key": "fast",
    }

    writer = _RecordingWriter()
    asyncio.run(daemon.handle_confirm(writer, confirm_id, True))

    assert writer.joined_text() == "I don't have a way to do that."
    for tok in writer.tokens():
        assert "milk" not in tok.lower()  # no fabricated confirmation of the specific request either


def test_action_request_with_no_tool_call_sends_exactly_one_token(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)

    async def spy_stream_chat(messages, model_key, tools=None):
        yield {"type": "thinking", "content": "Hmm, I don't think I can do this."}
        yield {"type": "token", "content": "I went ahead and handled it, all set."}
        yield {"type": "done"}

    monkeypatch.setattr(daemon, "stream_chat", spy_stream_chat)

    content = "Please queue a task to run: rm -rf /tmp/whatever"
    writer = _RecordingWriter()
    asyncio.run(daemon._handle_chat_via_existing_route(writer, content, intent.classify(content)))

    assert writer.types().count("token") == 1
    assert "handled it" not in writer.joined_text()


# ── Client disconnect during buffered execution ─────────────────────────────

def test_client_disconnect_during_buffered_execution_does_not_crash_and_action_still_completes(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    executed = {"n": 0}

    async def spy_stream_chat(messages, model_key, tools=None):
        yield {"type": "tool_call", "tool": "remember", "args": {"key": "x", "value": "y"}}
        yield {"type": "done"}

    async def fake_execute(record):
        executed["n"] += 1
        record.state = ActionState.SUCCEEDED
        record.result = "remembered: x=y"
        return record

    monkeypatch.setattr(daemon, "stream_chat", spy_stream_chat)
    monkeypatch.setattr(actions, "execute", fake_execute)

    # Fail on the first write attempt after tool_call/tool_result (the
    # final deterministic token) — simulates the client vanishing right
    # as the buffered outcome is about to be delivered.
    writer = _RecordingWriter(fail_on_write_number=3)

    content = "Remember that x is y."
    # Must not raise — send() swallows the connection error.
    asyncio.run(daemon._handle_chat_via_existing_route(writer, content, intent.classify(content)))

    assert executed["n"] == 1  # the action still ran to completion
    history = memory.get_history(limit=5)
    assert any(t["role"] == "assistant" and "Remembered: x is y." in str(t["content"]) for t in history)


# ── Ordinary reasoning keeps live streaming ──────────────────────────────────

def test_factual_reasoning_still_streams_live_multiple_tokens(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)

    async def spy_stream_chat(messages, model_key, tools=None):
        for word in ["Monads", " are", " a", " pattern."]:
            yield {"type": "token", "content": word}
        yield {"type": "done"}

    monkeypatch.setattr(daemon, "stream_chat", spy_stream_chat)

    content = "Explain monads."
    writer = _RecordingWriter()
    asyncio.run(daemon._handle_chat_via_existing_route(writer, content, intent.classify(content)))

    # Streamed live: multiple separate token events, not buffered into one.
    assert writer.types().count("token") == 4
    assert writer.joined_text() == "Monads are a pattern."
