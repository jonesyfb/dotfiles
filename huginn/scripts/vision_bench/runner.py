"""
Benchmark execution: safety guards, cold/warm trials, raw result capture.

Deliberately standalone — does NOT route through v2/coordinator.py (this
is an offline audition tool, not a production request), but DOES honor
the same cross-process /tmp/ollama.lock contract Garage Watch and Huginn
use, with a bounded acquisition deadline, so it can't silently run
concurrently with either and can't hang forever if one of them is using
Ollama when this starts.
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "v2"))

import evidence  # noqa: E402
from config import GAME_MODE_FLAG, GATE_PROMPT, OLLAMA_BASE  # noqa: E402
from coordinator import DeadlineExceeded, _acquire_shared_lock, _release_shared_lock  # noqa: E402

from . import schema  # noqa: E402
from .corpus import Scenario  # noqa: E402

GENERATION_OPTIONS = {
    "temperature": 0.1,
    "num_ctx": 8192,
    "seed": 7,
}
LOCK_ACQUIRE_DEADLINE_SECONDS = 30.0
GPU_BUSY_WARN_THRESHOLD = 15  # percent; idle baseline on this box is ~1-3%
GPU_BUSY_PATH = Path("/sys/class/drm/card1/device/gpu_busy_percent")


class GameModeActive(Exception):
    pass


def refuse_if_game_mode() -> None:
    if Path(GAME_MODE_FLAG).exists():
        raise GameModeActive(
            "Game mode is active (~/.local/share/huginn/game-mode exists). "
            "Refusing to run — this benchmark loads large models and will fight "
            "a running game for VRAM. Exit game mode first."
        )


def competing_gpu_activity_warning() -> str | None:
    try:
        busy = int(GPU_BUSY_PATH.read_text().strip())
    except Exception:
        return None
    if busy >= GPU_BUSY_WARN_THRESHOLD:
        return f"GPU already at {busy}% busy before this benchmark started — results may be contaminated by other GPU work."
    return None


def _bench_prompt(s: Scenario) -> str:
    # Reuse GATE_PROMPT's evidence-framing paragraphs only — drop production's
    # own message-length/schema instructions (100-char bubble, bool schema)
    # so they don't collide with the benchmark's own extended schema below.
    evidence = GATE_PROMPT.split("The message renders")[0]
    evidence = evidence.format(
        target=s.target,
        activity_summary=s.activity_summary,
        discord_status=s.discord_status,
        recent_verdicts=s.recent_verdicts,
    )
    return evidence + schema.BENCHMARK_INSTRUCTION


def _load_images_b64(paths: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """Returns (base64_images, warnings) — a corrupt/unreadable file is
    skipped with a warning rather than crashing the whole trial."""
    images: list[str] = []
    warnings: list[str] = []
    for p in paths:
        try:
            data = Path(p).read_bytes()
            # Cheap validity check: real PNGs start with this 8-byte signature.
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                warnings.append(f"{p}: not a valid PNG, skipped")
                continue
            images.append(base64.b64encode(data).decode())
        except Exception as e:
            warnings.append(f"{p}: unreadable ({e}), skipped")
    return images, warnings


async def _unload(model: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"{OLLAMA_BASE}/api/generate", json={"model": model, "keep_alive": 0})
    except Exception:
        pass
    await asyncio.sleep(1)


async def _residency(model: str) -> dict | None:
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
class TrialResult:
    model: str
    scenario_key: str
    trial: str  # "cold" | "warm_N" | "evidence_invalid"
    timed_out: bool
    cancelled: bool
    elapsed_seconds: float
    eval_count: int | None
    eval_duration_seconds: float | None
    raw_response: str
    parsed: dict  # asdict(schema.ParsedVerdict)
    image_warnings: list[str]
    error: str | None = None
    evidence_reason: str | None = None  # set only for deterministic evidence-invalid trials


def _evidence_invalid_trial(model: str, scenario_key: str, check: "evidence.EvidenceCheck") -> TrialResult:
    """Zero-inference deterministic result — the model is never called.
    verdict is 'uncertain' with confidence 1.0 (we're fully certain the
    *evidence* is untrustworthy, distinct from a model's own low-confidence
    semantic hedge on genuinely ambiguous-but-valid evidence)."""
    parsed = schema.ParsedVerdict(True, verdict="uncertain", confidence=1.0, message=check.detail)
    return TrialResult(
        model=model, scenario_key=scenario_key, trial="evidence_invalid",
        timed_out=False, cancelled=False, elapsed_seconds=0.0,
        eval_count=None, eval_duration_seconds=None, raw_response="",
        parsed=asdict(parsed), image_warnings=[], evidence_reason=check.reason.value,
    )


async def _judge(model: str, prompt: str, images_b64: list[str], timeout: float) -> TrialResult:
    message: dict = {"role": "user", "content": prompt}
    if images_b64:
        message["images"] = images_b64
    payload = {"model": model, "messages": [message], "stream": False, "options": GENERATION_OPTIONS}

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{OLLAMA_BASE}/api/chat", json=payload)
            r.raise_for_status()
            data = r.json()
            elapsed = time.monotonic() - start
            raw = data.get("message", {}).get("content", "")
            parsed = schema.parse(raw)
            return TrialResult(
                model=model, scenario_key="", trial="", timed_out=False, cancelled=False,
                elapsed_seconds=round(elapsed, 2),
                eval_count=data.get("eval_count"),
                eval_duration_seconds=round(data["eval_duration"] / 1e9, 2) if "eval_duration" in data else None,
                raw_response=raw, parsed=asdict(parsed), image_warnings=[],
            )
    except httpx.TimeoutException:
        return TrialResult(
            model=model, scenario_key="", trial="", timed_out=True, cancelled=False,
            elapsed_seconds=round(time.monotonic() - start, 2),
            eval_count=None, eval_duration_seconds=None, raw_response="",
            parsed=asdict(schema.ParsedVerdict(False, error="timeout")), image_warnings=[],
        )
    except asyncio.CancelledError:
        return TrialResult(
            model=model, scenario_key="", trial="", timed_out=False, cancelled=True,
            elapsed_seconds=round(time.monotonic() - start, 2),
            eval_count=None, eval_duration_seconds=None, raw_response="",
            parsed=asdict(schema.ParsedVerdict(False, error="cancelled")), image_warnings=[],
        )
    except Exception as e:
        return TrialResult(
            model=model, scenario_key="", trial="", timed_out=False, cancelled=False,
            elapsed_seconds=round(time.monotonic() - start, 2),
            eval_count=None, eval_duration_seconds=None, raw_response="",
            parsed=asdict(schema.ParsedVerdict(False, error=str(e))), image_warnings=[], error=str(e),
        )


async def run_model(model: str, scenarios: list[Scenario], timeout: float, trials_per_scenario: int) -> dict:
    """Evidence validity is checked deterministically first (v2/evidence.py
    — the same module gatekeeper.py uses in production), exactly like the
    real integration: a scenario with invalid evidence never reaches the
    model at all, and gets exactly one recorded (zero-cost) trial rather
    than trials_per_scenario repeats, since the result is deterministic.

    Scenarios with valid evidence get one cold trial (the first one overall
    that actually reaches the model) + trials_per_scenario warm repeats, for
    verdict-stability and latency-distribution measurement.

    Holds the shared cross-process lock for the model's entire session
    (bounded acquisition — see module docstring) rather than per-request,
    since this is an offline research tool, not latency-sensitive
    production traffic."""
    refuse_if_game_mode()

    try:
        lock_f = await _acquire_shared_lock(LOCK_ACQUIRE_DEADLINE_SECONDS)
    except DeadlineExceeded as e:
        return {"model": model, "error": f"could not acquire shared Ollama lock: {e}", "trials": []}

    trials: list[dict] = []
    residency_after_cold_load: dict | None = None
    residency_after_session: dict | None = None
    did_cold = False
    try:
        await _unload(model)

        for scenario in scenarios:
            check = evidence.validate_screenshots(list(scenario.image_paths))
            if not check.valid:
                trials.append(asdict(_evidence_invalid_trial(model, scenario.key, check)))
                continue

            images_b64, warnings = _load_images_b64(check.valid_paths)
            prompt = _bench_prompt(scenario)
            n = trials_per_scenario + (0 if did_cold else 1)
            for trial_n in range(n):
                result = await _judge(model, prompt, images_b64, timeout)
                result.scenario_key = scenario.key
                if not did_cold:
                    result.trial = "cold"
                    did_cold = True
                    residency_after_cold_load = await _residency(model)
                else:
                    result.trial = f"warm_{trial_n}"
                result.image_warnings = warnings
                trials.append(asdict(result))

        residency_after_session = await _residency(model)
    finally:
        _release_shared_lock(lock_f)
        await _unload(model)  # leave a clean slate for the next candidate
        residency_after_unload = await _residency(model)

    return {
        "model": model,
        "residency_after_cold_load": residency_after_cold_load,
        "residency_after_session": residency_after_session,
        "residency_after_final_unload": residency_after_unload,  # non-None here = left dirty state
        "trials": trials,
    }


def save_raw(results: list[dict], out_path: Path) -> None:
    out_path.write_text(json.dumps({
        "generation_options": GENERATION_OPTIONS,
        "results": results,
    }, indent=2))
