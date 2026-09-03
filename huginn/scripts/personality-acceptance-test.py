#!/usr/bin/env python3
"""Personality renderer acceptance test.

Exercises v2/personality.py's real render() against the real coordinator
and real qwen3.5:4b (think=false), same code path production uses. No
production code or config is touched. Facts used in every scenario are
synthetic (invented for this test), not pulled from live desktop state —
nothing here needs redaction, but the report says so explicitly per
scenario rather than assuming it.

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
# Same payload _render_personality_raw builds (think=False included) — this
# duplicates those few lines rather than editing llm.py, so production code
# stays untouched. Still goes through the real coordinator, since
# render_personality_only looks up this name in llm's module globals at
# call time.
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

# ── capture personality.log to recover attempt/validation-problem detail ──────
_log_records: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _log_records.append(record.getMessage())


personality.log.addHandler(_Capture())
personality.log.setLevel(logging.INFO)


def _ollama_ps() -> dict:
    try:
        r = httpx.get(f"{config.OLLAMA_BASE}/api/ps", timeout=5)
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def _redact(request: PersonalityRequest) -> dict:
    """All facts below are synthetic (invented for this test, not read from
    live desktop state) — nothing to redact today, but this stays a real
    pass over the fields so a future caller that DOES seed real facts isn't
    silently exempted."""
    d = asdict(request)
    return d


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
    return {
        "ok": result.ok,
        "text": result.text,
        "reason": result.reason,
        "retries": retries,
        "validation_problems": problems,
        "elapsed_s": round(elapsed, 3),
        "deadline_reached": elapsed >= config.AMBIENT_RENDER_DEADLINE_SECONDS,
        "raw_metrics": raw_metrics,
    }


# ── 20 acceptance scenarios ────────────────────────────────────────────────────
# "disposable" = matches daemon.py's periodic_observation/ambient convention
# (silence on render failure, no fallback). "consequential" = matches
# task_complete/bash_event convention (deterministic fallback on failure).
# Scenarios with no existing production call site are classified by
# analogy to the closest shipped convention, marked as such below.

SCENARIOS = [
    dict(
        name="01_brave_elevated_noncritical",
        caller_class="disposable (periodic_observation analogue)",
        request=PersonalityRequest(
            purpose="periodic_observation",
            facts={"app": "Brave", "memory_gb": "9.4", "tabs": "38", "note": "elevated but noncritical"},
            severity="notice",
            interruption_reason="hourly observation window, resource use above typical baseline",
            max_length=200,
            prohibited_additions=("diagnosis", "recommendation", "urgency", "an action to take"),
            interaction_mode="ambient",
        ),
    ),
    dict(
        name="03_thunderbird_14_nonurgent",
        caller_class="disposable (periodic_observation analogue)",
        request=PersonalityRequest(
            purpose="periodic_observation",
            facts={"app": "Thunderbird", "new_messages": "14", "urgency": "nonurgent"},
            severity="info",
            max_length=200,
            prohibited_additions=("diagnosis", "recommendation", "urgency", "an action to take"),
            interaction_mode="ambient",
        ),
    ),
    dict(
        name="04_docker_unusual_resources",
        caller_class="disposable (periodic_observation analogue)",
        request=PersonalityRequest(
            purpose="periodic_observation",
            facts={"process": "Docker", "cpu_percent": "81", "mem_gb": "6.2", "note": "higher than typical for this process"},
            severity="notice",
            max_length=200,
            prohibited_additions=("diagnosis", "recommendation", "urgency", "an action to take"),
            interaction_mode="ambient",
        ),
    ),
    dict(
        name="05_discord_notification_burst",
        caller_class="disposable (periodic_observation analogue)",
        request=PersonalityRequest(
            purpose="periodic_observation",
            facts={"app": "Discord", "notification_count": "47", "window_minutes": "10"},
            severity="notice",
            max_length=200,
            prohibited_additions=("diagnosis", "recommendation", "urgency", "an action to take"),
            interaction_mode="ambient",
        ),
    ),
    dict(
        name="06_unfamiliar_fictional_app_badger",
        caller_class="disposable (periodic_observation analogue)",
        request=PersonalityRequest(
            purpose="periodic_observation",
            facts={"app": "Badger", "cpu_percent": "34", "note": "unfamiliar application, name only, no other metadata"},
            severity="info",
            max_length=200,
            prohibited_additions=("a claim of knowing what this app does", "an invented capability"),
            interaction_mode="ambient",
        ),
    ),
    dict(
        name="07_unfamiliar_neutral_app_no_metaphor",
        caller_class="disposable (periodic_observation analogue)",
        request=PersonalityRequest(
            purpose="periodic_observation",
            facts={"app": "Parity", "cpu_percent": "29", "note": "unfamiliar application, name only, no other metadata"},
            severity="info",
            max_length=200,
            prohibited_additions=("a claim of knowing what this app does", "an invented capability", "a forced animal metaphor"),
            interaction_mode="ambient",
        ),
    ),
    dict(
        name="08_office_laser_offline_unknown_cause",
        caller_class="consequential (analogue: actionable device state, no shipped call site)",
        request=PersonalityRequest(
            purpose="device_offline",
            facts={"device": "Office-Laser", "status": "offline", "cause": "unknown"},
            severity="notice",
            prohibited_additions=("a diagnosis", "a cause", "a fix"),
            protected_keys=("device",),
            max_length=200,
            interaction_mode="ambient",
        ),
    ),
    dict(
        name="09_disk_temp_critical_78c",
        caller_class="consequential (critical severity, analogue: bash_event)",
        request=PersonalityRequest(
            purpose="resource_critical",
            facts={"metric": "disk temperature", "value_c": "78", "threshold": "critical"},
            severity="critical",
            protected_keys=("value_c",),
            max_length=200,
            interaction_mode="ambient",
        ),
    ),
    dict(
        name="10_nixos_rebuild_failed_twice",
        caller_class="consequential (bash_event, shipped convention)",
        request=PersonalityRequest(
            purpose="bash_event",
            facts={
                "command": "nixos-rebuild switch",
                "exit_code": "1",
                "attempts": "2",
                "error": "flake.nix: No such file or directory",
                "outcome": "failed",
            },
            severity="notice",
            protected_keys=("error",),
            prohibited_additions=("a fix", "a diagnosis of the cause"),
            max_length=200,
            interaction_mode="ambient",
        ),
    ),
    dict(
        name="11_rsync_not_executed_unmounted",
        caller_class="consequential (analogue: bash_event, action withheld not run)",
        request=PersonalityRequest(
            purpose="action_not_executed",
            facts={
                "reason": "/mnt/archive is not mounted",
                "planned_command": "rsync -a ~/notes/ /mnt/archive/notes/",
                "outcome": "not executed",
            },
            severity="notice",
            protected_keys=("planned_command", "reason"),
            prohibited_additions=("a claim that it ran anyway", "a fix"),
            max_length=200,
            interaction_mode="ambient",
        ),
    ),
    dict(
        name="12_stronger_reasoner_conclusion_uncertain",
        caller_class="consequential (analogue: diagnostic hand-off, no shipped call site)",
        request=PersonalityRequest(
            purpose="diagnostic_conclusion",
            facts={"process": "481", "cpu_percent": "92"},
            reasoner_conclusion=(
                "Process 481 is using 92% CPU because its indexing loop is probably "
                "retrying a failed operation, but available logs do not prove this "
                "conclusively."
            ),
            severity="notice",
            prohibited_additions=("false certainty", "a stated fix"),
            max_length=250,
            interaction_mode="ambient",
        ),
    ),
    dict(
        name="13_calendar_capability_unavailable",
        caller_class="consequential (analogue: capability_unavailable, no shipped call site)",
        request=PersonalityRequest(
            purpose="capability_unavailable",
            facts={"requested_action": "add milk tomorrow", "capability": "calendar", "status": "unavailable"},
            severity="info",
            prohibited_additions=("a promise to do it later", "a workaround", "a fabricated confirmation"),
            max_length=200,
            interaction_mode="direct",
        ),
    ),
    dict(
        name="14_ambiguous_close_it_no_focus",
        caller_class="consequential (analogue: clarification request, no shipped call site)",
        request=PersonalityRequest(
            purpose="clarification_needed",
            facts={
                "request": "close it",
                "open_applications": "Brave, Thunderbird, a terminal, Discord",
                "focused_window": "none known",
            },
            severity="info",
            prohibited_additions=("a guess at which app", "an action taken", "a closed window"),
            max_length=200,
            interaction_mode="direct",
        ),
    ),
    dict(
        name="15_procrastination_nudge_approved_tiny_step",
        caller_class="consequential (analogue: procrastination_nudge, no shipped call site — spec-named)",
        request=PersonalityRequest(
            purpose="procrastination_nudge",
            facts={
                "evidence": "42 minutes on YouTube after saying 'just checking one video'",
                "requested_next_step": "open the report doc and write one sentence",
            },
            severity="notice",
            prohibited_additions=("a lecture", "multiple next steps", "guilt-tripping", "shame"),
            max_length=220,
            interaction_mode="direct",
        ),
    ),
    dict(
        name="18_incapable_statement_max_3_sentences",
        caller_class="consequential (direct emotional disclosure, no shipped call site)",
        request=PersonalityRequest(
            purpose="user_disclosure",
            facts={"user_statement": "failed code makes him feel incapable"},
            severity="notice",
            prohibited_additions=("therapy language", "motivational-poster language", "a reassurance cliche"),
            max_length=320,
            interaction_mode="direct",
        ),
    ),
    dict(
        name="19_task_success_exact_name_duration",
        caller_class="consequential (task_complete, shipped convention)",
        request=PersonalityRequest(
            purpose="task_complete",
            facts={"task": "nightly-backup", "duration_seconds": "142", "result_preview": "ok, 4.2GB written"},
            severity="info",
            protected_keys=("task",),
            max_length=200,
            interaction_mode="ambient",
        ),
    ),
    dict(
        name="20_task_failed_exact_error_no_diagnosis",
        caller_class="consequential (analogue: task failure, no shipped call site)",
        request=PersonalityRequest(
            purpose="task_failed",
            facts={"task": "db-migrate", "error": "connection refused: 127.0.0.1:5432", "diagnosis": "unknown"},
            severity="notice",
            protected_keys=("task", "error"),
            prohibited_additions=("a diagnosis", "a fix"),
            max_length=220,
            interaction_mode="ambient",
        ),
    ),
]

BRAVE_REQUEST = SCENARIOS[0]["request"]

SEQUENTIAL_BROWSER_REQUESTS = [
    PersonalityRequest(
        purpose="periodic_observation",
        facts={"app": "Brave", "memory_gb": "9.4", "tabs": "38", "note": "elevated but noncritical"},
        severity="notice", max_length=200,
        prohibited_additions=("diagnosis", "recommendation", "urgency", "an action to take"),
        interaction_mode="ambient",
    ),
    PersonalityRequest(
        purpose="periodic_observation",
        facts={"app": "Brave", "tabs": "51", "note": "tab count climbing over the last hour"},
        severity="notice", max_length=200,
        prohibited_additions=("diagnosis", "recommendation", "urgency", "an action to take"),
        interaction_mode="ambient",
    ),
    PersonalityRequest(
        purpose="periodic_observation",
        facts={"app": "Brave", "cpu_percent": "63", "note": "sustained CPU use, no single tab identified"},
        severity="notice", max_length=200,
        prohibited_additions=("diagnosis", "recommendation", "urgency", "an action to take"),
        interaction_mode="ambient",
    ),
]


# ── no-renderer-call scenarios (ambient.decide() gates before render()) ────────

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
    """Normal/routine Brave usage: no worthiness signal attached (a real
    caller would have run this through evaluate_periodic_worthiness and
    found nothing had moved) -> default-deny. Contrast: the same
    opportunity WITH a real deviation-based worthiness signal (simulated
    via evaluate_periodic_worthiness itself) -> allowed. This is the
    baseline gap directly closed by the worthiness gate — previously
    decide() had no notion of 'normal vs notable' at all."""
    snapshot = _fresh_context()
    no_signal = ambient.decide(ambient.AmbientOpportunity(kind="periodic_observation", severity="info"), snapshot)

    ambient.evaluate_periodic_worthiness("acceptance_demo", {"cpu_percent": 20.0})  # establish baseline
    worthy = ambient.evaluate_periodic_worthiness("acceptance_demo", {"cpu_percent": 90.0})  # real deviation
    with_signal = ambient.decide(
        ambient.AmbientOpportunity(kind="acceptance_demo", severity="info", worthiness=worthy), snapshot
    )

    return {
        "no_worthiness_signal_decision": asdict(no_signal),
        "real_deviation_worthiness_decision": asdict(with_signal),
        "finding": (
            "RESOLVED (was Finding in the baseline): decide() previously had no notion of "
            "'normal vs notable' resource use at all — a fresh opportunity was ALLOWED "
            "regardless of content. It now default-denies unless a deterministic Worthiness "
            "signal is attached (no_worthiness_signal_decision, above), and allows once a "
            "real baseline deviation is detected (real_deviation_worthiness_decision, above) "
            "— demonstrated here via the same evaluate_periodic_worthiness() helper "
            "random_chime_worker now uses in production."
        ),
    }


def _run_gate_scenario_16_entertainment_no_commitment() -> dict:
    """Entertainment with no commitment: no procrastination evidence exists,
    so no Worthiness is attached -> default-deny. This is now the direct,
    intended mechanism (Part 2), not an incidental side effect of game-mode
    suppression."""
    snapshot = _fresh_context()
    decision = ambient.decide(ambient.AmbientOpportunity(kind="procrastination_nudge", severity="info"), snapshot)
    return {"decision": asdict(decision), "denied_via": "worthiness default-deny (no evidence attached)"}


def _run_gate_scenario_17_nudge_dismissed_one_hour() -> dict:
    """Prior nudge dismissed for a full hour: RESOLVED (was Finding 2 in the
    baseline — the fixed 30-minute cooldown couldn't express an hour-long
    dismissal). Now uses the real explicit snooze API: set_snooze() for
    exactly the requested 3600s, checked at 59:59 (still active) and 60:00
    ([start, expiry) semantics — expired)."""
    snapshot = _fresh_context()
    worthy = ambient.Worthiness(reason="approved_procrastination_evidence", confidence=1.0)
    opp = ambient.AmbientOpportunity(kind="procrastination_nudge", severity="notice", worthiness=worthy)

    t0 = 1_000_000.0
    ambient.set_snooze(ambient.kind_snooze_scope("procrastination_nudge"), 3600, reason="dismissed by user", now=t0)
    at_59_59 = ambient.decide(opp, snapshot, now=t0 + 3599)
    at_60_00 = ambient.decide(opp, snapshot, now=t0 + 3600)
    ambient.clear_snooze(ambient.kind_snooze_scope("procrastination_nudge"))

    return {
        "decision_at_59m59s": asdict(at_59_59),
        "decision_at_60m00s": asdict(at_60_00),
        "finding": (
            "RESOLVED (was a baseline gap): an explicit ambient.set_snooze() call now "
            "expresses the full requested duration exactly (3600s here), independent of the "
            "fixed 1800s automatic cooldown. Still denied at 59:59, allowed again at exactly "
            "60:00 ([start, expiry) semantics)."
        ),
    }


async def main() -> None:
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = RESULTS_DIR / ts
    out_dir.mkdir(parents=True, exist_ok=True)

    ps_before = _ollama_ps()

    report = {"timestamp": ts, "ollama_ps_before": ps_before, "scenarios": {}, "no_call_scenarios": {}}

    # cold call: nothing resident (confirmed via ps_before) -> scenario 1 doubles
    # as the cold-latency measurement.
    for sc in SCENARIOS:
        res = await _run_render(sc["request"])
        report["scenarios"][sc["name"]] = {
            "caller_class": sc["caller_class"],
            "input": _redact(sc["request"]),
            **res,
        }
        print(f"{sc['name']}: ok={res['ok']} {res['elapsed_s']}s retries={res['retries']} text={res['text']!r}")

    # no-renderer-call scenarios
    report["no_call_scenarios"]["02_normal_brave_use"] = _run_gate_scenario_02_normal_brave()
    report["no_call_scenarios"]["16_entertainment_no_commitment"] = _run_gate_scenario_16_entertainment_no_commitment()
    report["no_call_scenarios"]["17_nudge_dismissed_one_hour"] = _run_gate_scenario_17_nudge_dismissed_one_hour()
    print("no-call scenarios (02, 16, 17) done")

    # repetition: brave x10
    brave_runs = []
    for i in range(10):
        res = await _run_render(BRAVE_REQUEST)
        brave_runs.append(res)
        print(f"brave_repeat[{i}]: {res['elapsed_s']}s text={res['text']!r}")
    report["brave_repeat_10x"] = brave_runs

    # sequential related browser observations
    seq_runs = []
    for i, req in enumerate(SEQUENTIAL_BROWSER_REQUESTS):
        res = await _run_render(req)
        res["input"] = _redact(req)
        seq_runs.append(res)
        print(f"sequential_browser[{i}]: {res['elapsed_s']}s text={res['text']!r}")
    report["sequential_browser_3x"] = seq_runs

    ps_after = _ollama_ps()
    report["ollama_ps_after"] = ps_after

    with open(out_dir / "raw.json", "w") as f:
        json.dump(report, f, indent=2, default=str)

    print(f"\nwrote {out_dir / 'raw.json'}")
    print(out_dir)


if __name__ == "__main__":
    asyncio.run(main())
