#!/usr/bin/env python3
"""Personality renderer acceptance test — semantic-safety hardening round.

Exercises v2/personality.py's real render() against the real coordinator
and real qwen3.5:4b (think=false), same code path production uses. No
production code or config is touched by running this. Facts used in every
scenario are synthetic (invented for this test), not pulled from live
desktop state.

Writes raw.json + report.md to scripts/personality_bench/results/<ts>/.
Read-only with respect to the repo; does not commit anything.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
from dataclasses import asdict
from pathlib import Path

V2_DIR = Path(__file__).resolve().parent.parent / "v2"
sys.path.insert(0, str(V2_DIR))

import config  # noqa: E402
import ambient  # noqa: E402
import context as ctx  # noqa: E402
import llm as llm_module  # noqa: E402
import personality  # noqa: E402
from coordinator import Purpose  # noqa: E402
from personality import PersonalityRequest  # noqa: E402

import httpx  # noqa: E402

RESULTS_DIR = Path(__file__).resolve().parent / "personality_bench" / "results"

# ── instrument the raw Ollama call to capture token/duration metrics ──────────
_raw_capture: list[dict] = []


async def _instrumented_raw(system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": config.MODELS["personality"]["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "think": False,
        "options": {"temperature": 0.7},
    }
    async with httpx.AsyncClient(timeout=config.PERSONALITY_RENDER_TIMEOUT_SECONDS) as client:
        r = await client.post(f"{config.OLLAMA_BASE}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()
        content = data.get("message", {}).get("content", "")
        _raw_capture.append({
            "response": content,
            "total_duration_s": data.get("total_duration", 0) / 1e9,
            "load_duration_s": data.get("load_duration", 0) / 1e9,
            "prompt_eval_count": data.get("prompt_eval_count"),
            "eval_count": data.get("eval_count"),
            "eval_duration_s": data.get("eval_duration", 0) / 1e9,
        })
        return content


llm_module._render_personality_raw = _instrumented_raw

_log_records: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _log_records.append(record.getMessage())


personality.log.addHandler(_Capture())
personality.log.setLevel(logging.INFO)


def _ollama_ps() -> dict:
    try:
        return httpx.get(f"{config.OLLAMA_BASE}/api/ps", timeout=5).json()
    except Exception as e:
        return {"error": str(e)}


# ── semantic-contradiction scan (mirrors personality._validate_flavor's
# checks, applied to the report's own analysis independent of production
# code, so the report can catch a regression even if validate() itself
# had a bug) ────────────────────────────────────────────────────────────
_BANNED_ACTION_VERBS = personality._BANNED_ACTION_VERBS


def _semantic_scan(flavor: "str | None") -> list[str]:
    if not flavor:
        return []
    low = flavor.lower()
    hits = []
    for word in _BANNED_ACTION_VERBS:
        if re.search(rf"\b{re.escape(word)}\b", low):
            hits.append(f"banned_verb:{word}")
    if any(ch.isdigit() for ch in flavor):
        hits.append("contains_digit")
    if any(m in flavor for m in ("*", "[", "]")):
        hits.append("theatrical_marker")
    if flavor.strip().lower().startswith("huginn:"):
        hits.append("self_prefix")
    return hits


async def _run_render(request: PersonalityRequest, *, purpose: Purpose = Purpose.AMBIENT) -> dict:
    _log_records.clear()
    before = len(_raw_capture)
    t0 = time.monotonic()
    result = await personality.render(request, purpose=purpose)
    elapsed = time.monotonic() - t0
    attempts_seen = [int(m.group(1)) for msg in _log_records for m in [re.search(r"attempt=(\d+)", msg)] if m]
    retries = max(attempts_seen) if attempts_seen else 0
    problems = [m.group(1) for msg in _log_records for m in [re.search(r"problem=(\S+)", msg)] if m]
    raw_metrics = _raw_capture[before:]

    protected_present = all(str(v) in (result.deterministic or "") for v in request.facts.values()) if request.facts else True
    missing_protected = [str(v) for v in request.facts.values() if str(v) not in (result.deterministic or "")]

    return {
        "event_family": request.event_family,
        "ok": result.ok,
        "flavor": result.flavor,
        "deterministic": result.deterministic,
        "text": result.text,
        "reason": result.reason,
        "flavor_allowed": request.event_family in personality._FLAVOR_ELIGIBLE_FAMILIES,
        "retries": retries,
        "validation_problems": problems,
        "elapsed_s": round(elapsed, 3),
        "deadline_reached": elapsed >= config.AMBIENT_RENDER_DEADLINE_SECONDS,
        "raw_metrics": raw_metrics,
        "protected_values_verified": protected_present,
        "missing_protected_values": missing_protected,
        "semantic_contradictions": _semantic_scan(result.flavor),
    }


# ── acceptance scenarios, one per event family ─────────────────────────────────

SCENARIOS = [
    dict(name="01_brave_elevated", request=PersonalityRequest(
        purpose="periodic_observation", event_family="resource_observation",
        facts={"app": "Brave", "memory_gb": "9.4", "tabs": "38"}, severity="notice",
        flavor_cues={"subject": "a browser", "band": "elevated", "category": "browser"},
        prohibited_additions=("diagnosis", "recommendation", "urgency"),
    )),
    dict(name="03_thunderbird_nonurgent", request=PersonalityRequest(
        purpose="periodic_observation", event_family="resource_observation",
        facts={"app": "Thunderbird", "new_messages": "14"}, severity="info",
        flavor_cues={"subject": "the mail client", "band": "normal", "category": "mail"},
    )),
    dict(name="04_docker_unusual", request=PersonalityRequest(
        purpose="periodic_observation", event_family="resource_observation",
        facts={"process": "Docker", "cpu_percent": "81", "mem_gb": "6.2"}, severity="notice",
        flavor_cues={"subject": "a container runtime", "band": "elevated", "category": "background_service"},
    )),
    dict(name="05_discord_burst", request=PersonalityRequest(
        purpose="periodic_observation", event_family="resource_observation",
        facts={"app": "Discord", "notification_count": "47", "window_minutes": "10"}, severity="notice",
        flavor_cues={"subject": "a chat app", "band": "elevated", "category": "chat"},
    )),
    dict(name="06_badger_literal_creature", request=PersonalityRequest(
        purpose="periodic_observation", event_family="resource_observation",
        facts={"app": "Badger", "cpu_percent": "34"}, severity="info",
        flavor_cues={"subject": "an unfamiliar app", "band": "normal", "category": "unknown_app", "creature_hint": "badger"},
    )),
    dict(name="07_parity_neutral_name", request=PersonalityRequest(
        purpose="periodic_observation", event_family="resource_observation",
        facts={"app": "Parity", "cpu_percent": "29"}, severity="info",
        flavor_cues={"subject": "an unfamiliar app", "band": "normal", "category": "unknown_app", "creature_hint": "none - neutral name, do not force an animal"},
    )),
    dict(name="08_office_laser_offline", request=PersonalityRequest(
        purpose="device_offline", event_family="device_unavailable",
        facts={"device": "Office-Laser", "status": "offline", "cause": "unknown"}, severity="notice",
    )),
    dict(name="09_disk_temp_critical", request=PersonalityRequest(
        purpose="resource_critical", event_family="critical_threshold",
        facts={"metric": "disk temperature", "value": "78", "unit": " C"}, severity="critical",
    )),
    dict(name="10_nixos_rebuild_failed", request=PersonalityRequest(
        purpose="bash_event", event_family="command_failed",
        facts={"command": "nixos-rebuild switch", "attempts": "2", "error": "flake.nix: No such file or directory"},
        severity="notice",
    )),
    dict(name="11_rsync_not_executed", request=PersonalityRequest(
        purpose="action_not_executed", event_family="command_not_executed",
        facts={"reason": "/mnt/archive is not mounted", "planned_command": "rsync -a ~/notes/ /mnt/archive/notes/"},
        severity="notice",
    )),
    dict(name="12_reasoner_conclusion_uncertain", request=PersonalityRequest(
        purpose="diagnostic_conclusion", event_family="reasoner_conclusion",
        reasoner_conclusion=(
            "Process 481 is using 92% CPU because its indexing loop is probably "
            "retrying a failed operation, but available logs do not prove this conclusively."
        ),
        severity="notice",
    )),
    dict(name="13_calendar_unavailable", request=PersonalityRequest(
        purpose="capability_unavailable", event_family="capability_unavailable",
        facts={"capability": "calendar", "requested_action": "add milk tomorrow"}, severity="info",
    )),
    dict(name="14_ambiguous_close_it", request=PersonalityRequest(
        purpose="clarification_needed", event_family="clarification_needed",
        facts={"request": "close it", "open_applications": "Brave, Thunderbird, a terminal, Discord"},
        severity="info",
    )),
    dict(name="15_procrastination_nudge", request=PersonalityRequest(
        purpose="procrastination_nudge", event_family="procrastination_nudge",
        facts={"requested_next_step": "open the report and write one sentence"}, severity="notice",
        flavor_cues={"subject": "a video queue", "tone": "teasing", "category": "procrastination"},
        max_sentences=2,
    )),
    dict(name="17_command_finished_slow", request=PersonalityRequest(
        purpose="bash_event", event_family="command_slow",
        facts={"command": "cargo build --release", "elapsed_seconds": "45"}, severity="info",
        flavor_cues={"category": "shell_command", "band": "normal"},
    )),
    dict(name="19_task_succeeded", request=PersonalityRequest(
        purpose="task_complete", event_family="task_succeeded",
        facts={"task": "nightly-backup", "duration_seconds": "142", "result_preview": "4.2GB written"},
        severity="info",
        flavor_cues={"subject": "a background task", "category": "backup", "transition": "completed"},
    )),
    dict(name="20_task_failed_unknown", request=PersonalityRequest(
        purpose="task_failed", event_family="task_failed",
        facts={"task": "db-migrate", "error": "connection refused: 127.0.0.1:5432", "diagnosis": "unknown"},
        severity="notice",
    )),
]

# Item 4: the vulnerable-statement scenario ("Nathan says failed code makes
# him feel incapable") is explicitly OUT of scope for this renderer — it is
# a future direct-chat personality test, not ambient/notification rendering,
# and must not be scored against this module. Documented, not rendered.
EXCLUDED_SCENARIOS = {
    "18_incapable_statement": (
        "Moved to the future direct-chat personality backlog per this "
        "round's feedback item 4 — this renderer only ever produces a short "
        "mood clause plus a deterministic factual sentence for a discrete "
        "event; it has no conversational turn-taking and should not be "
        "asked to carry a supportive response to a personal disclosure. "
        "Not rendered, not scored, here."
    ),
}

BRAVE_REQUEST = SCENARIOS[0]["request"]

SEQUENTIAL_BROWSER_REQUESTS = [
    PersonalityRequest(
        purpose="periodic_observation", event_family="resource_observation",
        facts={"app": "Brave", "memory_gb": "9.4", "tabs": "38"}, severity="notice",
        flavor_cues={"subject": "a browser", "band": "elevated", "category": "browser"},
    ),
    PersonalityRequest(
        purpose="periodic_observation", event_family="resource_observation",
        facts={"app": "Brave", "tabs": "51"}, severity="notice",
        flavor_cues={"subject": "a browser", "band": "elevated", "category": "browser", "transition": "climbing"},
    ),
    PersonalityRequest(
        purpose="periodic_observation", event_family="resource_observation",
        facts={"app": "Brave", "cpu_percent": "63"}, severity="notice",
        flavor_cues={"subject": "a browser", "band": "elevated", "category": "browser"},
    ),
]


# ── no-renderer-call scenarios (unchanged mechanism from the prior slice) ──────

def _fresh_context(*, mode="ambient", interruptions_allowed=True, attention="available") -> ctx.RuntimeContext:
    return ctx.RuntimeContext(
        timestamp=time.time(),
        interaction=ctx.InteractionState(mode=mode, interruptions_allowed=interruptions_allowed, source="default"),
        attention=ctx.AttentionState(attention),
        task=ctx.TaskState("idle", 0),
        models={}, tools=ctx.ToolAvailability(True, True),
        desktop=ctx.DesktopState(focused_window=None, in_discord_call=False),
        model_resources=ctx.ModelResourceState((), True, False, {}, False, True),
        coordinator={},
    )


def _run_gate_scenario_02_normal_brave() -> dict:
    snapshot = _fresh_context()
    no_signal = ambient.decide(ambient.AmbientOpportunity(kind="periodic_observation", severity="info"), snapshot)
    ambient.evaluate_periodic_worthiness("acceptance_demo_v2", {"cpu_percent": 20.0})
    worthy = ambient.evaluate_periodic_worthiness("acceptance_demo_v2", {"cpu_percent": 90.0})
    with_signal = ambient.decide(
        ambient.AmbientOpportunity(kind="acceptance_demo_v2", severity="info", worthiness=worthy), snapshot
    )
    return {
        "no_worthiness_signal_decision": asdict(no_signal),
        "real_deviation_worthiness_decision": asdict(with_signal),
    }


def _run_gate_scenario_16_entertainment_no_commitment() -> dict:
    snapshot = _fresh_context()
    decision = ambient.decide(ambient.AmbientOpportunity(kind="procrastination_nudge", severity="info"), snapshot)
    return {"decision": asdict(decision)}


def _run_gate_scenario_17_nudge_dismissed_one_hour() -> dict:
    snapshot = _fresh_context()
    worthy = ambient.Worthiness(reason="approved_procrastination_evidence", confidence=1.0)
    opp = ambient.AmbientOpportunity(kind="procrastination_nudge", severity="notice", worthiness=worthy)
    t0 = 2_000_000.0
    ambient.set_snooze(ambient.kind_snooze_scope("procrastination_nudge"), 3600, reason="dismissed by user", now=t0)
    at_59_59 = ambient.decide(opp, snapshot, now=t0 + 3599)
    at_60_00 = ambient.decide(opp, snapshot, now=t0 + 3600)
    ambient.clear_snooze(ambient.kind_snooze_scope("procrastination_nudge"))
    return {"decision_at_59m59s": asdict(at_59_59), "decision_at_60m00s": asdict(at_60_00)}


async def main() -> None:
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = RESULTS_DIR / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    ps_before = _ollama_ps()
    report = {
        "timestamp": ts, "ollama_ps_before": ps_before,
        "scenarios": {}, "excluded_scenarios": EXCLUDED_SCENARIOS,
        "no_call_scenarios": {},
    }

    for sc in SCENARIOS:
        res = await _run_render(sc["request"])
        report["scenarios"][sc["name"]] = res
        print(f"{sc['name']}: family={res['event_family']} flavor_allowed={res['flavor_allowed']} ok={res['ok']}")
        print(f"    flavor:        {res['flavor']!r}")
        print(f"    deterministic: {res['deterministic']!r}")
        print(f"    composed:      {res['text']!r}")
        print(f"    reason={res['reason']} elapsed={res['elapsed_s']}s retries={res['retries']} "
              f"protected_ok={res['protected_values_verified']} semantic_hits={res['semantic_contradictions']}")

    report["no_call_scenarios"]["02_normal_brave_use"] = _run_gate_scenario_02_normal_brave()
    report["no_call_scenarios"]["16_entertainment_no_commitment"] = _run_gate_scenario_16_entertainment_no_commitment()
    report["no_call_scenarios"]["17_nudge_dismissed_one_hour"] = _run_gate_scenario_17_nudge_dismissed_one_hour()

    brave_runs = []
    for i in range(10):
        res = await _run_render(BRAVE_REQUEST)
        brave_runs.append(res)
        print(f"brave_repeat[{i}]: flavor={res['flavor']!r}")
    report["brave_repeat_10x"] = brave_runs

    seq_runs = []
    for i, req in enumerate(SEQUENTIAL_BROWSER_REQUESTS):
        res = await _run_render(req)
        seq_runs.append(res)
        print(f"sequential_browser[{i}]: flavor={res['flavor']!r}")
    report["sequential_browser_3x"] = seq_runs

    report["ollama_ps_after"] = _ollama_ps()

    with open(out_dir / "raw.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\nwrote {out_dir / 'raw.json'}")
    print(out_dir)


if __name__ == "__main__":
    asyncio.run(main())
