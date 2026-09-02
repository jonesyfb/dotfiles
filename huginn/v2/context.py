"""
Huginn Runtime Context Engine.

Read-only source of truth for what Huginn currently believes about the
desktop, its models, and its own background task state. This exists to stop
the same signals (most notably the GAME_MODE_FLAG existence check) from
being independently re-derived in daemon.py and gatekeeper.py.

This slice is deliberately inert: nothing here gates, decides, or changes
behavior. Callers still make their own decisions exactly as before — this
module only reports state. daemon.py's routing (route_model, chime timing)
and gatekeeper.py's verdict logic are unchanged.

Known gap (documented, not silently dropped): per-connection tool
confirmation state lives in daemon.py's _pending_confirms dict. Reading it
here would require context.py to import daemon.py, which would invert the
dependency direction every other module in this package uses (config <-
memory/llm/tools <- gatekeeper/context <- daemon) and risk a cycle. Task
state below is therefore derived only from the sqlite task queue; a future
slice that wants confirmation-pending visibility should have daemon.py push
that count into this module rather than context.py reaching backward for it.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from config import GAME_MODE_FLAG, MODELS, OLLAMA_BASE

# ── Interaction ──────────────────────────────────────────────────────────────

# Only "ambient" and "game" have a real detector today (GAME_MODE_FLAG is the
# only manual override that exists). focus/meeting/quiet/sleep are listed so
# this stays extensible, but nothing derives them yet — don't fabricate a
# confident answer for a mode with no sensor behind it.
INTERACTION_MODES = ("ambient", "game", "focus", "meeting", "quiet", "sleep")


@dataclass(frozen=True)
class InteractionState:
    mode: str
    interruptions_allowed: bool
    source: str
    evidence: tuple[str, ...] = ()


def collect_interaction() -> InteractionState:
    if Path(GAME_MODE_FLAG).exists():
        return InteractionState(
            mode="game",
            interruptions_allowed=False,
            source="manual: game-mode flag file",
            evidence=(f"{GAME_MODE_FLAG} exists",),
        )
    return InteractionState(
        mode="ambient",
        interruptions_allowed=True,
        source="default (no manual override active)",
    )


# ── Attention ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AttentionState:
    level: str  # "available" | "do_not_disturb"
    evidence: tuple[str, ...] = ()


def collect_attention(interaction: InteractionState) -> AttentionState:
    if not interaction.interruptions_allowed:
        return AttentionState("do_not_disturb", evidence=interaction.evidence)
    return AttentionState("available")


# ── Task ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TaskState:
    state: str  # "idle" | "tool_running" | "tasks_queued"
    queued_tasks: int
    evidence: tuple[str, ...] = ()


def collect_task() -> TaskState:
    from memory import get_all_tasks, get_pending_tasks

    recent = get_all_tasks(limit=10)
    running = [t for t in recent if t.get("status") == "running"]
    queued = get_pending_tasks()
    if running:
        return TaskState(
            "tool_running", len(queued),
            evidence=(f"{len(running)} background task(s) running",),
        )
    if queued:
        return TaskState(
            "tasks_queued", len(queued),
            evidence=(f"{len(queued)} background task(s) queued",),
        )
    return TaskState("idle", 0)


# ── Models ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ModelStatus:
    key: str
    backend: str
    model: str
    configured: bool
    available: bool | None  # None = couldn't be determined (probe failed)
    reason: str = ""


async def probe_ollama_tags() -> set[str] | None:
    """Base model names (before the `:tag`) currently pulled in Ollama, or
    None if Ollama couldn't be reached at all — callers must not treat None
    as "no models available", only as "unknown"."""
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{OLLAMA_BASE}/api/tags")
            r.raise_for_status()
            data = r.json()
            return {m["name"].split(":")[0] for m in data.get("models", [])}
    except Exception:
        return None


def collect_models(ollama_tags: set[str] | None) -> dict[str, ModelStatus]:
    statuses: dict[str, ModelStatus] = {}
    for key, spec in MODELS.items():
        backend = spec["backend"]
        model = spec["model"]
        if backend == "ollama":
            if ollama_tags is None:
                statuses[key] = ModelStatus(key, backend, model, True, None, "ollama unreachable")
            else:
                base = model.split(":")[0]
                pulled = base in ollama_tags
                statuses[key] = ModelStatus(
                    key, backend, model, True, pulled,
                    "" if pulled else "model not pulled",
                )
        elif backend == "claude":
            # Presence check only — never read or expose the key value itself.
            has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
            statuses[key] = ModelStatus(
                key, backend, model, True, has_key if has_key else None,
                "" if has_key else "ANTHROPIC_API_KEY not set",
            )
        else:
            statuses[key] = ModelStatus(key, backend, model, False, None, f"unknown backend {backend!r}")
    return statuses


# ── Tools ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ToolAvailability:
    claude_cli: bool
    ollama_reachable: bool


def collect_tools(ollama_tags: set[str] | None) -> ToolAvailability:
    return ToolAvailability(
        claude_cli=shutil.which("claude") is not None,
        ollama_reachable=ollama_tags is not None,
    )


# ── Desktop ──────────────────────────────────────────────────────────────────
# Moved here from gatekeeper.py so there's one owner for niri/pactl polling
# instead of each future consumer re-implementing it.

@dataclass(frozen=True)
class DesktopState:
    focused_window: dict | None
    in_discord_call: bool
    evidence: tuple[str, ...] = ()


def focused_window() -> dict | None:
    try:
        out = subprocess.run(
            ["niri", "msg", "-j", "windows"], capture_output=True, text=True, timeout=5
        )
        windows = json.loads(out.stdout)
        for w in windows:
            if w.get("is_focused"):
                return w
    except Exception:
        pass
    return None


def in_discord_call() -> bool:
    """True if Discord has an active voice call (its WebRTC audio engine is
    playing back, which only happens while connected to a call)."""
    try:
        out = subprocess.run(
            ["pactl", "-f", "json", "list", "sink-inputs"],
            capture_output=True, text=True, timeout=5,
        )
        sinks = json.loads(out.stdout)
        return any(
            s.get("properties", {}).get("application.name") == "WEBRTC VoiceEngine"
            for s in sinks
        )
    except Exception:
        return False


def collect_desktop() -> DesktopState:
    win = focused_window()
    discord = in_discord_call()
    evidence = []
    if win:
        evidence.append(f"focused: {win.get('app_id', '?')} — {win.get('title', '')[:60]}")
    if discord:
        evidence.append("discord voice call active")
    return DesktopState(focused_window=win, in_discord_call=discord, evidence=tuple(evidence))


# ── Snapshot ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RuntimeContext:
    timestamp: float
    interaction: InteractionState
    attention: AttentionState
    task: TaskState
    models: dict[str, ModelStatus]
    tools: ToolAvailability
    desktop: DesktopState


async def collect() -> RuntimeContext:
    """Full snapshot. Safe to call at any time — every sub-collector fails
    soft (never raises) and reports its own unknown/unreachable state rather
    than guessing."""
    interaction = collect_interaction()
    attention = collect_attention(interaction)
    task = await asyncio.to_thread(collect_task)
    ollama_tags = await probe_ollama_tags()
    models = collect_models(ollama_tags)
    tools = collect_tools(ollama_tags)
    desktop = await asyncio.to_thread(collect_desktop)
    return RuntimeContext(time.time(), interaction, attention, task, models, tools, desktop)
