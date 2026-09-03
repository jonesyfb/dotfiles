"""
Huginn gatekeeper: watches window focus, takes evidence screenshots, and asks
Huginn to judge whether gated apps (Steam, recreational YouTube) are earned.
"""
import asyncio
import json
import random
import re
import subprocess
import time

from config import (
    ACTIVITY_POLL, BROWSER_APPS, EDITOR_APPS, GATE_PROMPT, GATE_TTL_SECONDS,
    MODELS, SCREENS_DIR, SCREENSHOT_INTERVAL, SCREENSHOT_KEEP,
    STEAM_BYPASS_GRACE_SECONDS, YOUTUBE_GRACE_SECONDS,
)
from context import focused_window as _focused_window, in_discord_call
from coordinator import Denial
from evidence import InvalidReason, validate_screenshots
from llm import CoordinatorDenied, judge_local_only, unload_model
from memory import (
    activity_since, last_verdict, log_activity, prune_activity,
    recent_screenshots, recent_verdicts, save_screenshot, save_verdict,
)

_YOUTUBE_RE = re.compile(r"-\s*YouTube\s*-", re.I)
_STEAM_APP_RE = re.compile(r"^steam(_app_\d+)?$")
_NOTIFY = "/home/nate/.local/bin/huginn-notify"


async def activity_tracker_worker() -> None:
    """Poll the focused window every ACTIVITY_POLL seconds, log it, and watch
    for sustained recreational-YouTube or ungated-Steam focus to trigger a
    gate check + reaction."""
    await asyncio.sleep(30)
    youtube_since: float | None = None
    steam_since: float | None = None
    while True:
        await asyncio.sleep(ACTIVITY_POLL)
        win = await asyncio.to_thread(_focused_window)
        if win is None:
            youtube_since = None
            steam_since = None
            continue

        app_id, title = win.get("app_id", ""), win.get("title", "")
        await asyncio.to_thread(log_activity, app_id, title)
        await asyncio.to_thread(prune_activity)

        is_youtube = app_id in BROWSER_APPS and bool(_YOUTUBE_RE.search(title))
        if is_youtube:
            if youtube_since is None:
                youtube_since = time.monotonic()
            elif time.monotonic() - youtube_since >= YOUTUBE_GRACE_SECONDS:
                verdict = await check_gate("youtube")
                await _react_to_verdict("youtube", verdict, win)
                youtube_since = time.monotonic()  # re-arm grace period, don't spam
        else:
            youtube_since = None

        is_steam = bool(_STEAM_APP_RE.match(app_id))
        if is_steam:
            if steam_since is None:
                steam_since = time.monotonic()
            elif time.monotonic() - steam_since >= STEAM_BYPASS_GRACE_SECONDS:
                verdict = await check_gate("steam")
                await _react_to_verdict("steam", verdict, win)
                steam_since = time.monotonic()  # re-arm, don't spam
        else:
            steam_since = None


async def screenshot_worker() -> None:
    """Every SCREENSHOT_INTERVAL seconds, if an editor is focused, grab a
    screenshot as evidence for future gate judgments."""
    SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    await asyncio.sleep(45)
    while True:
        await asyncio.sleep(SCREENSHOT_INTERVAL)
        win = await asyncio.to_thread(_focused_window)
        if win is None or win.get("app_id", "") not in EDITOR_APPS:
            continue
        app_id = win["app_id"]
        path = SCREENS_DIR / f"{int(time.time())}.png"
        try:
            await asyncio.to_thread(
                subprocess.run, ["grim", str(path)], capture_output=True, timeout=10
            )
        except Exception:
            continue
        if not path.exists():
            continue
        await asyncio.to_thread(save_screenshot, str(path), app_id)
        _prune_screenshots()


def _prune_screenshots() -> None:
    paths = sorted(SCREENS_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime)
    for stale in paths[:-SCREENSHOT_KEEP]:
        stale.unlink(missing_ok=True)


