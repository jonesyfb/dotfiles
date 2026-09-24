"""
Screenshot capture for gatekeeper evidence.

One reusable async primitive (`capture_screenshot`) used by both the
periodic editor-focused worker (gatekeeper.screenshot_worker) and an
uncached gate check's mandatory on-demand capture (gatekeeper.check_gate)
— previously each had its own copy-pasted grim invocation, and only the
periodic one even checked grim's return code.

Also owns deterministic local resizing of evidence before it's sent to the
vision model: production screenshots are native desktop resolution
(measured 2560x2040 on this box) and a single one of those costs ~4100
Ollama prompt tokens against qwen3.8:27b — three of them alone blow past
the 8192-token context window before the prompt text is even counted (see
the HTTP 400 investigation, gatekeeper commit). Resizing to 1024px on the
long edge measured ~860 tokens/image, confirmed live against the real
model server on 2026-09-03 (scripts/vision_bench or ad-hoc repro against
/api/chat) — three of those plus the full GATE_PROMPT text still leaves
comfortable headroom under 8192 for generation. Never touches the
original evidence file: resized copies live in SCREENS_DIR/resized/,
regenerated (cheap) whenever the original is newer than the cached copy.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import subprocess
import time
from pathlib import Path

from config import (
    CAPTURE_LOCK_TIMEOUT_SECONDS, EVIDENCE_RESIZE_MAX_DIM, GRIM_TIMEOUT_SECONDS,
    SCREENS_DIR,
)
from evidence import is_valid_png

log = logging.getLogger("huginn.capture")

RESIZED_DIR = SCREENS_DIR / "resized"

_resolved_grim_env: dict[str, str] | None = None


def _grim_env() -> dict[str, str]:
    """grim needs WAYLAND_DISPLAY to find the compositor socket. huginn.service
    only gets it if niri had already pushed it into the systemd user manager
    before the daemon forked (a boot-time race — see huginn.service's
    After=graphical-session.target and the 2026-09-04 fix); ordering alone
    hasn't been reliable enough in practice, and every grim call since a
    fork with a stale env fails identically until the daemon restarts. If
    our own process env is missing it, pull the *current* value straight
    from the systemd user manager instead of trusting the fork-time
    snapshot — self-heals without needing a daemon restart."""
    global _resolved_grim_env
    if _resolved_grim_env is not None:
        return _resolved_grim_env
    env = dict(os.environ)
    if "WAYLAND_DISPLAY" in env:
        _resolved_grim_env = env
        return env
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True, timeout=5, text=True, check=True,
        ).stdout
        for line in out.splitlines():
            key, sep, value = line.partition("=")
            if sep and key in ("WAYLAND_DISPLAY", "DISPLAY", "XDG_RUNTIME_DIR") and key not in env:
                env[key] = value
    except Exception as e:
        log.warning("could not resolve display environment for grim: %s", e)
    if "WAYLAND_DISPLAY" in env:
        _resolved_grim_env = env
    return env


async def capture_screenshot(tag: str) -> "Path | None":
    """Take one full-desktop screenshot via grim into SCREENS_DIR with a
    collision-resistant filename (nanosecond timestamp + random suffix —
    plain integer seconds isn't unique enough when a periodic capture and
    an on-demand one can land in the same second). Checks grim's return
    code, the output file's existence, and that it's a decodable PNG.
    Never raises; returns None on any failure so a bad capture can never
    be mistaken for evidence. `tag` is a cosmetic filename hint only, not
    an identity — this is always a full-desktop capture regardless of
    what's focused."""
    SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENS_DIR / f"{time.time_ns()}-{tag}-{secrets.token_hex(4)}.png"
    try:
        proc = await asyncio.to_thread(
            subprocess.run, ["grim", str(path)], capture_output=True, timeout=GRIM_TIMEOUT_SECONDS,
            env=_grim_env(),
        )
    except Exception as e:
        log.warning("grim capture failed to run: %s", e)
        path.unlink(missing_ok=True)
        return None
    if proc.returncode != 0:
        log.warning("grim exited %d capturing evidence", proc.returncode)
        # A nonzero exit can still leave a partial/stale file at `path` —
        # never trust it just because it exists.
        path.unlink(missing_ok=True)
        return None
    if not path.exists() or not is_valid_png(path):
        log.warning("grim reported success but output is missing or not a valid PNG")
        path.unlink(missing_ok=True)
        return None
    return path


class CaptureLockTimeout(Exception):
    """Raised when a fresh on-demand capture couldn't even be scheduled
    within CAPTURE_LOCK_TIMEOUT_SECONDS — distinct from a capture that
    started and failed (grim/PNG problems, see capture_screenshot)."""


# Single-flight state: concurrent gate checks (e.g. a steam check and a
# youtube check landing in the same tick) share ONE grim invocation rather
# than each kicking off their own — it's always a full-desktop capture, so
# there's nothing target-specific to race on. The lock only guards the
# decision of whether to start a new task; it is released immediately
# after and is never held across the grim call itself, let alone across
# model inference.
_capture_task: "asyncio.Task | None" = None
_capture_task_lock = asyncio.Lock()


async def capture_fresh_on_demand(tag: str) -> "Path | None":
    global _capture_task
    try:
        async with asyncio.timeout(CAPTURE_LOCK_TIMEOUT_SECONDS):
            async with _capture_task_lock:
                if _capture_task is None or _capture_task.done():
                    _capture_task = asyncio.ensure_future(capture_screenshot(tag))
                task = _capture_task
    except TimeoutError:
        raise CaptureLockTimeout("timed out waiting to start an on-demand capture")
    return await task


def _resized_path_for(original: Path) -> Path:
    return RESIZED_DIR / f"{original.stem}.png"


def ensure_resized(original: Path, max_dim: int = EVIDENCE_RESIZE_MAX_DIM) -> Path:
    """Deterministic local resize for model consumption only. Preserves
    aspect ratio, never overwrites `original`, and caches the result in
    RESIZED_DIR (still inside the existing private screens area) keyed off
    the original's filename — reused as-is if already newer than the
    source, regenerated otherwise."""
    from PIL import Image

    RESIZED_DIR.mkdir(parents=True, exist_ok=True)
    out = _resized_path_for(original)
    if out.exists() and out.stat().st_mtime >= original.stat().st_mtime:
        return out

    with Image.open(original) as im:
        im.load()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        scale = min(1.0, max_dim / max(w, h))
        if scale < 1.0:
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        im.save(out, format="PNG")
    return out


def prepare_evidence_for_model(paths: list[str], max_dim: int = EVIDENCE_RESIZE_MAX_DIM) -> list[str]:
    """Resize each already-validated evidence path for the model's context
    budget. A resize failure on one file is skipped, not fatal — the
    caller already confirmed these are valid PNGs; degraded local resizing
    infrastructure shouldn't turn valid evidence into a hard error."""
    out = []
    for p in paths:
        try:
            out.append(str(ensure_resized(Path(p), max_dim=max_dim)))
        except Exception as e:
            log.warning("skipping evidence image that failed to resize: %s", e)
    return out
