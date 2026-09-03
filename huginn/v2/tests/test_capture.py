"""Tests for capture.py: the shared grim-capture primitive used by both
the periodic screenshot_worker and check_gate's mandatory on-demand
capture, plus the local-resize step that keeps evidence inside the vision
model's context window.
"""
import asyncio
import subprocess
import time
from pathlib import Path

import capture

PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def _write_valid_png(path: Path, size=(40, 30)):
    from PIL import Image
    Image.new("RGB", size, color=(10, 20, 30)).save(path, format="PNG")


class _FakeCompletedProcess:
    def __init__(self, returncode):
        self.returncode = returncode


# ── capture_screenshot ──────────────────────────────────────────────────────

def test_capture_screenshot_success(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "SCREENS_DIR", tmp_path)

    def fake_run(cmd, **kw):
        out_path = Path(cmd[-1])
        _write_valid_png(out_path)
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(capture.subprocess, "run", fake_run)

    result = asyncio.run(capture.capture_screenshot("test"))

    assert result is not None
    assert result.exists()
    assert capture.is_valid_png(result)


def test_capture_screenshot_grim_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "SCREENS_DIR", tmp_path)

    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 10)

    monkeypatch.setattr(capture.subprocess, "run", fake_run)

    result = asyncio.run(capture.capture_screenshot("test"))
    assert result is None


def test_capture_screenshot_nonzero_exit_with_stale_output_file_is_rejected(monkeypatch, tmp_path):
    """A nonzero grim exit must be treated as failure even if it happened
    to leave a file behind at the target path — existence alone must never
    be mistaken for success."""
    monkeypatch.setattr(capture, "SCREENS_DIR", tmp_path)

    def fake_run(cmd, **kw):
        out_path = Path(cmd[-1])
        _write_valid_png(out_path)  # a real, valid PNG — but grim "failed" anyway
        return _FakeCompletedProcess(1)

    monkeypatch.setattr(capture.subprocess, "run", fake_run)

    result = asyncio.run(capture.capture_screenshot("test"))

    assert result is None
    # the accidental output must be cleaned up, not left as phantom evidence
    assert list(tmp_path.glob("*.png")) == []


def test_capture_screenshot_missing_output_file(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "SCREENS_DIR", tmp_path)
    monkeypatch.setattr(capture.subprocess, "run", lambda cmd, **kw: _FakeCompletedProcess(0))

    result = asyncio.run(capture.capture_screenshot("test"))
    assert result is None


def test_capture_screenshot_invalid_png_despite_success_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "SCREENS_DIR", tmp_path)

    def fake_run(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"not a png")
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(capture.subprocess, "run", fake_run)

    result = asyncio.run(capture.capture_screenshot("test"))
    assert result is None
    assert list(tmp_path.glob("*.png")) == []


def test_capture_screenshot_filenames_are_collision_resistant(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "SCREENS_DIR", tmp_path)

    def fake_run(cmd, **kw):
        _write_valid_png(Path(cmd[-1]))
        return _FakeCompletedProcess(0)

    monkeypatch.setattr(capture.subprocess, "run", fake_run)

    async def two_captures():
        return await asyncio.gather(
            capture.capture_screenshot("a"), capture.capture_screenshot("a")
        )

    p1, p2 = asyncio.run(two_captures())
    assert p1 != p2
    assert p1.name != p2.name


# ── capture_fresh_on_demand: single-flight + lock timeout ──────────────────

def test_capture_fresh_on_demand_coalesces_concurrent_callers(monkeypatch, tmp_path):
    """Two gate checks racing at the same moment must share ONE grim
    invocation, not each kick off their own."""
    monkeypatch.setattr(capture, "SCREENS_DIR", tmp_path)
    calls = {"n": 0}

    async def fake_capture_screenshot(tag):
        calls["n"] += 1
        await asyncio.sleep(0.05)
        p = tmp_path / "shared.png"
        _write_valid_png(p)
        return p

    monkeypatch.setattr(capture, "capture_screenshot", fake_capture_screenshot)

    async def scenario():
        return await asyncio.gather(
            capture.capture_fresh_on_demand("steam"),
            capture.capture_fresh_on_demand("youtube"),
        )

    r1, r2 = asyncio.run(scenario())
    assert calls["n"] == 1
    assert r1 == r2


def test_capture_fresh_on_demand_raises_lock_timeout(monkeypatch):
    monkeypatch.setattr(capture, "CAPTURE_LOCK_TIMEOUT_SECONDS", 0.05)

    async def scenario():
        # Hold the single-flight lock ourselves so the real call can never
        # acquire it within the (shrunk) timeout.
        await capture._capture_task_lock.acquire()
        try:
            await capture.capture_fresh_on_demand("steam")
        finally:
            capture._capture_task_lock.release()

    raised = False
    try:
        asyncio.run(scenario())
    except capture.CaptureLockTimeout:
        raised = True
    assert raised


def test_capture_fresh_on_demand_starts_new_task_after_previous_completes(monkeypatch, tmp_path):
    monkeypatch.setattr(capture, "SCREENS_DIR", tmp_path)
    calls = {"n": 0}

    async def fake_capture_screenshot(tag):
        calls["n"] += 1
        p = tmp_path / f"shot{calls['n']}.png"
        _write_valid_png(p)
        return p

    monkeypatch.setattr(capture, "capture_screenshot", fake_capture_screenshot)

    async def scenario():
        first = await capture.capture_fresh_on_demand("steam")
        second = await capture.capture_fresh_on_demand("steam")
        return first, second

    first, second = asyncio.run(scenario())
    assert calls["n"] == 2
    assert first != second


# ── ensure_resized / prepare_evidence_for_model ─────────────────────────────

def test_ensure_resized_preserves_aspect_ratio_and_never_touches_original(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "RESIZED_DIR", tmp_path / "resized")
    original = tmp_path / "orig.png"
    _write_valid_png(original, size=(2000, 1000))
    original_bytes = original.read_bytes()

    out = capture.ensure_resized(original, max_dim=500)

    from PIL import Image
    with Image.open(out) as im:
        w, h = im.size
    assert max(w, h) == 500
    assert abs(w / h - 2000 / 1000) < 0.01
    assert original.read_bytes() == original_bytes  # untouched
    assert out.parent == tmp_path / "resized"  # derived file stays in the private screens area


def test_ensure_resized_is_cached_when_original_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "RESIZED_DIR", tmp_path / "resized")
    original = tmp_path / "orig.png"
    _write_valid_png(original, size=(800, 600))

    out1 = capture.ensure_resized(original, max_dim=400)
    mtime1 = out1.stat().st_mtime
    time.sleep(0.01)
    out2 = capture.ensure_resized(original, max_dim=400)

    assert out1 == out2
    assert out2.stat().st_mtime == mtime1  # not regenerated


def test_ensure_resized_skips_upscaling_smaller_originals(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "RESIZED_DIR", tmp_path / "resized")
    original = tmp_path / "small.png"
    _write_valid_png(original, size=(100, 80))

    out = capture.ensure_resized(original, max_dim=1024)

    from PIL import Image
    with Image.open(out) as im:
        assert im.size == (100, 80)


def test_prepare_evidence_for_model_skips_failures_without_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "RESIZED_DIR", tmp_path / "resized")
    good = tmp_path / "good.png"
    _write_valid_png(good)

    result = capture.prepare_evidence_for_model([str(good), "/does/not/exist.png"])

    assert len(result) == 1
    assert Path(result[0]).exists()
