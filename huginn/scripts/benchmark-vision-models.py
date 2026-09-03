#!/usr/bin/env python3
"""
Standalone benchmark for auditioning local vision-model candidates for the
gatekeeper role — NOT wired into the daemon or the coordinator. Run this by
hand when considering a replacement for gemma4:31b; it does not select or
change anything on its own (HUGINN_CODEX_CLAUDE_PROMPT.md explicitly says
not to pick a replacement in this commit).

Uses the exact same GATE_PROMPT template and screenshot files gatekeeper.py
uses, so candidates are compared on identical inputs. For each candidate
model, records:
  - cold load latency (first request after an explicit unload)
  - warm generation latency (immediate second request)
  - memory split (total size vs size_vram, from /api/ps)
  - verdict validity (does the response parse as {"approved": bool, "message": str}?)
  - timeout rate over N trials at the configured deadline

Usage:
  uv run --project .. python3 scripts/benchmark-vision-models.py gemma4:31b llava:13b ...
  (with no args, benchmarks just gemma4:31b — the current model — as a baseline)
"""
import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "v2"))

import httpx  # noqa: E402

from config import GATE_PROMPT, GATE_QUEUE_DEADLINE_SECONDS, OLLAMA_BASE, SCREENS_DIR  # noqa: E402

TRIALS_PER_MODEL = 3


def _parse_verdict(raw: str) -> dict | None:
    """Same shape gatekeeper._parse_verdict expects — returns None if the
    response doesn't parse as a valid verdict at all (distinct from a
    successful parse that just says approved=False)."""
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        if "approved" not in data or "message" not in data:
            return None
        return {"approved": bool(data["approved"]), "message": str(data["message"])}
    except json.JSONDecodeError:
        return None


async def _unload(model: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(f"{OLLAMA_BASE}/api/generate", json={"model": model, "keep_alive": 0})
    except Exception:
        pass
    await asyncio.sleep(1)  # let Ollama actually free the VRAM before timing the next load


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


async def _judge_once(model: str, prompt: str, images_b64: list[str], timeout: float) -> tuple[bool, float, str]:
    """Returns (timed_out, elapsed_seconds, raw_response_or_error)."""
    message: dict = {"role": "user", "content": prompt}
    if images_b64:
        message["images"] = images_b64
    payload = {"model": model, "messages": [message], "stream": False}
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{OLLAMA_BASE}/api/chat", json=payload)
            r.raise_for_status()
            elapsed = time.monotonic() - start
            return False, elapsed, r.json().get("message", {}).get("content", "")
    except httpx.TimeoutException:
        return True, time.monotonic() - start, ""
    except Exception as e:
        return False, time.monotonic() - start, f"ERROR: {e}"


def _sample_prompt() -> str:
    return GATE_PROMPT.format(
        target="steam",
        activity_summary="- zed (main.py — editing): ~34m\n- brave-browser (docs): ~12m",
        discord_status="no",
        recent_verdicts="(no prior verdicts)",
    )


def _sample_images_b64(limit: int = 3) -> list[str]:
    import base64
    paths = sorted(SCREENS_DIR.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return [base64.b64encode(p.read_bytes()).decode() for p in paths]


async def benchmark_model(model: str, prompt: str, images_b64: list[str]) -> dict:
    print(f"\n=== {model} ===")
    await _unload(model)

    cold_timed_out, cold_elapsed, _cold_raw = await _judge_once(model, prompt, images_b64, GATE_QUEUE_DEADLINE_SECONDS)
    residency = await _residency(model)
    print(f"cold: {'TIMEOUT' if cold_timed_out else f'{cold_elapsed:.1f}s'}")
    if residency:
        total = residency.get("size", 0) / 1e9
        vram = residency.get("size_vram", 0) / 1e9
        print(f"memory split: {vram:.1f}GB VRAM / {total:.1f}GB total ({100*vram/total:.0f}% GPU)" if total else "memory split: unknown")

    warm_results = []
    timeouts = 0
    for i in range(TRIALS_PER_MODEL):
        timed_out, elapsed, raw = await _judge_once(model, prompt, images_b64, GATE_QUEUE_DEADLINE_SECONDS)
        if timed_out:
            timeouts += 1
            print(f"warm trial {i+1}: TIMEOUT (> {GATE_QUEUE_DEADLINE_SECONDS}s)")
            continue
        verdict = _parse_verdict(raw)
        valid = verdict is not None
        warm_results.append(elapsed)
        print(f"warm trial {i+1}: {elapsed:.1f}s, verdict_valid={valid}" + (f", approved={verdict['approved']}" if valid else f", raw={raw[:80]!r}"))

    return {
        "model": model,
        "cold_seconds": None if cold_timed_out else round(cold_elapsed, 1),
        "cold_timed_out": cold_timed_out,
        "warm_seconds_avg": round(sum(warm_results) / len(warm_results), 1) if warm_results else None,
        "vram_gb": round(residency.get("size_vram", 0) / 1e9, 1) if residency else None,
        "total_gb": round(residency.get("size", 0) / 1e9, 1) if residency else None,
        "timeout_rate": f"{timeouts}/{TRIALS_PER_MODEL}",
    }


async def main() -> None:
    models = sys.argv[1:] or ["gemma4:31b"]
    prompt = _sample_prompt()
    images_b64 = _sample_images_b64()
    print(f"Benchmarking {len(models)} model(s) with {len(images_b64)} real screenshot(s) attached, "
          f"deadline={GATE_QUEUE_DEADLINE_SECONDS}s, {TRIALS_PER_MODEL} warm trials each.")

    results = []
    for model in models:
        results.append(await benchmark_model(model, prompt, images_b64))
        await _unload(model)  # leave a clean slate for the next candidate / for real use afterward

    print("\n=== Summary ===")
    print(f"{'model':<20} {'cold':>8} {'warm avg':>10} {'vram/total':>14} {'timeouts':>10}")
    for r in results:
        cold = "TIMEOUT" if r["cold_timed_out"] else f"{r['cold_seconds']}s"
        warm = f"{r['warm_seconds_avg']}s" if r["warm_seconds_avg"] is not None else "n/a"
        mem = f"{r['vram_gb']}/{r['total_gb']}GB" if r["vram_gb"] is not None else "unknown"
        print(f"{r['model']:<20} {cold:>8} {warm:>10} {mem:>14} {r['timeout_rate']:>10}")


if __name__ == "__main__":
    asyncio.run(main())
