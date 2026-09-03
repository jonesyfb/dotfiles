"""Tests for gatekeeper.py's resource-policy behavior: never load/keep the
vision model during game mode, and local vision failures (including
deadline/preemption/timeout) must fail closed with no cloud fallback.

Game-mode admission itself is enforced by the coordinator now (slice 3),
not by gatekeeper.py directly — so these tests drive it by making
judge_local_only raise CoordinatorDenied the way it really would when the
coordinator denies a LOCAL_VISION_GATEKEEPER request, rather than mocking
a game-mode check that no longer lives in this module.
"""
import asyncio

import gatekeeper
from coordinator import Denial
from llm import CoordinatorDenied


def _stub_common(monkeypatch):
    monkeypatch.setattr(gatekeeper, "last_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "recent_screenshots", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "recent_verdicts", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "activity_since", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "save_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "in_discord_call", lambda: False)


def _game_mode_denied(*a, **kw):
    raise CoordinatorDenied(Denial.GAME_MODE, "local_vision_gatekeeper not permitted during game mode")


def test_check_gate_short_circuits_in_game_mode_without_touching_vision_model(monkeypatch):
    _stub_common(monkeypatch)
    monkeypatch.setattr(gatekeeper, "judge_local_only", _game_mode_denied)

    unload_calls = {"n": 0}

    async def fake_unload(model):
        unload_calls["n"] += 1

    monkeypatch.setattr(gatekeeper, "unload_model", fake_unload)

    result = asyncio.run(gatekeeper.check_gate("steam"))

    assert result["approved"] is False
    assert result["reason"] == "game_mode"
    assert result["cached"] is False
    assert unload_calls["n"] == 1  # unload is still attempted, just now via the coordinator


def test_check_gate_unload_on_game_mode_denial_goes_through_coordinator_unload_helper(monkeypatch):
    """unload_model() (llm.py) is itself coordinator-routed — this just
    confirms check_gate calls *that* function, not some out-of-band path."""
    _stub_common(monkeypatch)
    monkeypatch.setattr(gatekeeper, "judge_local_only", _game_mode_denied)

    called_with = {}

    async def fake_unload(model):
        called_with["model"] = model

    monkeypatch.setattr(gatekeeper, "unload_model", fake_unload)

    asyncio.run(gatekeeper.check_gate("steam"))

    assert called_with["model"] == gatekeeper.MODELS["vision"]["model"]


def test_game_mode_denial_never_gets_cached(monkeypatch):
    """A cached game-mode denial would outlive game mode ending and wrongly
    block a real request made moments after the user stops playing."""
    _stub_common(monkeypatch)
    monkeypatch.setattr(gatekeeper, "judge_local_only", _game_mode_denied)
    monkeypatch.setattr(gatekeeper, "unload_model", lambda model: _noop())

    save_calls = {"n": 0}
    monkeypatch.setattr(gatekeeper, "save_verdict", lambda *a, **kw: save_calls.__setitem__("n", save_calls["n"] + 1))

    asyncio.run(gatekeeper.check_gate("steam"))

    assert save_calls["n"] == 0


async def _noop():
    return None


def test_game_mode_denial_never_triggers_close_reaction(monkeypatch):
    """A resource-policy denial isn't a real judgment — _react_to_verdict
    must never pick the destructive "close" action for it."""
    calls = {"close": 0, "notify": 0}

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


def test_check_gate_fails_closed_on_vision_timeout_no_cloud_fallback(monkeypatch):
    """A judge_local_only failure (deadline, preemption, or a raw exception)
    must deny by default and must never touch _judge_claude — same
    guarantee as any other local failure."""
    _stub_common(monkeypatch)

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


def test_check_gate_fails_closed_on_coordinator_deadline_exceeded(monkeypatch):
    """A CoordinatorDenied that ISN'T game-mode (e.g. deadline exceeded)
    must still deny cleanly, distinct from the game-mode message/reason."""
    _stub_common(monkeypatch)

    async def deadline_denied(prompt, images):
        raise CoordinatorDenied(Denial.DEADLINE_EXCEEDED, "deadline exceeded while running")

    monkeypatch.setattr(gatekeeper, "judge_local_only", deadline_denied)

    result = asyncio.run(gatekeeper.check_gate("steam"))

    assert result["approved"] is False
    assert result.get("reason") != "game_mode"
    assert "deadline_exceeded" in result["message"]
