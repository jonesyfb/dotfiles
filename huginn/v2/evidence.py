"""
Deterministic evidence validation for gatekeeper screenshots.

Moves "is this evidence trustworthy at all" out of model judgment and into
policy code. Audition finding (scripts/vision_bench/RECOMMENDATION.md):
every candidate model, including the provisional winner, confidently
guessed approve/deny on stale/missing/corrupt screenshots instead of
recognizing the evidence itself as unreliable — 0.0-0.6 uncertainty
calibration across the board. A screenshot that's missing, corrupt, too
old, or inconsistent with the activity log must never reach the vision
model; the caller gets a typed `uncertain` result with a machine-readable
reason instead, at zero inference cost.

Semantic ambiguity is explicitly NOT this module's job — a technically
valid, fresh, readable screenshot showing genuinely unclear activity is
still the model's call via its own "uncertain" verdict option. This module
only answers "is the evidence itself trustworthy", never "what does it
mean". Confidence alone must not manufacture certainty either way; see
gatekeeper.py's use of this module for how the two stay separated.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# Screenshots are taken every SCREENSHOT_INTERVAL (config.py, currently
# 600s) while an editor is focused — the freshest one can legitimately be
# nearly that old under normal operation. Default threshold gives a 5-
# minute buffer beyond that before calling it stale.
DEFAULT_MAX_AGE_SECONDS = 900


class InvalidReason(Enum):
    MISSING = "missing_screenshot"
    CORRUPT = "corrupt_screenshot"
    STALE = "stale_screenshot"
    EMPTY_SET = "empty_screenshot_set"
    TIMESTAMP_MISMATCH = "timestamp_mismatch"


@dataclass(frozen=True)
class EvidenceCheck:
    valid: bool
    valid_paths: tuple[str, ...]  # only paths that passed every check — safe to send to the model
    reason: "InvalidReason | None"
    detail: str  # machine-readable-ish diagnostic string, never sent to the model


def _is_valid_png(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(8) == b"\x89PNG\r\n\x1a\n"
    except Exception:
        return False


def validate_screenshots(
    paths: list[str],
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    now: "float | None" = None,
    latest_activity_ts: "float | None" = None,
) -> EvidenceCheck:
    """Checks, per file: exists -> decodable PNG -> not older than
    max_age_seconds. Then, if latest_activity_ts is given, checks that the
    freshest surviving screenshot isn't stale *relative to* a recent
    activity-log claim (evidence that doesn't reflect what the log says is
    happening right now). Returns the surviving valid paths (a possible
    subset of the input) and a single overall verdict — this function
    never raises and never touches the network or a model."""
    now = now if now is not None else time.time()

    if not paths:
        return EvidenceCheck(False, (), InvalidReason.MISSING, "no screenshot provided")

    survivors: list[str] = []
    reasons_seen: set[InvalidReason] = set()
    problems: list[str] = []
    for p in paths:
        path = Path(p)
        if not path.exists():
            reasons_seen.add(InvalidReason.MISSING)
            problems.append(f"{p}: does not exist")
            continue
        if not _is_valid_png(path):
            reasons_seen.add(InvalidReason.CORRUPT)
            problems.append(f"{p}: not a valid PNG (corrupt or wrong format)")
            continue
        try:
            age = now - path.stat().st_mtime
        except OSError as e:
            reasons_seen.add(InvalidReason.CORRUPT)
            problems.append(f"{p}: stat failed ({e})")
            continue
        if age > max_age_seconds:
            reasons_seen.add(InvalidReason.STALE)
            problems.append(f"{p}: {age:.0f}s old, exceeds {max_age_seconds}s threshold")
            continue
        survivors.append(p)

    if not survivors:
        reason = next(iter(reasons_seen)) if len(reasons_seen) == 1 else InvalidReason.EMPTY_SET
        return EvidenceCheck(False, (), reason, "; ".join(problems))

    if latest_activity_ts is not None:
        activity_recency = now - latest_activity_ts
        newest_screenshot_age = min(now - Path(p).stat().st_mtime for p in survivors)
        # Activity log claims something within the freshness window (i.e.
        # "current"), but the freshest surviving screenshot lags that claim
        # by more than a third of the staleness threshold — a smaller,
        # tighter margin than plain staleness, because this is about
        # covering a *specific recent claim*, not just "not too old".
        mismatch_margin = max_age_seconds / 3
        if activity_recency < max_age_seconds and (newest_screenshot_age - activity_recency) > mismatch_margin:
            return EvidenceCheck(
                False, tuple(survivors), InvalidReason.TIMESTAMP_MISMATCH,
                f"activity log claims something {activity_recency:.0f}s ago but the freshest screenshot "
                f"is {newest_screenshot_age:.0f}s old — evidence doesn't cover the claimed activity",
            )

    return EvidenceCheck(True, tuple(survivors), None, f"{len(survivors)}/{len(paths)} screenshot(s) valid")
