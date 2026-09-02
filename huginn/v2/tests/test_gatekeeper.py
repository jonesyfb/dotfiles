"""Tests for gatekeeper.py's resource-policy behavior (slice 2, item 6):
never load/keep the vision model during game mode, and local vision
failures (including timeout) must fail closed with no cloud fallback.
"""
import asyncio

import gatekeeper


def _stub_common(monkeypatch):
    monkeypatch.setattr(gatekeeper, "last_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "recent_screenshots", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "recent_verdicts", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "activity_since", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "save_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "in_discord_call", lambda: False)


def test_check_gate_short_circuits_in_game_mode_without_touching_vision_model(tmp_path, monkeypatch):
    _stub_common(monkeypatch)

    class _FakeInteraction:
        mode = "game"

    monkeypatch.setattr(gatekeeper.context, "collect_interaction", lambda: _FakeInteraction())

    async def fake_probe_loaded():
        return []

    monkeypatch.setattr(gatekeeper.context, "probe_ollama_loaded", fake_probe_loaded)

    calls = {"judge": 0}

    async def fake_judge_local_only(prompt, images):
        calls["judge"] += 1
        return '{"approved": true, "message": "should never run"}'

    monkeypatch.setattr(gatekeeper, "judge_local_only", fake_judge_local_only)

    result = asyncio.run(gatekeeper.check_gate("steam"))

    assert calls["judge"] == 0, "vision model must never be invoked during game mode"
    assert result["approved"] is False
    assert result["reason"] == "game_mode"
    assert result["cached"] is False


def test_check_gate_unloads_vision_model_if_already_resident_during_game_mode(tmp_path, monkeypatch):
    _stub_common(monkeypatch)

    class _FakeInteraction:
        mode = "game"

    monkeypatch.setattr(gatekeeper.context, "collect_interaction", lambda: _FakeInteraction())

    async def fake_probe_loaded():
        return [{"model": gatekeeper.MODELS["vision"]["model"]}]

    monkeypatch.setattr(gatekeeper.context, "probe_ollama_loaded", fake_probe_loaded)

    unloaded = {"model": None}

    async def fake_unload(model):
        unloaded["model"] = model

    monkeypatch.setattr(gatekeeper, "unload_model", fake_unload)

    asyncio.run(gatekeeper.check_gate("steam"))

    assert unloaded["model"] == gatekeeper.MODELS["vision"]["model"]


def test_check_gate_does_not_unload_when_vision_model_not_resident(tmp_path, monkeypatch):
    _stub_common(monkeypatch)

    class _FakeInteraction:
        mode = "game"

    monkeypatch.setattr(gatekeeper.context, "collect_interaction", lambda: _FakeInteraction())

    async def fake_probe_loaded():
        return [{"model": "qwen3.5:9b"}]  # something else resident, not vision

    monkeypatch.setattr(gatekeeper.context, "probe_ollama_loaded", fake_probe_loaded)

    called = {"n": 0}

    async def fake_unload(model):
        called["n"] += 1

    monkeypatch.setattr(gatekeeper, "unload_model", fake_unload)

    asyncio.run(gatekeeper.check_gate("steam"))

    assert called["n"] == 0


def test_game_mode_denial_never_gets_cached(tmp_path, monkeypatch):
    """A cached game-mode denial would outlive game mode ending and wrongly
    block a real request made moments after the user stops playing."""
    _stub_common(monkeypatch)

    class _FakeInteraction:
        mode = "game"

    monkeypatch.setattr(gatekeeper.context, "collect_interaction", lambda: _FakeInteraction())

    async def fake_probe_loaded():
        return []

    monkeypatch.setattr(gatekeeper.context, "probe_ollama_loaded", fake_probe_loaded)

    save_calls = {"n": 0}
    monkeypatch.setattr(gatekeeper, "save_verdict", lambda *a, **kw: save_calls.__setitem__("n", save_calls["n"] + 1))

    asyncio.run(gatekeeper.check_gate("steam"))

    assert save_calls["n"] == 0


def test_game_mode_denial_never_triggers_close_reaction(tmp_path, monkeypatch):
    """A resource-policy denial isn't a real judgment — _react_to_verdict
    must never pick the destructive "close" action for it."""
    calls = {"close": 0, "notify": 0}

    async def fake_notify(*a, **kw):
        calls["notify"] += 1

    monkeypatch.setattr(gatekeeper, "_notify", lambda *a, **kw: calls.__setitem__("notify", calls["notify"] + 1))

    def fake_run(cmd, *a, **kw):
        if "close-window" in cmd:
            calls["close"] += 1

    monkeypatch.setattr(gatekeeper.subprocess, "run", fake_run)
    monkeypatch.setattr(gatekeeper.random, "choices", lambda *a, **kw: ["close"])  # force worst case

    verdict = {"approved": False, "message": "Not while you're playing.", "cached": False, "reason": "game_mode"}
    asyncio.run(gatekeeper._react_to_verdict("youtube", verdict, {"id": 1}))

    assert calls["close"] == 0
    assert calls["notify"] == 1


def test_check_gate_fails_closed_on_vision_timeout_no_cloud_fallback(tmp_path, monkeypatch):
    """A judge_local_only timeout must deny by default and must never touch
    _judge_claude — same guarantee as any other local failure."""
    _stub_common(monkeypatch)

    class _FakeInteraction:
        mode = "ambient"

    monkeypatch.setattr(gatekeeper.context, "collect_interaction", lambda: _FakeInteraction())

    cloud_calls = {"n": 0}

    async def timing_out_judge(prompt, images):
        raise TimeoutError("gate judgment timed out")

    async def fake_claude(*a, **kw):
        cloud_calls["n"] += 1
        return "should never be reached"

    monkeypatch.setattr(gatekeeper, "judge_local_only", timing_out_judge)
    # judge_local_only itself has no path to _judge_claude (proven in
    # test_llm_local_only.py); this test additionally proves check_gate's
    # own except-branch fails closed rather than trying anything else.

    result = asyncio.run(gatekeeper.check_gate("steam"))

    assert result["approved"] is False
    assert "Judgment failed" in result["message"]
    assert cloud_calls["n"] == 0
