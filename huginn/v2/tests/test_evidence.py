"""Tests for deterministic evidence validation (v2/evidence.py).

Covers: missing screenshot, corrupt/undecodable file, staleness beyond
threshold, empty/mixed-failure sets, timestamp mismatch against a recent
activity claim, and that a technically-valid-but-old-enough screenshot
still passes (staleness has a real threshold, not zero tolerance).
"""
import os
import time

from evidence import EvidenceCheck, InvalidReason, validate_screenshots

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _write_png(path, mtime=None):
    path.write_bytes(PNG_HEADER)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_missing_screenshot_no_paths_at_all():
    result = validate_screenshots([])
    assert result.valid is False
    assert result.reason == InvalidReason.MISSING
    assert result.valid_paths == ()


def test_missing_screenshot_path_does_not_exist(tmp_path):
    result = validate_screenshots([str(tmp_path / "nonexistent.png")])
    assert result.valid is False
    assert result.reason == InvalidReason.MISSING


def test_corrupt_screenshot_not_a_real_png(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not actually a png at all")
    result = validate_screenshots([str(bad)])
    assert result.valid is False
    assert result.reason == InvalidReason.CORRUPT


def test_stale_screenshot_beyond_threshold(tmp_path):
    old = tmp_path / "old.png"
    now = time.time()
    _write_png(old, mtime=now - 1000)
    result = validate_screenshots([str(old)], max_age_seconds=900, now=now)
    assert result.valid is False
    assert result.reason == InvalidReason.STALE


def test_screenshot_within_threshold_is_valid(tmp_path):
    fresh = tmp_path / "fresh.png"
    now = time.time()
    _write_png(fresh, mtime=now - 500)  # within the 900s default threshold
    result = validate_screenshots([str(fresh)], max_age_seconds=900, now=now)
    assert result.valid is True
    assert result.valid_paths == (str(fresh),)
    assert result.reason is None


def test_empty_set_after_filtering_mixed_failures(tmp_path):
    missing = str(tmp_path / "missing.png")
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"garbage")
    result = validate_screenshots([missing, str(corrupt)])
    assert result.valid is False
    assert result.reason == InvalidReason.EMPTY_SET  # mixed causes, not a single one
    assert result.valid_paths == ()


def test_single_cause_reason_when_all_failures_match(tmp_path):
    now = time.time()
    old1 = tmp_path / "old1.png"
    old2 = tmp_path / "old2.png"
    _write_png(old1, mtime=now - 2000)
    _write_png(old2, mtime=now - 3000)
    result = validate_screenshots([str(old1), str(old2)], max_age_seconds=900, now=now)
    assert result.valid is False
    assert result.reason == InvalidReason.STALE  # both failed the same way


def test_partial_survival_keeps_only_valid_paths(tmp_path):
    now = time.time()
    fresh = tmp_path / "fresh.png"
    stale = tmp_path / "stale.png"
    _write_png(fresh, mtime=now - 100)
    _write_png(stale, mtime=now - 2000)
    result = validate_screenshots([str(fresh), str(stale)], max_age_seconds=900, now=now)
    assert result.valid is True
    assert result.valid_paths == (str(fresh),)


def test_timestamp_mismatch_when_activity_recent_but_screenshot_lags(tmp_path):
    now = time.time()
    screenshot = tmp_path / "shot.png"
    _write_png(screenshot, mtime=now - 800)  # within staleness threshold alone
    # Activity log claims something happened 30s ago (very current), but the
    # only screenshot is 800s old — doesn't cover that claim.
    result = validate_screenshots(
        [str(screenshot)], max_age_seconds=900, now=now, latest_activity_ts=now - 30,
    )
    assert result.valid is False
    assert result.reason == InvalidReason.TIMESTAMP_MISMATCH
    assert result.valid_paths == (str(screenshot),)  # screenshot itself was individually fine


def test_no_mismatch_when_screenshot_covers_recent_activity(tmp_path):
    now = time.time()
    screenshot = tmp_path / "shot.png"
    _write_png(screenshot, mtime=now - 20)
    result = validate_screenshots(
        [str(screenshot)], max_age_seconds=900, now=now, latest_activity_ts=now - 30,
    )
    assert result.valid is True


def test_no_mismatch_check_when_activity_ts_not_provided(tmp_path):
    now = time.time()
    screenshot = tmp_path / "shot.png"
    _write_png(screenshot, mtime=now - 800)
    result = validate_screenshots([str(screenshot)], max_age_seconds=900, now=now)
    assert result.valid is True  # no latest_activity_ts given -> mismatch check skipped entirely


def test_result_is_frozen_dataclass():
    result = validate_screenshots([])
    assert isinstance(result, EvidenceCheck)
    try:
        result.valid = True
        mutated = True
    except Exception:
        mutated = False
    assert mutated is False