def _notify(notif_type: str, message: str) -> None:
    try:
        subprocess.run(
            [_NOTIFY, "--type", notif_type, "--title", "ᚹ Huginn", "--body", message],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


async def _react_to_verdict(target: str, verdict: dict, window: dict) -> None:
    """Notify (and for youtube, sometimes act) on a denial. Approved verdicts
    stay silent — no reason to chirp at someone legitimately playing/watching."""
    if verdict["approved"]:
        return

    # A game-mode resource-policy denial or an evidence-invalid uncertain
    # result isn't a real judgment (no evidence was weighed, or the
    # evidence itself couldn't be trusted) — never let either trigger the
    # destructive "close" reaction, only a notify.
    if target != "youtube" or verdict.get("reason") == "game_mode" or verdict.get("uncertain"):
        await asyncio.to_thread(_notify, "warn", verdict["message"])
        return

    action = random.choices(["nag", "refocus", "close"], weights=[0.6, 0.3, 0.1])[0]
    await asyncio.to_thread(_notify, "warn", verdict["message"])
    if action == "refocus":
        await asyncio.to_thread(
            subprocess.run, ["niri", "msg", "action", "focus-window-previous"],
            capture_output=True, timeout=5,
        )
    elif action == "close":
        win_id = window.get("id")
        if win_id is not None:
            await asyncio.to_thread(
                subprocess.run,
                ["niri", "msg", "action", "close-window", "--id", str(win_id)],
                capture_output=True, timeout=5,
            )


def activity_summary(seconds: int = 12 * 3600) -> str:
    rows = activity_since(seconds)
    if not rows:
        return "(no activity recorded yet today)"
    runs: list[list] = []
    for r in rows:
        if runs and runs[-1][0] == r["app_id"]:
            runs[-1][2] = r["ts"]
        else:
            runs.append([r["app_id"], r["ts"], r["ts"], r["title"]])
    lines = []
    for app_id, start, end, title in runs:
        mins = max(1, (end - start) // 60)
        lines.append(f"- {app_id} ({title[:60]}): ~{mins}m")
    return "\n".join(lines)


def _summarize_verdicts(target: str) -> str:
    rows = recent_verdicts(target, limit=5)
    if not rows:
        return "(no prior verdicts)"
    return "\n".join(
        f"- {'approved' if r['approved'] else 'denied'}: {r['message']}" for r in rows
    )


_EVIDENCE_INVALID_MESSAGES = {
    InvalidReason.MISSING: "No screenshot evidence right now — can't verify. Try again shortly.",
    InvalidReason.CORRUPT: "Screenshot evidence unreadable — can't verify. Try again shortly.",
    InvalidReason.STALE: "Screenshot evidence too old to trust — can't verify. Try again shortly.",
    InvalidReason.EMPTY_SET: "No usable screenshot evidence — can't verify. Try again shortly.",
    InvalidReason.TIMESTAMP_MISMATCH: "Evidence doesn't match recent activity — can't verify. Try again shortly.",
}


def _latest_activity_ts(window_seconds: int = 12 * 3600) -> float | None:
    rows = activity_since(window_seconds)
    return max((r["ts"] for r in rows), default=None)


async def check_gate(target: str) -> dict:
    cached = last_verdict(target, GATE_TTL_SECONDS)
    if cached:
        return {"approved": bool(cached["approved"]), "message": cached["message"], "cached": True}

    # Evidence validity is a deterministic, pre-inference check — moved out
    # of model judgment entirely. Audition finding: every candidate model
    # tested confidently guessed approve/deny on stale/missing/corrupt
    # screenshots instead of recognizing invalid evidence as grounds for
    # uncertainty (0.0-0.6 calibration across the board). A model never
    # even sees evidence that fails this check, and this never claims
    # procrastination — it's a distinct "can't verify" result, not a denial.
    images = recent_screenshots(limit=5)
    check = validate_screenshots(images, latest_activity_ts=_latest_activity_ts())
    if not check.valid:
        return {
            "approved": False,
            "uncertain": True,
            "message": _EVIDENCE_INVALID_MESSAGES[check.reason],
            "cached": False,
            "reason": check.reason.value,
        }

    prompt = GATE_PROMPT.format(
        target=target,
        activity_summary=activity_summary(),
        discord_status="yes" if await asyncio.to_thread(in_discord_call) else "no",
        recent_verdicts=_summarize_verdicts(target),
    )

    try:
        raw = await judge_local_only(prompt, list(check.valid_paths))
        verdict = _parse_verdict(raw)
    except CoordinatorDenied as e:
        if e.denial == Denial.GAME_MODE:
            # Resource policy, not a judgment: gemma4:31b doesn't fit this
            # box's VRAM alongside anything else and is slow once split.
            # The coordinator already refused to touch Ollama at all — this
            # just proactively unloads it if it happens to already be
            # resident, via the coordinator (never out-of-band). Not cached:
            # a cached denial here would outlive game mode ending, wrongly
            # blocking a real request made moments after the user stops.
            await unload_model(MODELS["vision"]["model"])
            return {
                "approved": False,
                "uncertain": True,
                "message": "Not while you're playing. Ask again after.",
                "cached": False,
                "reason": "game_mode",
            }
        # A coordinator denial (deadline/preempted/queue-full/lock-timeout)
        # is a resource failure, not a judgment — inability to verify, never
        # an accusation. Not cached: a transient failure shouldn't stick
        # around as a denial for GATE_TTL_SECONDS (the old bool-only schema
        # didn't distinguish these; this does).
        return {
            "approved": False,
            "uncertain": True,
            "message": "Couldn't verify in time. Try again shortly.",
            "cached": False,
            "reason": e.denial.value,
        }
    except Exception:
        return {
            "approved": False,
            "uncertain": True,
            "message": "Couldn't verify right now. Try again shortly.",
            "cached": False,
            "reason": "judge_error",
        }

    verdict["message"] = _clip(verdict["message"])
    if not verdict.get("uncertain"):
        save_verdict(target, verdict["approved"], verdict["message"])
    return {**verdict, "cached": False}


# Notification bubble is a fixed 220px-wide, 250px-tall panel — a verdict much
# past this wraps past the panel edge and renders as nothing visible at all.
_MESSAGE_CAP = 110


def _clip(message: str) -> str:
    message = message.strip()
    if len(message) <= _MESSAGE_CAP:
        return message
    return message[:_MESSAGE_CAP - 1].rsplit(" ", 1)[0] + "…"


_UNPARSEABLE_MESSAGE = "Couldn't read a clear verdict. Try again shortly."


def _parse_verdict(raw: str) -> dict:
    """Extended 3-way schema (verdict: approve|deny|uncertain + confidence),
    used by qwen3.8:27b per the audition (scripts/vision_bench/schema.py —
    this mirrors that, since production needed the same uncertain option to
    ever be reachable). A malformed or unparseable response is itself
    inability to verify, not a denial — it maps to uncertain=True, never a
    confident approved=False."""
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return {"approved": False, "uncertain": True, "message": _UNPARSEABLE_MESSAGE}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {"approved": False, "uncertain": True, "message": _UNPARSEABLE_MESSAGE}

    verdict = data.get("verdict")
    message = str(data.get("message", "")) or _UNPARSEABLE_MESSAGE
    if verdict == "approve":
        return {"approved": True, "uncertain": False, "message": message}
    if verdict == "deny":
        return {"approved": False, "uncertain": False, "message": message}
    if verdict == "uncertain":
        return {"approved": False, "uncertain": True, "message": message}
    return {"approved": False, "uncertain": True, "message": _UNPARSEABLE_MESSAGE}
