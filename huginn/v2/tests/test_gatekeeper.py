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
from evidence import EvidenceCheck
from llm import CoordinatorDenied


def _stub_common(monkeypatch):
    """Stubs for tests about game-mode/timeout/deadline behavior, NOT about
    evidence validation itself (that's test_evidence.py + the dedicated
    evidence-short-circuit tests below) — so evidence validation is stubbed
    to always pass, letting these tests reach judge_local_only as before."""
    monkeypatch.setattr(gatekeeper, "last_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "recent_screenshots", lambda *a, **kw: ["/fake/screenshot.png"])
    monkeypatch.setattr(gatekeeper, "recent_verdicts", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "activity_since", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "save_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "in_discord_call", lambda: False)
    monkeypatch.setattr(
        gatekeeper, "validate_screenshots",
        lambda paths, **kw: EvidenceCheck(True, tuple(paths), None, "stubbed valid"),
    )


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
    must deny-by-uncertain (never a confident accusation) and must never
    touch _judge_claude — same guarantee as any other local failure."""
    _stub_common(monkeypatch)

    cloud_calls = {"n": 0}
    save_calls = {"n": 0}
    monkeypatch.setattr(gatekeeper, "save_verdict", lambda *a, **kw: save_calls.__setitem__("n", save_calls["n"] + 1))

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
    assert result["uncertain"] is True
    assert result["reason"] == "judge_error"
    assert "couldn't verify" in result["message"].lower()
    lowered = result["message"].lower()
    for accusatory_word in ("slack", "lazy", "caught", "procrastinat", "judgment failed"):
        assert accusatory_word not in lowered
    assert cloud_calls["n"] == 0
    assert save_calls["n"] == 0, "a transient failure must not be cached as a denial"


def test_check_gate_evidence_invalid_short_circuits_before_judge(monkeypatch):
    """Invalid evidence must never reach the vision model at all — this is
    the core of the deterministic evidence-validation requirement."""
    monkeypatch.setattr(gatekeeper, "last_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "recent_screenshots", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "recent_verdicts", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "activity_since", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "in_discord_call", lambda: False)

    save_calls = {"n": 0}
    monkeypatch.setattr(gatekeeper, "save_verdict", lambda *a, **kw: save_calls.__setitem__("n", save_calls["n"] + 1))

    judge_calls = {"n": 0}

    async def fake_judge(prompt, images):
        judge_calls["n"] += 1
        return '{"verdict": "approve", "confidence": 1.0, "message": "should never run"}'

    monkeypatch.setattr(gatekeeper, "judge_local_only", fake_judge)

    result = asyncio.run(gatekeeper.check_gate("steam"))

    assert judge_calls["n"] == 0, "invalid evidence must never reach the vision model"
    assert result["approved"] is False
    assert result["uncertain"] is True
    assert result["reason"] == "missing_screenshot"
    assert result["cached"] is False
    assert save_calls["n"] == 0, "an evidence-invalid result should not be cached either"


def test_check_gate_evidence_invalid_message_is_neutral_not_accusatory(monkeypatch):
    monkeypatch.setattr(gatekeeper, "last_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "recent_screenshots", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "activity_since", lambda *a, **kw: [])

    result = asyncio.run(gatekeeper.check_gate("steam"))

    lowered = result["message"].lower()
    for accusatory_word in ("slack", "lazy", "caught", "procrastinat"):
        assert accusatory_word not in lowered


def test_check_gate_passes_only_valid_paths_to_judge(monkeypatch, tmp_path):
    """When some screenshots are valid and others aren't, only the
    survivors should be sent to the model."""
    good = tmp_path / "good.png"
    good.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    monkeypatch.setattr(gatekeeper, "last_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "recent_screenshots", lambda *a, **kw: [str(good), "/does/not/exist.png"])
    monkeypatch.setattr(gatekeeper, "recent_verdicts", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "activity_since", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "in_discord_call", lambda: False)
    monkeypatch.setattr(gatekeeper, "save_verdict", lambda *a, **kw: None)

    received = {}

    async def fake_judge(prompt, images):
        received["images"] = images
        return '{"verdict": "approve", "confidence": 1.0, "message": "fine"}'

    monkeypatch.setattr(gatekeeper, "judge_local_only", fake_judge)

    asyncio.run(gatekeeper.check_gate("steam"))

    assert received["images"] == [str(good)]


def test_react_to_verdict_treats_uncertain_as_notify_only_never_close(monkeypatch):
    calls = {"close": 0, "notify": 0}
    monkeypatch.setattr(gatekeeper, "_notify", lambda *a, **kw: calls.__setitem__("notify", calls["notify"] + 1))

    def fake_run(cmd, *a, **kw):
        if "close-window" in cmd:
            calls["close"] += 1

    monkeypatch.setattr(gatekeeper.subprocess, "run", fake_run)
    monkeypatch.setattr(gatekeeper.random, "choices", lambda *a, **kw: ["close"])

    verdict = {"approved": False, "uncertain": True, "message": "Can't verify.", "cached": False, "reason": "stale_screenshot"}
    asyncio.run(gatekeeper._react_to_verdict("youtube", verdict, {"id": 1}))

    assert calls["close"] == 0
    assert calls["notify"] == 1


def test_check_gate_fails_closed_on_coordinator_deadline_exceeded(monkeypatch):
    """A CoordinatorDenied that ISN'T game-mode (e.g. deadline exceeded)
    must still deny-by-uncertain cleanly, distinct from the game-mode
    message/reason, and the machine-readable reason (not the user-facing
    message) is where the specific denial type lives."""
    _stub_common(monkeypatch)
    save_calls = {"n": 0}
    monkeypatch.setattr(gatekeeper, "save_verdict", lambda *a, **kw: save_calls.__setitem__("n", save_calls["n"] + 1))

    async def deadline_denied(prompt, images):
        raise CoordinatorDenied(Denial.DEADLINE_EXCEEDED, "deadline exceeded while running")

    monkeypatch.setattr(gatekeeper, "judge_local_only", deadline_denied)

    result = asyncio.run(gatekeeper.check_gate("steam"))

    assert result["approved"] is False
    assert result["uncertain"] is True
    assert result["reason"] == "deadline_exceeded"
    assert result["reason"] != "game_mode"
    assert "couldn't verify" in result["message"].lower()
    assert save_calls["n"] == 0


# ── _parse_verdict: extended 3-way schema ─────────────────────────────────────

def test_parse_verdict_approve():
    result = gatekeeper._parse_verdict('{"verdict": "approve", "confidence": 0.9, "message": "earned it"}')
    assert result == {"approved": True, "uncertain": False, "message": "earned it"}


def test_parse_verdict_deny():
    result = gatekeeper._parse_verdict('{"verdict": "deny", "confidence": 0.9, "message": "not earned"}')
    assert result == {"approved": False, "uncertain": False, "message": "not earned"}


def test_parse_verdict_uncertain():
    result = gatekeeper._parse_verdict('{"verdict": "uncertain", "confidence": 0.3, "message": "unclear"}')
    assert result == {"approved": False, "uncertain": True, "message": "unclear"}


def test_parse_verdict_malformed_json_is_uncertain_not_a_denial():
    result = gatekeeper._parse_verdict("not json at all")
    assert result["approved"] is False
    assert result["uncertain"] is True


def test_parse_verdict_unrecognized_verdict_value_is_uncertain():
    result = gatekeeper._parse_verdict('{"verdict": "maybe", "confidence": 0.5, "message": "hedge"}')
    assert result["approved"] is False
    assert result["uncertain"] is True


def test_parse_verdict_missing_verdict_key_is_uncertain():
    result = gatekeeper._parse_verdict('{"confidence": 0.5, "message": "no verdict field"}')
    assert result["approved"] is False
    assert result["uncertain"] is True


def test_check_gate_caches_only_confident_verdicts_not_model_uncertain(monkeypatch):
    """A genuine model 'uncertain' verdict (ambiguous activity, not a system
    failure) also shouldn't be cached — re-evaluate rather than lock in a
    low-confidence read for the full TTL."""
    _stub_common(monkeypatch)
    save_calls = {"n": 0}
    monkeypatch.setattr(gatekeeper, "save_verdict", lambda *a, **kw: save_calls.__setitem__("n", save_calls["n"] + 1))

    async def uncertain_judge(prompt, images):
        return '{"verdict": "uncertain", "confidence": 0.3, "message": "unclear activity"}'

    monkeypatch.setattr(gatekeeper, "judge_local_only", uncertain_judge)

    result = asyncio.run(gatekeeper.check_gate("steam"))

    assert result["uncertain"] is True
    assert save_calls["n"] == 0


def test_check_gate_caches_a_confident_approve(monkeypatch):
    _stub_common(monkeypatch)
    save_calls = {"n": 0}
    monkeypatch.setattr(gatekeeper, "save_verdict", lambda *a, **kw: save_calls.__setitem__("n", save_calls["n"] + 1))

    async def confident_judge(prompt, images):
        return '{"verdict": "approve", "confidence": 0.9, "message": "earned it"}'

    monkeypatch.setattr(gatekeeper, "judge_local_only", confident_judge)

    result = asyncio.run(gatekeeper.check_gate("steam"))

    assert result["approved"] is True
    assert result["uncertain"] is False
    assert save_calls["n"] == 1
