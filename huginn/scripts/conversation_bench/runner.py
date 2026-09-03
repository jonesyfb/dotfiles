"""
Blind direct-conversation model audition — execution engine.

Standalone, like scripts/benchmark-vision-models.py: does NOT route
through v2/coordinator.py's scheduler (this is an offline research tool,
not production traffic), but DOES honor the same cross-process
/tmp/ollama.lock contract and game-mode refusal, and unloads its model
between candidates to leave a clean slate.

Fairness contract (see scenarios.py docstring for the prompt-shape
rationale):
- Same system prompt (config.DIRECT_SOCIAL_SYSTEM_PROMPT) for every
  candidate, unmodified.
- Same entity lens (v2/entities.py), pointed at an isolated throwaway
  sqlite file for the whole run — never the production database.
- Same sampling options and think:false for every call.
- No tools, ever.
- Entity corrections/restores are applied by the harness deterministically
  between turns (entities.register()/forget_override()), never inferred
  from a candidate's own output — every candidate sees identical
  authoritative entity state at the same point in a scenario.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "v2"))

import entities  # noqa: E402
import memory  # noqa: E402
from config import DIRECT_SOCIAL_SYSTEM_PROMPT, GAME_MODE_FLAG, OLLAMA_BASE  # noqa: E402
from coordinator import DeadlineExceeded, _acquire_shared_lock, _release_shared_lock  # noqa: E402

from .scenarios import GENERATION_OPTIONS, MULTI_TURN_RUNS, SEEDS, SINGLE_TURN_TRIALS, Scenario  # noqa: E402

LOCK_ACQUIRE_DEADLINE_SECONDS = 30.0
GPU_BUSY_WARN_THRESHOLD = 15
GPU_BUSY_PATH = Path("/sys/class/drm/card1/device/gpu_busy_percent")


class GameModeActive(Exception):
    pass


def refuse_if_game_mode() -> None:
    if Path(GAME_MODE_FLAG).exists():
        raise GameModeActive(
            "Game mode is active. Refusing to run — this benchmark loads large models "
            "and will fight a running game for VRAM. Exit game mode first."
        )


def competing_gpu_activity_warning() -> "str | None":
    try:
        busy = int(GPU_BUSY_PATH.read_text().strip())
    except Exception:
        return None
    if busy >= GPU_BUSY_WARN_THRESHOLD:
        return f"GPU already at {busy}% busy before this benchmark started — results may be contaminated."
    return None


def use_isolated_memory() -> Path:
    """Points v2/memory.py (and therefore v2/entities.py, which is the
    only production module this harness touches) at a throwaway sqlite
    file — the real ~/.local/share/huginn/huginn_v2.db is never opened."""
    tmp = Path(tempfile.mkdtemp(prefix="huginn_conversation_bench_")) / "bench.db"
    memory.DB_PATH = tmp
    return tmp


def entity_note_for(mentions: list[str]) -> str:
    """Identical shape to daemon._entity_note_for — duplicated rather than
    imported so this harness has no dependency on daemon.py (which pulls
    in the async socket server, task workers, etc.)."""
    if not mentions:
        return ""
    lines = ["Resolved identities for things mentioned (use only if it fits naturally):"]
    for name in mentions:
        identity = entities.resolve(name)
        if identity is None:
            continue
        bits = [identity.canonical_name]
        if identity.archetype:
            bits.append(f"archetype: {identity.archetype}")
        if identity.collective_form:
            bits.append(f"collective form: {identity.collective_form}")
        lines.append("  - " + ", ".join(bits))
    return "\n".join(lines) if len(lines) > 1 else ""


def build_user_prompt(
    message: str, history: list[dict], entity_note: str,
    available_context_claims: tuple, procrastination_nudge_authorized: bool,
) -> str:
    """Same shape as personality._build_direct_social_prompt MINUS the
    per-SocialSubtype length/sentence target line — see scenarios.py
    module docstring for why that line is deliberately excluded here."""
    lines = []
    if available_context_claims:
        lines.append("Known right now — you may reference ONLY these facts about the present, nothing else:")
        for c in available_context_claims:
            lines.append(f"  - {c}")
    else:
        lines.append(
            "observed_current_state: none — you have no information about what he's "
            "currently doing, viewing, or how he feels beyond what he just said."
        )
    lines.append(f"procrastination_nudge_authorized: {'true' if procrastination_nudge_authorized else 'false'}")
    if history:
        lines.append("")
        lines.append("Recent conversation (oldest first):")
        for turn in history:
            speaker = "Nathan" if turn["role"] == "user" else "You"
            lines.append(f"{speaker}: {turn['content']}")
    if entity_note:
        lines.append("")
        lines.append(entity_note)
    lines.append("")
    lines.append(f"Nathan just said: {message}")
    lines.append("Reply directly to him now, in character, following the identity, voice, and hard limits above.")
    return "\n".join(lines)


async def _unload(model: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"{OLLAMA_BASE}/api/generate", json={"model": model, "keep_alive": 0})
    except Exception:
        pass
    await asyncio.sleep(1)


async def _residency(model: str) -> "dict | None":
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{OLLAMA_BASE}/api/ps")
            for m in r.json().get("models", []):
                if m.get("model") == model:
                    return m
    except Exception:
        pass
    return None


@dataclass
class Turn:
    scenario_key: str
    category: str
    run_label: str  # "trial_1" | "trial_2" | "trial_3" for single-turn; "run_1".."run_3" for multi-turn
    turn_index: int
    seed: int
    message: str
    history_before: list[dict]
    entity_note: str
    available_context_claims: tuple
    procrastination_nudge_authorized: bool
    prompt: str
    response: str
    elapsed_seconds: float
    load_duration_seconds: "float | None"
    eval_count: "int | None"
    eval_duration_seconds: "float | None"
    error: "str | None" = None


async def _call(model: str, prompt: str, seed: int) -> dict:
    options = dict(GENERATION_OPTIONS)
    options["seed"] = seed
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": DIRECT_SOCIAL_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "think": False,
        "options": options,
    }
    start = time.monotonic()
    async with httpx.AsyncClient(timeout=180) as c:
        r = await c.post(f"{OLLAMA_BASE}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()
    elapsed = time.monotonic() - start
    return {
        "elapsed_seconds": round(elapsed, 2),
        "response": data.get("message", {}).get("content", "").strip(),
        "load_duration_seconds": round(data["load_duration"] / 1e9, 3) if "load_duration" in data else None,
        "eval_count": data.get("eval_count"),
        "eval_duration_seconds": round(data["eval_duration"] / 1e9, 3) if "eval_duration" in data else None,
    }


async def _run_conversation(model: str, scenario: Scenario, run_label: str, seed: int) -> list[Turn]:
    history: list[dict] = list(scenario.pre_history)
    turns_out: list[Turn] = []
    last_index = len(scenario.turns) - 1

    for i, message in enumerate(scenario.turns):
        if i in scenario.entity_ops:
            op, name, archetype = scenario.entity_ops[i]
            current = entities.resolve(name)
            if op == "register" and current is not None:
                entities.register(entities.EntityIdentity(
                    canonical_name=current.canonical_name, archetype=archetype,
                    forbidden_domains=current.forbidden_domains, confidence=1.0, source="user",
                ))
            elif op == "forget":
                entities.forget_override(name)

        mentions = entities.extract_mentions(message)
        note = entity_note_for(mentions)
        claims = scenario.available_context_claims if i == last_index else ()
        authorized = scenario.procrastination_nudge_authorized if i == last_index else False
        prompt = build_user_prompt(message, history, note, claims, authorized)

        try:
            result = await _call(model, prompt, seed)
            response = result["response"]
            error = None
        except Exception as e:
            result = {"elapsed_seconds": None, "response": "", "load_duration_seconds": None,
                      "eval_count": None, "eval_duration_seconds": None}
            response = ""
            error = str(e)

        turns_out.append(Turn(
            scenario_key=scenario.key, category=scenario.category, run_label=run_label,
            turn_index=i, seed=seed, message=message, history_before=list(history),
            entity_note=note, available_context_claims=claims,
            procrastination_nudge_authorized=authorized, prompt=prompt, response=response,
            elapsed_seconds=result["elapsed_seconds"], load_duration_seconds=result["load_duration_seconds"],
            eval_count=result["eval_count"], eval_duration_seconds=result["eval_duration_seconds"], error=error,
        ))
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": response if not error else f"[error: {error}]"})

    return turns_out


async def run_candidate(model: str, scenarios: list[Scenario]) -> dict:
    refuse_if_game_mode()
    try:
        lock_f = await _acquire_shared_lock(LOCK_ACQUIRE_DEADLINE_SECONDS)
    except DeadlineExceeded as e:
        return {"model": model, "error": f"could not acquire shared Ollama lock: {e}", "turns": []}

    all_turns: list[dict] = []
    residency: "dict | None" = None
    try:
        await _unload(model)

        for scenario in scenarios:
            n_runs = MULTI_TURN_RUNS if scenario.is_multi_turn else SINGLE_TURN_TRIALS
            label_prefix = "run" if scenario.is_multi_turn else "trial"
            for run_n in range(n_runs):
                seed = SEEDS[run_n]
                # Entity state must start clean for each independent trial/run
                # of a scenario that mutates it, and clean between scenarios.
                for _op, name, _archetype in scenario.entity_ops.values():
                    entities.forget_override(name)
                turns = await _run_conversation(model, scenario, f"{label_prefix}_{run_n + 1}", seed)
                all_turns.extend(asdict(t) for t in turns)
                if residency is None:
                    residency = await _residency(model)

        residency_end = await _residency(model)
    finally:
        _release_shared_lock(lock_f)
        await _unload(model)

    return {"model": model, "turns": all_turns, "residency_first_call": residency, "residency_session_end": residency_end}
