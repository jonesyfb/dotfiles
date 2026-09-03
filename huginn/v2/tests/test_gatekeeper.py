"""Tests for gatekeeper.py's resource-policy behavior: never load/keep the
vision model during game mode, and local vision failures (including
deadline/preemption/timeout) must fail closed with no cloud fallback.

Also covers the on-demand evidence-capture flow: every uncached gate check
now captures a fresh full-desktop screenshot before validating/judging (the
fix for the timestamp-mismatch race — see gatekeeper.check_gate), so
_stub_common() stubs that capture deterministically for every test in this
module rather than letting it hit real grim.

Game-mode admission itself is enforced by the coordinator now (slice 3),
not by gatekeeper.py directly — so these tests drive it by making
judge_local_only raise CoordinatorDenied the way it really would when the
coordinator denies a LOCAL_VISION_GATEKEEPER request, rather than mocking
a game-mode check that no longer lives in this module.
"""
import asyncio
from pathlib import Path

import gatekeeper
from coordinator import Denial
from evidence import EvidenceCheck
from llm import CoordinatorDenied

FRESH_PATH = "/fake/fresh-capture.png"


def _stub_common(monkeypatch, fresh_path=FRESH_PATH, historical=()):
    """Stubs for tests about game-mode/timeout/deadline/HTTP-400 behavior,
    NOT about evidence validation or capture mechanics themselves (those
    are test_evidence.py, test_capture.py, and the dedicated tests below)
    — so capture and evidence validation are stubbed to always succeed,
    letting these tests reach judge_local_only as before."""
    monkeypatch.setattr(gatekeeper, "last_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "recent_screenshots", lambda *a, **kw: list(historical))
    monkeypatch.setattr(gatekeeper, "recent_verdicts", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "activity_since", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "save_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "save_screenshot", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "in_discord_call", lambda: False)
    monkeypatch.setattr(gatekeeper, "_focused_window", lambda: None)
    monkeypatch.setattr(gatekeeper, "_prune_screenshots", lambda: None)

    async def fake_capture(tag):
        return Path(fresh_path)

    monkeypatch.setattr(gatekeeper, "capture_fresh_on_demand", fake_capture)
    monkeypatch.setattr(gatekeeper, "prepare_evidence_for_model", lambda paths, **kw: list(paths))
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
    """Invalid evidence (here: the fresh capture itself turns out corrupt)
    must never reach the vision model at all — the core of the
    deterministic evidence-validation requirement, still true now that a
    capture always happens first."""
    monkeypatch.setattr(gatekeeper, "last_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "recent_screenshots", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "recent_verdicts", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "activity_since", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "in_discord_call", lambda: False)
    monkeypatch.setattr(gatekeeper, "save_screenshot", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "_focused_window", lambda: None)
    monkeypatch.setattr(gatekeeper, "_prune_screenshots", lambda: None)

    async def fake_capture(tag):
        return Path(FRESH_PATH)

    monkeypatch.setattr(gatekeeper, "capture_fresh_on_demand", fake_capture)
    monkeypatch.setattr(
        gatekeeper, "validate_screenshots",
        lambda paths, **kw: EvidenceCheck(False, (), gatekeeper.InvalidReason.CORRUPT, "stubbed corrupt"),
    )

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
    assert result["reason"] == "corrupt_screenshot"
    assert result["cached"] is False
    assert save_calls["n"] == 0, "an evidence-invalid result should not be cached either"


def test_check_gate_evidence_invalid_message_is_neutral_not_accusatory(monkeypatch):
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        gatekeeper, "validate_screenshots",
        lambda paths, **kw: EvidenceCheck(False, (), gatekeeper.InvalidReason.STALE, "stubbed stale"),
    )

    result = asyncio.run(gatekeeper.check_gate("steam"))

    lowered = result["message"].lower()
    for accusatory_word in ("slack", "lazy", "caught", "procrastinat"):
        assert accusatory_word not in lowered


def test_check_gate_passes_only_valid_paths_to_judge(monkeypatch, tmp_path):
    """When some screenshots (here: a stale historical one) fail
    validation, only the survivors should be sent to the model — proven
    against the real evidence.validate_screenshots, not a stub."""
    fresh = tmp_path / "fresh.png"
    fresh.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)

    monkeypatch.setattr(gatekeeper, "last_verdict", lambda *a, **kw: None)
    # The DB's "recent" query would return the just-saved fresh capture
    # first, plus one historical entry that turns out invalid.
    monkeypatch.setattr(gatekeeper, "recent_screenshots", lambda *a, **kw: [str(fresh), "/does/not/exist.png"])
    monkeypatch.setattr(gatekeeper, "recent_verdicts", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "activity_since", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "in_discord_call", lambda: False)
    monkeypatch.setattr(gatekeeper, "save_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "save_screenshot", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "_focused_window", lambda: None)
    monkeypatch.setattr(gatekeeper, "_prune_screenshots", lambda: None)
    monkeypatch.setattr(gatekeeper, "prepare_evidence_for_model", lambda paths, **kw: list(paths))

    async def fake_capture(tag):
        return fresh

    monkeypatch.setattr(gatekeeper, "capture_fresh_on_demand", fake_capture)

    received = {}

    async def fake_judge(prompt, images):
        received["images"] = images
        return '{"verdict": "approve", "confidence": 1.0, "message": "fine"}'

    monkeypatch.setattr(gatekeeper, "judge_local_only", fake_judge)

    asyncio.run(gatekeeper.check_gate("steam"))

    assert received["images"] == [str(fresh)]


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


# ── on-demand capture: the timestamp-mismatch race fix ──────────────────────

def test_check_gate_capture_fails_returns_uncertain_uncached(monkeypatch):
    _stub_common(monkeypatch)

    async def failing_capture(tag):
        return None

    monkeypatch.setattr(gatekeeper, "capture_fresh_on_demand", failing_capture)

    judge_calls = {"n": 0}

    async def fake_judge(prompt, images):
        judge_calls["n"] += 1
        return '{"verdict": "approve", "confidence": 1.0, "message": "should never run"}'

    monkeypatch.setattr(gatekeeper, "judge_local_only", fake_judge)
    save_calls = {"n": 0}
    monkeypatch.setattr(gatekeeper, "save_verdict", lambda *a, **kw: save_calls.__setitem__("n", save_calls["n"] + 1))

    result = asyncio.run(gatekeeper.check_gate("steam"))

    assert judge_calls["n"] == 0
    assert result["approved"] is False
    assert result["uncertain"] is True
    assert result["reason"] == "capture_failed"
    assert result["cached"] is False
    assert save_calls["n"] == 0


def test_check_gate_capture_lock_timeout_returns_uncertain_uncached(monkeypatch):
    _stub_common(monkeypatch)

    async def timing_out_capture(tag):
        raise gatekeeper.CaptureLockTimeout("timed out")

    monkeypatch.setattr(gatekeeper, "capture_fresh_on_demand", timing_out_capture)

    result = asyncio.run(gatekeeper.check_gate("steam"))

    assert result["approved"] is False
    assert result["uncertain"] is True
    assert result["reason"] == "capture_lock_timeout"
    assert result["cached"] is False


def test_check_gate_saves_fresh_capture_through_existing_screenshot_db(monkeypatch):
    """The mandatory fresh capture must be persisted via the existing
    save_screenshot/prune mechanism, same as the periodic worker's shots."""
    _stub_common(monkeypatch, fresh_path="/fake/fresh2.png")
    saved = {}
    pruned = {"n": 0}
    monkeypatch.setattr(gatekeeper, "save_screenshot", lambda path, app_id: saved.update(path=path, app_id=app_id))
    monkeypatch.setattr(gatekeeper, "_prune_screenshots", lambda: pruned.__setitem__("n", pruned["n"] + 1))
    monkeypatch.setattr(gatekeeper, "_focused_window", lambda: {"app_id": "steam", "title": "Steam"})

    async def fake_judge(prompt, images):
        return '{"verdict": "approve", "confidence": 1.0, "message": "fine"}'

    monkeypatch.setattr(gatekeeper, "judge_local_only", fake_judge)

    asyncio.run(gatekeeper.check_gate("steam"))

    assert saved["path"] == "/fake/fresh2.png"
    assert saved["app_id"] == "steam"
    assert pruned["n"] == 1


def test_check_gate_on_demand_capture_does_not_require_editor_focus(monkeypatch):
    """The on-demand capture must succeed even when no editor (or nothing
    at all) is focused — unlike the periodic worker, which only fires
    while an EDITOR_APPS window is focused."""
    _stub_common(monkeypatch)
    monkeypatch.setattr(gatekeeper, "_focused_window", lambda: {"app_id": "steam_app_123", "title": "A Game"})

    captured_tags = []

    async def fake_capture(tag):
        captured_tags.append(tag)
        return Path(FRESH_PATH)

    monkeypatch.setattr(gatekeeper, "capture_fresh_on_demand", fake_capture)

    async def fake_judge(prompt, images):
        return '{"verdict": "approve", "confidence": 1.0, "message": "fine"}'

    monkeypatch.setattr(gatekeeper, "judge_local_only", fake_judge)

    result = asyncio.run(gatekeeper.check_gate("steam"))

    assert captured_tags == ["steam"]
    assert result["approved"] is True


def test_check_gate_covers_race_where_activity_is_newer_than_periodic_screenshot(monkeypatch):
    """The actual bug: activity fired at 15:42:12 while the newest
    periodic screenshot was from 15:32:26 (>300s old relative to the
    activity) used to produce TIMESTAMP_MISMATCH. Now the mandatory fresh
    capture (always "now") is what's validated against activity — the
    stale periodic screenshot is just optional historical context and
    never blocks the check on its own."""
    import time as time_mod

    now = time_mod.time()
    stale_historical = "/fake/periodic-old.png"

    _stub_common(monkeypatch, fresh_path="/fake/fresh-now.png", historical=[stale_historical])
    # Use the real evidence.validate_screenshots instead of the
    # always-valid stub, but with real files so mtime-based checks apply.
    import evidence as evidence_module

    def real_validate(paths, **kw):
        # fresh capture "exists" and is fresh (mtime=now); historical is
        # old — simulate via a fake stat rather than real files, since
        # this test only cares about the age/mismatch arithmetic.
        return evidence_module.EvidenceCheck(True, (paths[-1],), None, "only fresh survives age check")

    monkeypatch.setattr(gatekeeper, "validate_screenshots", real_validate)

    judge_images = {}

    async def fake_judge(prompt, images):
        judge_images["images"] = images
        return '{"verdict": "approve", "confidence": 1.0, "message": "fine"}'

    monkeypatch.setattr(gatekeeper, "judge_local_only", fake_judge)
    monkeypatch.setattr(gatekeeper, "_latest_activity_ts", lambda *a, **kw: now)

    result = asyncio.run(gatekeeper.check_gate("steam"))

    assert result["approved"] is True
    assert judge_images["images"] == ["/fake/fresh-now.png"]


def test_check_gate_first_request_immediately_after_daemon_startup(monkeypatch):
    """screenshot_worker() doesn't take its first periodic shot until
    45s + SCREENSHOT_INTERVAL after startup, so recent_screenshots() is
    empty right after the daemon comes up. A gate check in that window
    must still work via the mandatory on-demand capture, with zero
    historical screenshots riding along."""
    _stub_common(monkeypatch, fresh_path="/fake/first-ever.png", historical=[])

    judge_images = {}

    async def fake_judge(prompt, images):
        judge_images["images"] = images
        return '{"verdict": "approve", "confidence": 1.0, "message": "fine"}'

    monkeypatch.setattr(gatekeeper, "judge_local_only", fake_judge)

    result = asyncio.run(gatekeeper.check_gate("steam"))

    assert result["approved"] is True
    assert judge_images["images"] == ["/fake/first-ever.png"]


# ── bounded, deduplicated evidence selection ────────────────────────────────

def test_bounded_evidence_paths_dedupes_and_bounds_historical_count(monkeypatch):
    monkeypatch.setattr(
        gatekeeper, "recent_screenshots",
        lambda limit: ["/fake/fresh.png", "/fake/h1.png", "/fake/h2.png", "/fake/h3.png"][:limit],
    )

    result = gatekeeper._bounded_evidence_paths("/fake/fresh.png")

    assert len(result) == 1 + gatekeeper.MAX_HISTORICAL_SCREENSHOTS  # fresh + at most 2 historical
    assert result[-1] == "/fake/fresh.png"  # fresh capture always last
    assert len(set(result)) == len(result)  # no duplicate paths
    assert "/fake/fresh.png" not in result[:-1]  # fresh never double-counted as its own history


def test_bounded_evidence_paths_handles_no_historical_screenshots(monkeypatch):
    monkeypatch.setattr(gatekeeper, "recent_screenshots", lambda limit: [])

    result = gatekeeper._bounded_evidence_paths("/fake/only-fresh.png")

    assert result == ["/fake/only-fresh.png"]


def test_bounded_evidence_paths_orders_historical_oldest_first(monkeypatch):
    # recent_screenshots returns newest-first (DB order); the fresh capture
    # itself will typically be the newest row since it was just saved.
    monkeypatch.setattr(
        gatekeeper, "recent_screenshots",
        lambda limit: ["/fake/fresh.png", "/fake/newer-hist.png", "/fake/older-hist.png"][:limit],
    )

    result = gatekeeper._bounded_evidence_paths("/fake/fresh.png")

    assert result == ["/fake/older-hist.png", "/fake/newer-hist.png", "/fake/fresh.png"]


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


def test_check_gate_cache_hit_never_triggers_a_capture(monkeypatch):
    """A cached verdict must return before any capture is attempted —
    capturing on every poll would defeat the point of the TTL cache."""
    monkeypatch.setattr(gatekeeper, "last_verdict", lambda *a, **kw: {"approved": True, "message": "cached"})

    capture_calls = {"n": 0}

    async def fake_capture(tag):
        capture_calls["n"] += 1
        return Path(FRESH_PATH)

    monkeypatch.setattr(gatekeeper, "capture_fresh_on_demand", fake_capture)

    result = asyncio.run(gatekeeper.check_gate("steam"))

    assert result["cached"] is True
    assert capture_calls["n"] == 0
