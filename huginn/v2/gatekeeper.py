"""
Huginn gatekeeper: watches window focus, takes evidence screenshots, and asks
Huginn to judge whether gated apps (Steam, recreational YouTube) are earned.
"""
import asyncio
import json
import logging
import random
import re
import subprocess
import time

from capture import (
    CaptureLockTimeout, capture_fresh_on_demand, capture_screenshot,
    prepare_evidence_for_model,
)
from config import (
    ACTIVITY_POLL, BROWSER_APPS, EDITOR_APPS, GATE_PROMPT,
    MAX_HISTORICAL_SCREENSHOTS, MODELS, SCREENS_DIR, SCREENSHOT_INTERVAL,
    SCREENSHOT_KEEP, STEAM_BYPASS_GRACE_SECONDS, YOUTUBE_GRACE_SECONDS,
)
from context import focused_window as _focused_window, in_discord_call
from coordinator import Denial
from evidence import InvalidReason, validate_screenshots
from llm import CoordinatorDenied, OllamaInvalidRequest, judge_local_only, unload_model
from memory import (
    activity_since, add_turn, log_activity, prune_activity,
    recent_screenshots, recent_verdicts, save_screenshot, save_verdict,
)

log = logging.getLogger("huginn.gatekeeper")

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
    screenshot as evidence for future gate judgments. Capture itself is
    the same primitive check_gate()'s on-demand capture uses
    (capture.capture_screenshot) — this worker's only job is deciding
    *when* (editor focused, on the long periodic cadence)."""
    SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    await asyncio.sleep(45)
    while True:
        await asyncio.sleep(SCREENSHOT_INTERVAL)
        win = await asyncio.to_thread(_focused_window)
        if win is None or win.get("app_id", "") not in EDITOR_APPS:
            continue
        app_id = win["app_id"]
        path = await capture_screenshot("periodic")
        if path is None:
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

_CAPTURE_FAILED_MESSAGE = "Couldn't capture evidence right now — can't verify. Try again shortly."
_CAPTURE_LOCK_TIMEOUT_MESSAGE = "Busy capturing evidence for another check — try again in a moment."
_INVALID_REQUEST_MESSAGE = "Couldn't verify right now. Try again shortly."


def _latest_activity_ts(window_seconds: int = 12 * 3600) -> float | None:
    rows = activity_since(window_seconds)
    return max((r["ts"] for r in rows), default=None)


def _bounded_evidence_paths(fresh: str) -> list[str]:
    """Mandatory fresh capture + at most MAX_HISTORICAL_SCREENSHOTS other
    recent screenshots, deduplicated by path, explicitly ordered oldest
    historical first and the mandatory fresh capture always last (it's the
    one guaranteed to cover the activity that triggered this check)."""
    historical = recent_screenshots(limit=MAX_HISTORICAL_SCREENSHOTS + 1)
    seen = {fresh}
    picked: list[str] = []
    for p in historical:
        if p in seen:
            continue
        seen.add(p)
        picked.append(p)
        if len(picked) >= MAX_HISTORICAL_SCREENSHOTS:
            break
    picked.reverse()
    picked.append(fresh)
    return picked


def _log_verdict_to_history(target: str, message: str) -> None:
    """Real stances (approve/deny — never a transient uncertain/infra
    result) go into the same `history` table the chat panel reads for
    context. Without this, a desktop-triggered denial and a chat plea
    about it were two disconnected systems: the model in chat had no
    idea a denial just happened, and arguing "let me open steam, it's
    Sunday" in chat couldn't touch the actual verdict. Tagged so the
    model can tell it's a gatekeeper stance, not something it said itself."""
    add_turn("assistant", f"[gatekeeper:{target}] {message}")


