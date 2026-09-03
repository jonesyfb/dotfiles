"""Tests for the conservative direct-social conversation path:
personality.render_direct_social() and daemon.handle_chat's pre-check
routing ahead of the existing route_model()/tool-calling loop.

All mocked at the llm.render_personality_only / personality.render_direct_social
boundary — no real Ollama call anywhere in this file.
"""
import asyncio

import personality
from personality import DirectSocialResult


def _use_temp_db(tmp_path, monkeypatch):
    import memory
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")


# ── render_direct_social: semantic guards ───────────────────────────────────

def test_render_direct_social_ok(monkeypatch):
    async def fake_render_personality_only(system_prompt, user_prompt, purpose, deadline_seconds):
        return "Morning. Sleep well, or did the machine keep you up again?"

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    result = asyncio.run(personality.render_direct_social("Morning."))
    assert result.ok is True
    assert "Sleep well" in result.text


def test_render_direct_social_rejects_action_claim(monkeypatch):
    responses = iter([
        "I already closed Brave for you.",   # claims an action it can't take
        "No idea, but if you say so.",
    ])

    async def fake_render_personality_only(*a, **kw):
        return next(responses)

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    result = asyncio.run(personality.render_direct_social("Close it."))
    assert result.ok is True
    assert "closed" not in result.text.lower()


def test_render_direct_social_rejects_fabricated_memory_claim(monkeypatch):
    async def always_fabricates(*a, **kw):
        return "You told me that last week, remember?"

    monkeypatch.setattr(personality, "render_personality_only", always_fabricates)

    result = asyncio.run(personality.render_direct_social("What was that restaurant again?"))
    assert result.ok is False
    assert result.reason == "validation_failed"


def test_render_direct_social_rejects_invented_desktop_fact(monkeypatch):
    async def always_invents(*a, **kw):
        return "Looking at your current window, it's a mess in there."

    monkeypatch.setattr(personality, "render_personality_only", always_invents)

    result = asyncio.run(personality.render_direct_social("How's it looking?"))
    assert result.ok is False


def test_render_direct_social_rejects_theatrical_and_self_prefix(monkeypatch):
    async def theatrical(*a, **kw):
        return "*ruffles feathers* Huginn: hello there."

    monkeypatch.setattr(personality, "render_personality_only", theatrical)

    result = asyncio.run(personality.render_direct_social("Hi."))
    assert result.ok is False


def test_render_direct_social_has_no_cloud_fallback_path():
    """Structural: render_direct_social calls render_personality_only,
    which has no code path to _judge_claude/stream_claude at all — same
    guarantee as the event-rendering path."""
    import inspect
    src = inspect.getsource(personality.render_direct_social)
    assert "claude" not in src.lower()
    assert "cloud" not in src.lower()


def test_render_direct_social_deterministic_denial_returns_not_ok(monkeypatch):
    from llm import CoordinatorDenied
    from coordinator import Denial

    async def denied(*a, **kw):
        raise CoordinatorDenied(Denial.GAME_MODE, "not admitted")

    monkeypatch.setattr(personality, "render_personality_only", denied)

    result = asyncio.run(personality.render_direct_social("Hey."))
    assert result.ok is False
    assert result.reason == "coordinator_denied:game_mode"


def test_render_direct_social_includes_entity_note_in_prompt(monkeypatch):
    captured = {}

    async def fake_render_personality_only(system_prompt, user_prompt, purpose, deadline_seconds):
        captured["user_prompt"] = user_prompt
        return "Brave's pride is fine, just hungry."

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    asyncio.run(personality.render_direct_social(
        "What do you think of Brave?", entity_note="Brave, archetype: lion",
    ))
    assert "archetype: lion" in captured["user_prompt"]


def test_render_direct_social_history_included_but_bounded(monkeypatch):
    captured = {}

    async def fake_render_personality_only(system_prompt, user_prompt, purpose, deadline_seconds):
        captured["user_prompt"] = user_prompt
        return "Sure, still here."

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}]
    asyncio.run(personality.render_direct_social("still there?", history=history))
    assert "hi" in captured["user_prompt"]


# ── daemon.handle_chat: routing pre-check ────────────────────────────────────

class _FakeWriter:
    def __init__(self):
        self.sent = []

    def write(self, data):
        self.sent.append(data)

    async def drain(self):
        pass


def _sent_types(writer):
    import json
    return [json.loads(line)["type"] for line in b"".join(writer.sent).decode().splitlines()]


