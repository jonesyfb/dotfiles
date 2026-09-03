#!/usr/bin/env python3
"""
Direct-social hardening acceptance run — re-runs the committed baseline
(scripts/direct_chat_bench/results/20260903T080001Z/) against the
post-hardening code, plus the new scenario categories this round added
(identity kernel, present-state traps, capability questions,
procrastination authorization, entity correction/restore, dismissal,
repetition, and routing categories 6/7/8/9). Talks to the REAL running
daemon over its Unix socket — no mocking. Classification metadata
(intent/subtype) is computed in-process via the same intent.py the
daemon uses, so the report can show routing decisions alongside the
live model output.

Usage: uv run python scripts/direct_chat_bench/run.py
Requires: `systemctl --user restart huginn` first, on a clean/expected
sqlite history (the script calls `clear` between scenarios itself).
"""
import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "v2"))

import entities  # noqa: E402
import intent  # noqa: E402

SOCKET_PATH = str(Path.home() / ".local/share/huginn/huginn.sock")
OUT_DIR = Path(__file__).resolve().parent / "results" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def send_chat(content: str) -> tuple[str, float, list[dict]]:
    t0 = time.monotonic()
    events: list[dict] = []
    text_parts: list[str] = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(SOCKET_PATH)
        s.sendall((json.dumps({"type": "chat", "content": content, "tts": False}) + "\n").encode())
        buf = ""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk.decode()
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                events.append(obj)
                if obj.get("type") == "token":
                    text_parts.append(obj.get("content", ""))
                elif obj.get("type") == "confirm_required":
                    text_parts.append(f"[confirm_required: {obj.get('tool')} {obj.get('args')}]")
                elif obj.get("type") == "tool_call":
                    text_parts.append(f"[tool_call: {obj.get('tool')} {obj.get('args')}]")
                if obj.get("type") == "done":
                    return "".join(text_parts), time.monotonic() - t0, events
    return "".join(text_parts), time.monotonic() - t0, events


def clear():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(SOCKET_PATH)
        s.sendall((json.dumps({"type": "clear"}) + "\n").encode())
        s.recv(4096)


def classify_row(content: str) -> str:
    d = intent.classify(content)
    if d.intent == intent.IntentClass.SOCIAL_DIRECT and d.high_confidence:
        mentions = entities.extract_mentions(content)
        subtype = intent.classify_social_subtype(content, mentions)
        return f"`social_direct`/`{subtype.value}`"
    return f"`{d.intent.value}` ({d.reason})"


def run_single_turns(f, inputs: list[str]):
    f.write("| Input | Classification | Latency | Response |\n|---|---|---|---|\n")
    for text in inputs:
        clear()
        resp, latency, _ = send_chat(text)
        row_class = classify_row(text)
        resp_escaped = resp.replace("|", "\\|").replace("\n", " ")
        f.write(f"| {text!r} | {row_class} | {latency:.2f}s | {resp_escaped!r} |\n")


def run_scenario(f, name: str, turns: list[str]):
    f.write(f"\n### {name}\n\n")
    clear()
    for text in turns:
        resp, latency, _ = send_chat(text)
        row_class = classify_row(text)
        f.write(f"- **{text!r}** -> {row_class} ({latency:.2f}s): {resp!r}\n")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    report = OUT_DIR / "report.md"
    with report.open("w") as f:
        f.write("# Direct-social hardening acceptance report\n\n")
        f.write(
            f"Run: `{OUT_DIR.name}` — post-hardening code (intent subtypes, "
            "entity correction/restore, present-state/procrastination/"
            "capability/repetition validators, identity-kernel prompt, "
            "generation-time token limits). Real running daemon, real "
            "qwen3.5:4b for SOCIAL_DIRECT, real route_model()/stream_chat() "
            "tool loop for everything else. Compare against "
            "`../20260903T080001Z/report.md` (pre-hardening baseline).\n\n"
        )

        f.write("## Original baseline single-turn inputs (re-run)\n\n")
        run_single_turns(f, [
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
        ])

        f.write("\n## Identity-kernel questions\n\n")
        run_single_turns(f, [
            "You're a raven.",
            "Who are you?",
            "Are you an AI?",
            "Where do you live?",
            "Who is Muninn?",
        ])

        f.write("\n## Unsupported-current-state traps\n\n")
        run_single_turns(f, [
            "Morning.",
            "How long have I been at this?",
            "You've probably had way too much coffee by now.",
        ])

        f.write("\n## Capability questions\n\n")
        run_single_turns(f, [
            "Are you actually useful?",
            "What can you actually do?",
        ])

        f.write("\n## Entity-lens naturalness\n\n")
        run_single_turns(f, [
            "What do you think of the Lion?",
            "What do you think of Docker?",
            "What do you think of Thunderbird?",
            "What do you think of Discord?",
            "Parity keeps crashing.",
        ])

        f.write("\n## Colloquial factual routing\n\n")
        run_single_turns(f, [
            "What's up with Docker?",
            "What do you think of Docker?",
            "Close Docker.",
            "Docker.",
        ])

        f.write("\n## Dismissal handling\n\n")
        run_single_turns(f, [
            "Drop it.",
            "Enough.",
            "Not now.",
            "Leave me alone.",
            "Leave me alone for an hour.",
        ])

        f.write("\n## Multi-turn scenarios\n\n")
        run_scenario(f, "entity_correction_then_later_reference_then_restore", [
            "I think of Docker as an octopus, not a whale.",
            "What do you think of Docker?",
            "Never mind, go back to the original for Docker.",
            "What do you think of Docker?",
        ])
        run_scenario(f, "dismissal_then_re_tease", [
            "You're being pretty grim today.",
            "Drop it.",
            "You're a raven.",
        ])
        run_scenario(f, "repeated_acknowledgements", [
            "Probably not, you're a raven.",
            "Fair enough.",
            "Fair enough.",
        ])
        run_scenario(f, "social_then_factual_then_social", [
            "Hey.",
            "What's up with Docker?",
            "Anyway, night owl huh?",
        ])
        run_scenario(f, "social_then_action_via_buffered_integrity_path", [
            "Brave's really been hogging memory lately.",
            "Remember that my favorite food is tacos.",
        ])
        run_scenario(f, "ten_turn_conversation", [
            "Morning.",
            "What do you think of Brave?",
            "I've got that browser open.",
            "What's up with Docker?",
            "I think of Docker as an octopus, not a whale.",
            "Are you actually useful?",
            "I keep failing at learning game development.",
            "Drop it.",
            "You're a raven.",
            "Fair enough.",
        ])

        f.write("\n## Notes\n\n")
        f.write(
            "- Classification column shows intent.classify() (+ classify_social_subtype() "
            "when SOCIAL_DIRECT) computed in-process against the same intent.py the daemon "
            "imports — not parsed from logs.\n"
            "- `procrastination_nudge_authorized=True` and generation-time token-limit "
            "behavior are exercised by `v2/tests/test_direct_social_hardening.py` "
            "(mocked-boundary, deterministic) rather than this live run, since "
            "daemon.handle_direct_social has no live trigger for authorization yet "
            "(documented as intentional — no deterministic nudge-authorization feed exists "
            "for direct chat in this slice).\n"
        )

    print(f"wrote {report}")


if __name__ == "__main__":
    main()
