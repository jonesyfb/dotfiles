#!/usr/bin/env python3
"""Direct-social routing acceptance eval — single baseline run.

Exercises the real intent classifier, real entity lens, real
personality.render_direct_social() through the real coordinator and real
qwen3.5:4b, and real daemon.handle_chat() routing — against the actual
sqlite history the daemon uses (a temp copy, not production data). No
production code is modified by running this.

Per instruction: run once, report, do not repeatedly tune while observing.

Writes report.json + report.md to scripts/direct_chat_bench/results/<ts>/.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

V2_DIR = Path(__file__).resolve().parent.parent / "v2"
sys.path.insert(0, str(V2_DIR))

RESULTS_DIR = Path(__file__).resolve().parent / "direct_chat_bench" / "results"


class _CaptureWriter:
    def __init__(self):
        self.chunks: list[dict] = []

    def write(self, data: bytes) -> None:
        for line in data.decode().splitlines():
            if line.strip():
                self.chunks.append(json.loads(line))

    async def drain(self) -> None:
        pass

    def full_text(self) -> str:
        return "".join(c.get("content", "") for c in self.chunks if c.get("type") == "token")

    def event_types(self) -> list[str]:
        return [c["type"] for c in self.chunks]


async def run_one(daemon, intent, entities, content: str) -> dict:
    decision = intent.classify(content)
    mentions = entities.extract_mentions(content)

    writer = _CaptureWriter()
    t0 = time.monotonic()
    await daemon.handle_chat(writer, content)
    elapsed = time.monotonic() - t0

    used_direct_social = writer.event_types().count("token") <= 1 and "thinking" not in writer.event_types()
    return {
        "input": content,
        "intent": decision.intent.value,
        "high_confidence": decision.high_confidence,
        "intent_reason": decision.reason,
        "entity_mentions": mentions,
        "likely_used_direct_social_path": used_direct_social,
        "response": writer.full_text(),
        "event_types": writer.event_types(),
        "elapsed_s": round(elapsed, 3),
    }


SINGLE_TURN = [
    "Morning.",
    "What do you think of Brave?",
    "The Lion is getting fat again.",
    "Are you actually useful?",
    "I'm bored.",
    "I keep failing at learning game development.",
    "Close it.",
    "Add milk to my calendar tomorrow.",
    "Why is my computer stuttering?",
    "Explain monads.",
    "Remember that my favorite color is green.",
    "What was the restaurant client I mentioned?",
    "I'm watching YouTube because I'm done working.",
    "You're being annoying. Leave me alone for an hour.",
]


async def main() -> None:
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = RESULTS_DIR / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    # Isolated sqlite copy (outside out_dir, so it's never accidentally
    # committed alongside the report) so this eval never touches production
    # history/facts.
    import config
    import tempfile
    real_db = config.DB_PATH
    temp_db = Path(tempfile.mkdtemp(prefix="huginn-direct-chat-eval-")) / "eval.db"
    config.DB_PATH = temp_db

    import daemon, intent, entities, memory  # noqa: E402  (import after DB_PATH patch)
    memory.DB_PATH = temp_db

    report = {"timestamp": ts, "single_turn": [], "multi_turn": {}}

    for content in SINGLE_TURN:
        memory.clear_history()
        res = await run_one(daemon, intent, entities, content)
        report["single_turn"].append(res)
        print(f"{content!r} -> intent={res['intent']} conf={res['high_confidence']} "
              f"direct_social={res['likely_used_direct_social_path']} {res['elapsed_s']}s")
        print(f"    {res['response']!r}")

    # ── Multi-turn scenarios ────────────────────────────────────────────────
    async def run_turn(label, content):
        res = await run_one(daemon, intent, entities, content)
        print(f"  [{label}] {content!r} -> intent={res['intent']} direct_social={res['likely_used_direct_social_path']}")
        print(f"      {res['response']!r}")
        return res

    multi = {}

    memory.clear_history()
    banter = []
    for msg in ["Hey.", "Not much going on today.", "You ever get bored watching me work?",
                "Probably not, you're a raven.", "Fair enough.", "Anyway, what's up with Docker?"]:
        banter.append(await run_turn("banter", msg))
    multi["casual_banter_becoming_factual"] = banter

    memory.clear_history()
    tool_turn = []
    tool_turn.append(await run_turn("joke->tool", "Brave's really been hogging memory lately."))
    tool_turn.append(await run_turn("joke->tool", "Actually close it for me."))
    multi["social_becoming_tool_request"] = tool_turn

    memory.clear_history()
    correction = []
    correction.append(await run_turn("mention", "Docker's being a whale again, taking forever to build."))
    correction.append(await run_turn("correction", "I don't think of Docker as a whale, more like an octopus with too many arms."))
    multi["user_corrects_entity_interpretation"] = correction

    memory.clear_history()
    dismiss = []
    dismiss.append(await run_turn("nudge-ish", "Yeah I've been on YouTube most of the afternoon."))
    dismiss.append(await run_turn("dismiss", "Drop it, I don't want to hear it right now."))
    multi["user_dismisses_teasing"] = dismiss

    memory.clear_history()
    unresolved_it = []
    unresolved_it.append(await run_turn("no-antecedent", "It's doing that thing again."))
    multi["unresolvable_it_reference"] = unresolved_it

    memory.clear_history()
    game_mode_flag = Path(config.GAME_MODE_FLAG)
    game_mode_flag.parent.mkdir(parents=True, exist_ok=True)
    game_mode_flag.touch()
    try:
        game_mode_runs = []
        game_mode_runs.append(await run_turn("game-mode social", "Morning."))
        game_mode_runs.append(await run_turn("game-mode factual", "Explain monads."))
        multi["game_mode_permits_social_blocks_reasoning"] = game_mode_runs
    finally:
        game_mode_flag.unlink(missing_ok=True)

    report["multi_turn"] = multi

    with open(out_dir / "report.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    config.DB_PATH = real_db
    print(f"\nwrote {out_dir / 'report.json'}")
    print(out_dir)


if __name__ == "__main__":
    asyncio.run(main())
