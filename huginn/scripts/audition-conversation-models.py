#!/usr/bin/env python3
"""
Blind direct-conversation model audition — qwen3.5:4b vs. larger already-
installed candidates, to decide whether a bigger model should become the
normal direct-social model. Standalone, does not touch production
routing/prompts/validators/entities registry/gatekeeper/tools/action
provenance/coordinator scheduling/Ollama config/TTS — see
scripts/conversation_bench/runner.py's module docstring for the exact
fairness contract.

Usage:
  uv run --project .. python3 scripts/audition-conversation-models.py
  uv run --project .. python3 scripts/audition-conversation-models.py --models qwen3.5:4b qwen3.5:9b

Outputs under scripts/conversation_bench/results/<timestamp>/:
  mapping.json             — candidate letter -> real model tag (read this LAST)
  report_blind.md          — exact prompts/outputs, letter-keyed, no timing
  report_performance.md    — latency/tokens/VRAM, letter-keyed
  raw.json                 — full machine-readable results, real model names
"""
import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from conversation_bench import report, runner  # noqa: E402
from conversation_bench.scenarios import MULTI_TURN_RUNS, SCENARIOS, SEEDS, SINGLE_TURN_TRIALS  # noqa: E402

DEFAULT_MODELS = ["qwen3.5:4b", "qwen3.5:9b", "qwen3.8:27b", "gemma4:31b"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--models", nargs="*", default=DEFAULT_MODELS, help="Ollama model tags to audition")
    p.add_argument("--mapping-seed", type=int, default=1234, help="seed for the letter<->model shuffle (default 1234)")
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

    db_path = runner.use_isolated_memory()
    print(f"Isolated benchmark sqlite: {db_path} (production DB never touched)")

    n_single = sum(1 for s in SCENARIOS if not s.is_multi_turn)
    n_multi = sum(1 for s in SCENARIOS if s.is_multi_turn)
    print(
        f"Auditioning {len(args.models)} model(s) against {len(SCENARIOS)} scenarios "
        f"({n_single} single-turn x {SINGLE_TURN_TRIALS} trials, {n_multi} multi-turn x "
        f"{MULTI_TURN_RUNS} full runs), seeds={SEEDS}."
    )

    results_by_model: dict[str, dict] = {}
    for model in args.models:
        print(f"\n=== {model} ===")
        start = time.monotonic()
        result = await runner.run_candidate(model, SCENARIOS)
        elapsed = time.monotonic() - start
        if result.get("error"):
            print(f"  FAILED: {result['error']}")
        else:
            n_turns = len(result["turns"])
            n_errors = sum(1 for t in result["turns"] if t["error"])
            print(f"  {n_turns} turns in {elapsed:.0f}s, {n_errors} error(s)")
        results_by_model[model] = result

    mapping = report.build_mapping(args.models, seed=args.mapping_seed)
    reverse = {v: k for k, v in mapping.items()}
    results_by_letter = {reverse[model]: result for model, result in results_by_model.items() if model in reverse}

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(__file__).resolve().parent / "conversation_bench" / "results" / timestamp
    out_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "mapping.json").write_text(json.dumps({
        "mapping": mapping, "mapping_seed": args.mapping_seed,
        "note": "candidate letter -> real Ollama model tag. Read AFTER judging report_blind.md.",
    }, indent=2))

    (out_dir / "raw.json").write_text(json.dumps(results_by_model, indent=2, default=str))

    (out_dir / "report_blind.md").write_text(report.render_blind_report(results_by_letter))
    (out_dir / "report_performance.md").write_text(report.render_performance_report(results_by_letter))

    print(f"\nWrote: {out_dir}")
    print("  mapping.json (candidate letter -> real model — read LAST)")
    print("  report_blind.md (judge this FIRST)")
    print("  report_performance.md")
    print("  raw.json")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
