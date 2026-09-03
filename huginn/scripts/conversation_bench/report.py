"""
Blind-report generation for the direct-conversation model audition.

Three outputs, deliberately separated:
- mapping.json      — candidate letter -> real Ollama model tag (NOT shown in the other two files)
- report_blind.md   — exact prompts/outputs only, keyed by candidate letter, NO timing anywhere
- report_performance.md — latency/tokens/VRAM, keyed by the SAME candidate letters

Judge report_blind.md before report_performance.md — that ordering is the
point of splitting them.
"""
from __future__ import annotations

import random
from collections import defaultdict

from .scenarios import SCENARIOS, Scenario

LETTERS = ["A", "B", "C", "D"]


def build_mapping(candidates: list[str], seed: int = 1234) -> dict[str, str]:
    """Deterministic (documented seed) but not identity-revealing order."""
    shuffled = list(candidates)
    random.Random(seed).shuffle(shuffled)
    return {LETTERS[i]: model for i, model in enumerate(shuffled)}


def _percentile(values: list[float], p: float) -> "float | None":
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return round(s[f], 2)
    return round(s[f] + (s[c] - s[f]) * (k - f), 2)


def _turns_for(result: dict, scenario_key: str) -> list[dict]:
    return [t for t in result["turns"] if t["scenario_key"] == scenario_key]


def render_blind_report(results_by_letter: dict[str, dict]) -> str:
    out = ["# Direct-conversation model audition — BLIND prose report\n"]
    out.append(
        "Candidates are labeled A-D only; see mapping.json (kept separate) for real model "
        "identity. No timing/performance data appears in this file — see "
        "report_performance.md, also letter-keyed, for that. Same system prompt "
        "(config.DIRECT_SOCIAL_SYSTEM_PROMPT, unmodified), same entity lens, same fixed "
        "sampling settings, think:false, no tools, no production memory, no cloud models, "
        "for every candidate — see scripts/conversation_bench/scenarios.py and runner.py "
        "for the exact harness.\n"
    )

    by_category: dict[str, list[Scenario]] = defaultdict(list)
    for s in SCENARIOS:
        by_category[s.category].append(s)

    category_names = {
        "A": "Identity", "B": "Entity embodiment", "C": "Banter and presence",
        "D": "Loyalty without therapy", "E": "Procrastination boundaries",
        "F": "Epistemic restraint", "G": "Multi-turn transitions",
    }

    for cat in "ABCDEFG":
        if cat not in by_category:
            continue
        out.append(f"\n## Category {cat}: {category_names[cat]}\n")
        for scenario in by_category[cat]:
            out.append(f"\n### `{scenario.key}`\n")
            if scenario.notes:
                out.append(f"_{scenario.notes}_\n")
            if scenario.pre_history:
                out.append("Injected prior history:")
                for h in scenario.pre_history:
                    who = "Nathan" if h["role"] == "user" else "Huginn"
                    out.append(f"> {who}: {h['content']}")
                out.append("")

            for letter in sorted(results_by_letter):
                result = results_by_letter[letter]
                turns = _turns_for(result, scenario.key)
                if not turns:
                    continue
                out.append(f"\n**Candidate {letter}**\n")
                run_labels = sorted(set(t["run_label"] for t in turns), key=lambda x: (x.rsplit("_", 1)[0], int(x.rsplit("_", 1)[1])))
                for run_label in run_labels:
                    run_turns = sorted([t for t in turns if t["run_label"] == run_label], key=lambda t: t["turn_index"])
                    if scenario.is_multi_turn:
                        out.append(f"- {run_label}:")
                        for t in run_turns:
                            resp = t["response"] or f"[ERROR: {t['error']}]"
                            out.append(f"  - Nathan: {t['message']!r} -> Huginn: {resp!r}")
                    else:
                        t = run_turns[0]
                        resp = t["response"] or f"[ERROR: {t['error']}]"
                        out.append(f"- {run_label}: {resp!r}")
    return "\n".join(out)


def render_performance_report(results_by_letter: dict[str, dict]) -> str:
    out = ["# Direct-conversation model audition — performance report (letter-keyed)\n"]
    out.append(
        "Same candidate letters as report_blind.md and mapping.json. Judge prose quality "
        "in report_blind.md BEFORE reading this file.\n"
    )
    out.append("| Candidate | Total calls | Errors | Cold load (s) | Warm p50 (s) | Warm p95 (s) | Avg tok/s | VRAM resident (GB) |")
    out.append("|---|---|---|---|---|---|---|---|")

    for letter in sorted(results_by_letter):
        result = results_by_letter[letter]
        turns = result["turns"]
        errors = sum(1 for t in turns if t["error"])
        cold = turns[0]["load_duration_seconds"] if turns else None
        warm_elapsed = [t["elapsed_seconds"] for t in turns[1:] if t["elapsed_seconds"] is not None and not t["error"]]
        p50 = _percentile(warm_elapsed, 0.5)
        p95 = _percentile(warm_elapsed, 0.95)
        tok_rates = [
            t["eval_count"] / t["eval_duration_seconds"]
            for t in turns
            if t["eval_count"] and t["eval_duration_seconds"]
        ]
        avg_tok_s = round(sum(tok_rates) / len(tok_rates), 1) if tok_rates else None
        residency = result.get("residency_first_call") or result.get("residency_session_end")
        vram_gb = round(residency["size_vram"] / (1024 ** 3), 2) if residency and residency.get("size_vram") else None
        out.append(
            f"| {letter} | {len(turns)} | {errors} | {cold if cold is not None else 'n/a'} | "
            f"{p50 if p50 is not None else 'n/a'} | {p95 if p95 is not None else 'n/a'} | "
            f"{avg_tok_s if avg_tok_s is not None else 'n/a'} | {vram_gb if vram_gb is not None else 'n/a'} |"
        )

    out.append(
        "\nCold load = load_duration on each candidate's first call this session (model was "
        "explicitly unloaded beforehand, so this is a true cold load). Warm p50/p95 exclude "
        "that first call. Avg tok/s = eval_count / eval_duration averaged over all successful "
        "calls. VRAM resident is read once via /api/ps; note OLLAMA_MAX_LOADED_MODELS=1 means "
        "only one candidate is ever resident at a time even during this benchmark."
    )
    return "\n".join(out)