def test_handle_chat_routes_high_confidence_social_to_direct_path(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    import daemon

    called = {"direct": False, "existing": False}

    async def fake_direct(writer, content):
        called["direct"] = True
        await daemon.send(writer, {"type": "token", "content": "Morning to you too."})
        await daemon.send(writer, {"type": "done"})

    async def fake_existing(writer, content, decision=None):
        called["existing"] = True
        await daemon.send(writer, {"type": "done"})

    monkeypatch.setattr(daemon, "handle_direct_social", fake_direct)
    monkeypatch.setattr(daemon, "_handle_chat_via_existing_route", fake_existing)

    writer = _FakeWriter()
    asyncio.run(daemon.handle_chat(writer, "Morning."))

    assert called["direct"] is True
    assert called["existing"] is False


def test_handle_chat_routes_tool_action_to_existing_route(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    import daemon

    called = {"direct": False, "existing": False}

    async def fake_direct(writer, content):
        called["direct"] = True

    async def fake_existing(writer, content, decision=None):
        called["existing"] = True
        await daemon.send(writer, {"type": "done"})

    monkeypatch.setattr(daemon, "handle_direct_social", fake_direct)
    monkeypatch.setattr(daemon, "_handle_chat_via_existing_route", fake_existing)

    writer = _FakeWriter()
    asyncio.run(daemon.handle_chat(writer, "Remember that my favorite color is green."))

    assert called["direct"] is False
    assert called["existing"] is True


def test_handle_chat_routes_ambiguous_to_existing_route(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    import daemon

    called = {"direct": False, "existing": False}

    async def fake_direct(writer, content):
        called["direct"] = True

    async def fake_existing(writer, content, decision=None):
        called["existing"] = True
        await daemon.send(writer, {"type": "done"})

    monkeypatch.setattr(daemon, "handle_direct_social", fake_direct)
    monkeypatch.setattr(daemon, "_handle_chat_via_existing_route", fake_existing)

    long_ambiguous = " ".join(["word"] * 40)
    writer = _FakeWriter()
    asyncio.run(daemon.handle_chat(writer, long_ambiguous))

    assert called["existing"] is True


def test_handle_direct_social_falls_back_to_existing_route_on_validation_failure(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    import daemon

    async def fake_render_direct_social(content, **kw):
        return DirectSocialResult(False, None, "validation_failed")

    called = {"existing": False}

    async def fake_existing(writer, content, decision=None):
        called["existing"] = True
        await daemon.send(writer, {"type": "done"})

    monkeypatch.setattr(personality, "render_direct_social", fake_render_direct_social)
    monkeypatch.setattr(daemon, "_handle_chat_via_existing_route", fake_existing)

    writer = _FakeWriter()
    asyncio.run(daemon.handle_direct_social(writer, "Morning."))

    assert called["existing"] is True


def test_handle_direct_social_never_calls_route_model_on_success(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    import daemon

    async def fake_render_direct_social(content, **kw):
        return DirectSocialResult(True, "Morning. You're up early.", "rendered")

    calls = {"route_model": 0}
    monkeypatch.setattr(personality, "render_direct_social", fake_render_direct_social)
    monkeypatch.setattr(daemon, "route_model", lambda *a, **kw: calls.__setitem__("route_model", calls["route_model"] + 1))

    writer = _FakeWriter()
    asyncio.run(daemon.handle_direct_social(writer, "Morning."))

    assert calls["route_model"] == 0


def test_game_mode_still_blocks_non_social_but_not_social(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    import daemon
    from pathlib import Path

    monkeypatch.setattr(daemon, "GAME_MODE_FLAG", tmp_path / "game-mode")
    Path(daemon.GAME_MODE_FLAG).touch()

    social_called = {"n": 0}
    existing_called = {"n": 0}

    async def fake_direct(writer, content):
        social_called["n"] += 1
        await daemon.send(writer, {"type": "done"})

    async def fake_existing(writer, content, decision=None):
        existing_called["n"] += 1
        await daemon.send(writer, {"type": "done"})

    monkeypatch.setattr(daemon, "handle_direct_social", fake_direct)
    monkeypatch.setattr(daemon, "_handle_chat_via_existing_route", fake_existing)

    writer = _FakeWriter()
    asyncio.run(daemon.handle_chat(writer, "Morning."))  # social, high confidence
    assert social_called["n"] == 1

    writer2 = _FakeWriter()
    asyncio.run(daemon.handle_chat(writer2, "Explain monads."))  # factual, blocked by game mode
    assert existing_called["n"] == 0
    assert b"Game mode" in b"".join(writer2.sent)
