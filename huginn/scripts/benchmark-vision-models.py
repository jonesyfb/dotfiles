#!/usr/bin/env python3
"""
Controlled, repeatable audition for local vision-model gatekeeper
candidates. Standalone — NOT wired into the daemon or v2/coordinator.py.
Never changes gatekeeper's configured model, Ollama service environment,
Runtime Context Engine policy, personality routing, or SYSTEM_PROMPT; it
only measures and reports.

Safety:
- Refuses to run while Huginn's game-mode flag is active (loading large
  models would fight a running game for VRAM).
- Warns (but does not block) if the GPU already shows significant
  competing activity before starting.
- Honors the same /tmp/ollama.lock cross-process contract Huginn and
  Garage Watch use, with a bounded acquisition deadline — never blocks
  forever, and can't silently run concurrently with either.
- Every screenshot is synthetic (scripts/vision_bench/corpus.py) — no real
  desktop content, so nothing sensitive can end up in results or logs.
- All inference is local Ollama calls only; nothing here ever reaches a
  cloud endpoint.

Usage:
  uv run --project .. python3 scripts/benchmark-vision-models.py gemma4:31b gemma4:e2b gemma4:e4b llava:7b
  uv run --project .. python3 scripts/benchmark-vision-models.py --warm-trials 2 gemma4:e2b

Outputs (written under scripts/vision_bench/results/<timestamp>/):
  raw.json       — every trial's full response, timing, and parsed verdict
  report.md      — human-readable comparison table + notes
  corpus/*.png   — the exact synthetic screenshots used (for reproducibility)
"""
import argparse
import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vision_bench import corpus, report, runner  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("models", nargs="*", default=["gemma4:31b"], help="Ollama model tags to benchmark")
    p.add_argument("--warm-trials", type=int, default=1, help="warm trials per non-first scenario (default 1)")
    p.add_argument("--timeout", type=float, default=180.0, help="per-request timeout in seconds (default 180)")
    p.add_argument("--force", action="store_true", help="proceed even if competing GPU activity is detected")
    return p.parse_args()


async def main() -> int:
    args = parse_args()

    try:
        runner.refuse_if_game_mode()
    except runner.GameModeActive as e:
        print(f"REFUSING TO RUN: {e}", file=sys.stderr)
        return 1

    warning = runner.competing_gpu_activity_warning()
    if warning:
        print(f"WARNING: {warning}", file=sys.stderr)
        if not args.force:
            print("Re-run with --force to proceed anyway, or wait until the GPU is idle.", file=sys.stderr)
            return 1

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(__file__).resolve().parent / "vision_bench" / "results" / timestamp
    corpus_dir = out_dir / "corpus"
    scenarios = corpus.build(corpus_dir)
    scenario_by_key = {s.key: s for s in scenarios}

    print(f"Auditioning {len(args.models)} model(s) against {len(scenarios)} scenarios "
          f"({args.warm_trials} warm trial(s) each), timeout={args.timeout}s.")
    print(f"Corpus written to {corpus_dir} (synthetic — no real desktop content).")

    results = []
    for model in args.models:
        print(f"\n=== {model} ===")
        start = time.monotonic()
        try:
            result = await runner.run_model(model, scenarios, args.timeout, args.warm_trials)
        except Exception as e:
            print(f"  FAILED: {e}")
            results.append({"model": model, "error": str(e), "trials": []})
            continue
        if result.get("error"):
            print(f"  FAILED: {result['error']}")
            results.append(result)
            continue
        elapsed = time.monotonic() - start
        n_trials = len(result["trials"])
        n_timeouts = sum(1 for t in result["trials"] if t["timed_out"])
        print(f"  {n_trials} trials in {elapsed:.0f}s, {n_timeouts} timeout(s)")
        results.append(result)

    out_dir.mkdir(parents=True, exist_ok=True)
    runner.save_raw(results, out_dir / "raw.json")

    scores = [report.score_model(r, scenario_by_key) for r in results if not r.get("error")]
    md = report.render_markdown(scores, scenarios) + "\n\n" + report.render_scenario_breakdown(results, scenarios)
    (out_dir / "report.md").write_text(md)

    print(f"\n{md}")
    print(f"\nRaw results: {out_dir / 'raw.json'}")
    print(f"Report: {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