async def check_gate(target: str) -> dict:
    # No verdict caching — a stale denial used to stick around for
    # GATE_TTL_SECONDS regardless of what changed (new day, joined a
    # Discord call with friends, argued the case in chat), so a legit
    # "circumstances changed" retry got the exact same canned denial back.
    # Every check re-judges fresh now; recent_verdicts() below still feeds
    # the prompt so tone stays consistent without blocking a real re-look.

    # Hardcoded Sunday lenience: no work-earned standard applies on a day
    # off. Skips judgment entirely (no capture, no model call) rather than
    # just biasing the prompt — a day-of-week check is a fact, not
    # something that needs an LLM's opinion.
    if time.localtime().tm_wday == 6:  # Monday=0 .. Sunday=6
        verdict = {"approved": True, "message": "It's Sunday. Go have fun.", "cached": False}
        save_verdict(target, True, verdict["message"])
        _log_verdict_to_history(target, verdict["message"])
        return verdict

    # The activity that triggered this check is already recorded by the
    # time check_gate runs (activity_tracker_worker logs before calling
    # this) — capture a fresh screenshot now, on demand, rather than
    # trusting whatever the periodic editor-only worker happened to grab
    # last. This is what actually fixes the timestamp-mismatch race: a
    # gate check no longer depends on a screenshot that can be up to
    # SCREENSHOT_INTERVAL stale relative to the activity it's judging.
    latest_activity_ts = _latest_activity_ts()
    try:
        fresh_path = await capture_fresh_on_demand(target)
    except CaptureLockTimeout:
        return {
            "approved": False,
            "uncertain": True,
            "message": _CAPTURE_LOCK_TIMEOUT_MESSAGE,
            "cached": False,
            "reason": "capture_lock_timeout",
        }
    if fresh_path is None:
        return {
            "approved": False,
            "uncertain": True,
            "message": _CAPTURE_FAILED_MESSAGE,
            "cached": False,
            "reason": "capture_failed",
        }

    win = await asyncio.to_thread(_focused_window)
    app_id = (win or {}).get("app_id") or target
    await asyncio.to_thread(save_screenshot, str(fresh_path), app_id)
    _prune_screenshots()

    # Evidence validity is a deterministic, pre-inference check — moved out
    # of model judgment entirely. Audition finding: every candidate model
    # tested confidently guessed approve/deny on stale/missing/corrupt
    # screenshots instead of recognizing invalid evidence as grounds for
    # uncertainty (0.0-0.6 calibration across the board). A model never
    # even sees evidence that fails this check, and this never claims
    # procrastination — it's a distinct "can't verify" result, not a denial.
    # Still run even with a guaranteed-fresh capture in hand: a genuinely
    # broken clock or a capture that silently covers the wrong screen
    # should still be caught here, not just trusted because grim exited 0.
    images = _bounded_evidence_paths(str(fresh_path))
    check = validate_screenshots(images, latest_activity_ts=latest_activity_ts)
    if not check.valid:
        return {
            "approved": False,
            "uncertain": True,
            "message": _EVIDENCE_INVALID_MESSAGES[check.reason],
            "cached": False,
            "reason": check.reason.value,
        }

    model_images = prepare_evidence_for_model(list(check.valid_paths))

    prompt = GATE_PROMPT.format(
        target=target,
        activity_summary=activity_summary(),
        discord_status="yes" if await asyncio.to_thread(in_discord_call) else "no",
        recent_verdicts=_summarize_verdicts(target),
    )

    try:
        raw = await judge_local_only(prompt, model_images)
        verdict = _parse_verdict(raw)
    except OllamaInvalidRequest as e:
        # Malformed request to Ollama (root cause, investigated live: full-
        # resolution evidence exceeding num_ctx — see capture.py's resize
        # and llm._judge_ollama's options) — distinct from a backend/
        # network failure. Logged (status/category only) in llm.py, never
        # here, and never cached: whatever made the request malformed may
        # not on the next attempt (e.g. evidence set size changes).
        log.warning("gate check for %r hit an invalid Ollama request: category=%s", target, e.category)
        return {
            "approved": False,
            "uncertain": True,
            "message": _INVALID_REQUEST_MESSAGE,
            "cached": False,
            "reason": "invalid_request",
        }
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
        _log_verdict_to_history(target, verdict["message"])
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
