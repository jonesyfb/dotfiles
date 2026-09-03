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

import coordinator as _coordinator_module
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


def collect_attention(
    interaction: InteractionState, desktop: "DesktopState | None" = None
) -> AttentionState:
    if not interaction.interruptions_allowed:
        return AttentionState("do_not_disturb", evidence=interaction.evidence)
    if desktop is not None and desktop.in_discord_call:
        return AttentionState("do_not_disturb", evidence=("discord voice call active",))
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
    loaded: bool = False  # currently resident in Ollama right now (ollama-backed only)


async def probe_ollama_tags() -> set[str] | None:
    """Exact model:tag strings currently pulled in Ollama (e.g. "qwen3.5:9b"),
    or None if Ollama couldn't be reached at all — callers must not treat
    None as "no models available", only as "unknown".

    Exact tags, not base names: qwen3.5:9b, qwen3.5:27b, and the personality
    model's qwen3.5:4b all share the base name "qwen3.5" — matching on base
    name alone would report qwen3.5:4b as pulled just because a same-family,
    different-size model is."""
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{OLLAMA_BASE}/api/tags")
            r.raise_for_status()
            data = r.json()
            return {m["name"] for m in data.get("models", [])}
    except Exception:
        return None


async def probe_ollama_loaded() -> list[dict] | None:
    """Raw /api/ps entries for models currently resident in Ollama (each has
    at least `model`, `size`, `size_vram`, `expires_at`), or None if the
    probe itself failed — distinct from "nothing loaded" (an empty list)."""
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{OLLAMA_BASE}/api/ps")
            r.raise_for_status()
            return r.json().get("models", [])
    except Exception:
        return None


def collect_models(
    ollama_tags: set[str] | None, loaded: list[dict] | None = None
) -> dict[str, ModelStatus]:
    loaded_names = {m.get("model", "") for m in (loaded or [])}
    statuses: dict[str, ModelStatus] = {}
    for key, spec in MODELS.items():
        backend = spec["backend"]
        model = spec["model"]
        if backend == "ollama":
            if ollama_tags is None:
                statuses[key] = ModelStatus(key, backend, model, True, None, "ollama unreachable")
            else:
                pulled = model in ollama_tags
                statuses[key] = ModelStatus(
                    key, backend, model, True, pulled,
                    "" if pulled else "model not pulled",
                    loaded=model in loaded_names,
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


# ── Model resources ──────────────────────────────────────────────────────────
# Distinct from ModelStatus (per-model availability): this is the aggregate
# resource picture — what's actually resident right now, whether using a
# given model would evict something else, and the game-mode/cloud
# constraints the resource policy (v2/ambient.py, gatekeeper.py) must honor.

@dataclass(frozen=True)
class ModelResourceState:
    loaded_models: tuple[str, ...]       # Ollama model strings currently resident
    ollama_reachable: bool
    contention: bool                     # more than one distinct model resident at once
    swap_required: dict[str, bool]       # per MODELS key: would selecting it evict a different resident model
    game_mode_restricts_to_personality: bool
    cloud_prohibited_for_local_only: bool  # restates llm.judge_local_only's structural guarantee, for the debug view
    evidence: tuple[str, ...] = ()


def collect_model_resources(
    models: dict[str, ModelStatus],
    loaded: list[dict] | None,
    interaction: InteractionState,
) -> ModelResourceState:
    reachable = loaded is not None
    loaded = loaded or []
    loaded_names = tuple(m.get("model", "") for m in loaded)
    distinct = set(loaded_names)

    swap_required = {
        key: bool(distinct) and status.model not in distinct
        for key, status in models.items()
        if status.backend == "ollama"
    }

    return ModelResourceState(
        loaded_models=loaded_names,
        ollama_reachable=reachable,
        contention=len(distinct) > 1,
        swap_required=swap_required,
        game_mode_restricts_to_personality=(interaction.mode == "game"),
        cloud_prohibited_for_local_only=True,
        evidence=tuple(f"{n} resident" for n in loaded_names),
    )


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
    model_resources: ModelResourceState
    coordinator: dict  # coordinator.Coordinator.snapshot() — already diagnostic-safe


async def collect() -> RuntimeContext:
    """Full snapshot. Safe to call at any time — every sub-collector fails
    soft (never raises) and reports its own unknown/unreachable state rather
    than guessing."""
    interaction = collect_interaction()
    task = await asyncio.to_thread(collect_task)
    ollama_tags = await probe_ollama_tags()
    loaded = await probe_ollama_loaded()
    models = collect_models(ollama_tags, loaded)
    tools = collect_tools(ollama_tags)
    desktop = await asyncio.to_thread(collect_desktop)
    attention = collect_attention(interaction, desktop)
    model_resources = collect_model_resources(models, loaded, interaction)
    coordinator_snapshot = _coordinator_module.coordinator.snapshot()
    return RuntimeContext(
        time.time(), interaction, attention, task, models, tools, desktop,
        model_resources, coordinator_snapshot,
    )


# ── Debug view ───────────────────────────────────────────────────────────────
# Hand-written, not a generic asdict() dump — that's deliberate. This is the
# one function responsible for redaction: it must never surface a window
# title (may contain private page/document content), a credential value, or
# anything from gatekeeper's screenshots/activity_log (which this module
# never touches in the first place). Each section's existing source/reason/
# evidence strings already double as the observed/inferred/manual-override/
# stale marking — no separate taxonomy is layered on top of them.

def to_debug_dict(snapshot: RuntimeContext) -> dict:
    overrides_active = []
    if snapshot.interaction.source.startswith("manual"):
        overrides_active.append(snapshot.interaction.mode)

    return {
        "timestamp": snapshot.timestamp,
        "overrides_active": overrides_active,
        "interaction": {
            "mode": snapshot.interaction.mode,
            "interruptions_allowed": snapshot.interaction.interruptions_allowed,
            "basis": snapshot.interaction.source,
            "evidence": list(snapshot.interaction.evidence),
        },
        "attention": {
            "level": snapshot.attention.level,
            "evidence": list(snapshot.attention.evidence),
        },
        "task": {
            "state": snapshot.task.state,
            "queued_tasks": snapshot.task.queued_tasks,
            "evidence": list(snapshot.task.evidence),
        },
        "models": {
            key: {
                "backend": s.backend,
                "model": s.model,
                "configured": s.configured,
                "available": s.available,  # null = unknown, not "no"
                "loaded": s.loaded,
                "reason": s.reason,
            }
            for key, s in snapshot.models.items()
        },
        "tools": {
            "claude_cli": snapshot.tools.claude_cli,
            "ollama_reachable": snapshot.tools.ollama_reachable,
        },
        "desktop": {
            # app_id only — the window title can contain page/document
            # content and has no diagnostic value here.
            "focused_app_id": (snapshot.desktop.focused_window or {}).get("app_id"),
            "in_discord_call": snapshot.desktop.in_discord_call,
        },
        "model_resources": {
            "loaded_models": list(snapshot.model_resources.loaded_models),
            "ollama_reachable": snapshot.model_resources.ollama_reachable,
            "contention": snapshot.model_resources.contention,
            "swap_required": dict(snapshot.model_resources.swap_required),
            "game_mode_restricts_to_personality": snapshot.model_resources.game_mode_restricts_to_personality,
            "cloud_prohibited_for_local_only": snapshot.model_resources.cloud_prohibited_for_local_only,
        },
        # Already diagnostic-safe by construction (coordinator.snapshot()
        # only ever emits request_class/purpose/model/label/timing/denial —
        # never prompts, images, or generated content).
        "coordinator": snapshot.coordinator,
    }
