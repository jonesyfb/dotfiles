"""Tests for the narrow personality renderer (v2/personality.py).

All mocked at the llm.render_personality_only boundary — no real Ollama
call anywhere in this file, so these run without GPU access. A handful of
tests below also exercise the REAL coordinator singleton (with a fake
underlying fn) specifically to prove real admission/game-mode/preemption
behavior without touching Ollama.

Rewritten for the semantic-safety hardening slice (see
scripts/personality_bench/results/20260903T055434Z/report.md): exact-value
safety alone wasn't enough — a model that never sees a fact can still
imply a false outcome in its flavor clause ("the gears turned" for an
unexecuted rsync). This introduces typed deterministic presenters
(_PRESENTERS) per event_family, a flavor-eligibility allowlist
(_FLAVOR_ELIGIBLE_FAMILIES) that skips the model call entirely for
risky families, and semantic-contradiction validation for flavor text
that IS attempted.
"""
import asyncio

import pytest

import coordinator as coordinator_module
import llm
import personality
from coordinator import Denial, Purpose, RequestClass, coordinator
from llm import CoordinatorDenied
from personality import PersonalityRequest


# ── _validate_flavor: malformed/verbose/theatrical/leaked-value/semantic ────

def test_validate_flavor_accepts_clean_short_text():
    req = PersonalityRequest(purpose="x", max_length=100)
    assert personality._validate_flavor("Brave has grown restless again.", req, ()) is None


def test_validate_flavor_accepts_empty():
    req = PersonalityRequest(purpose="x")
    assert personality._validate_flavor("", req, ()) is None


def test_validate_flavor_rejects_too_long():
    req = PersonalityRequest(purpose="x", max_length=10)
    assert personality._validate_flavor("this is way too long for the cap", req, ()) == "too_long"


def test_validate_flavor_rejects_asterisk_stage_direction():
    req = PersonalityRequest(purpose="x", max_length=200)
    assert personality._validate_flavor("*ruffles feathers* Interesting.", req, ()) == "theatrical_formatting"


def test_validate_flavor_rejects_bracketed_action():
    req = PersonalityRequest(purpose="x", max_length=200)
    assert personality._validate_flavor("[caws softly] Noted.", req, ()) == "theatrical_formatting"


def test_validate_flavor_rejects_self_prefix():
    req = PersonalityRequest(purpose="x", max_length=200)
    assert personality._validate_flavor("Huginn: that's a lot of tabs.", req, ()) == "self_prefixed"


def test_validate_flavor_rejects_any_digit():
    req = PersonalityRequest(purpose="x", max_length=200)
    assert personality._validate_flavor("It's using 9 gigabytes.", req, ()) == "contains_digits"


def test_validate_flavor_accepts_spelled_out_numbers():
    req = PersonalityRequest(purpose="x", max_length=200)
    assert personality._validate_flavor("It's using a lot of memory.", req, ()) is None


def test_validate_flavor_rejects_leaked_protected_value():
    req = PersonalityRequest(purpose="x", max_length=200)
    assert personality._validate_flavor(
        "It happened in /home/nate/project.", req, ("/home/nate/project",)
    ) == "leaked_protected_value"


def test_validate_flavor_rejects_known_misspelling():
    req = PersonalityRequest(purpose="x", max_length=200)
    assert personality._validate_flavor("Fourty minutes of scrolling.", req, ()) == "known_misspelling"


def test_validate_flavor_rejects_too_many_sentences():
    req = PersonalityRequest(purpose="x", max_length=200, max_sentences=2)
    text = "One. Two. Three."
    assert personality._validate_flavor(text, req, ()) == "too_many_sentences"


def test_validate_flavor_allows_up_to_sentence_cap():
    req = PersonalityRequest(purpose="x", max_length=200, max_sentences=2)
    assert personality._validate_flavor("One. Two.", req, ()) is None


# ── Semantic-contradiction checks (item 7) ──────────────────────────────────

@pytest.mark.parametrize("verb", [
    "added", "saved", "scheduled", "recorded", "completed", "sent",
    "deleted", "closed", "executed", "fixed", "diagnosed", "succeeded",
])
def test_validate_flavor_rejects_action_outcome_claims(verb):
    req = PersonalityRequest(purpose="x", max_length=200)
    text = f"Looks like it {verb} just fine."
    assert personality._validate_flavor(text, req, ()) == "implies_action_outcome"


def test_validate_flavor_rejects_causality_when_diagnosis_unknown():
    req = PersonalityRequest(purpose="x", max_length=200, facts={"diagnosis": "unknown"})
    text = "It's acting up because the disk is failing."
    assert personality._validate_flavor(text, req, ()) == "implies_causality_when_unknown"


def test_validate_flavor_allows_causality_when_diagnosis_known():
    req = PersonalityRequest(purpose="x", max_length=200, facts={"diagnosis": "a known bad cable"})
    text = "It's acting up because of the usual culprit."
    assert personality._validate_flavor(text, req, ()) is None


def test_validate_flavor_rejects_certainty_over_hedged_reasoner():
    req = PersonalityRequest(purpose="x", max_length=200, reasoner_conclusion="It might be a leak.")
    text = "This is definitely a memory leak."
    assert personality._validate_flavor(text, req, ()) == "implies_certainty_over_hedge"


def test_validate_flavor_rejects_execution_claim_when_not_executed():
    req = PersonalityRequest(purpose="x", max_length=200, facts={"outcome": "not executed"})
    text = "Good news, that command ran without a hitch."
    assert personality._validate_flavor(text, req, ()) == "implies_execution_when_not_executed"


def test_validate_flavor_allows_plain_text_when_not_executed():
    req = PersonalityRequest(purpose="x", max_length=200, facts={"outcome": "not executed"})
    text = "The door stayed shut this time."
    assert personality._validate_flavor(text, req, ()) is None


# ── Deterministic presenters: typed, natural, never a key/value dump ────────

def test_present_resource_observation_brave_style():
    req = PersonalityRequest(
        purpose="x", event_family="resource_observation", severity="notice",
        facts={"app": "Brave", "memory_gb": "9.4", "tabs": "38"},
    )
    text = personality._compose_deterministic(req)
    assert text == "Brave is using 9.4 GB across 38 tabs. Elevated, not critical."
    assert ":" not in text  # no key: value serialization


def test_present_resource_observation_falls_back_safely_for_unknown_keys():
    req = PersonalityRequest(
        purpose="x", event_family="resource_observation",
        facts={"app": "Badger", "some_unknown_field": "xyz123"},
    )
    text = personality._compose_deterministic(req)
    assert "xyz123" not in text  # unknown keys never dumped
    assert "Badger" in text


def test_present_critical_threshold():
    req = PersonalityRequest(
        purpose="x", event_family="critical_threshold", severity="critical",
        facts={"metric": "disk temperature", "value": "78", "unit": " C"},
    )
    assert personality._compose_deterministic(req) == "Disk temperature is critical: 78 C."


def test_present_device_unavailable_unknown_cause():
    req = PersonalityRequest(
        purpose="x", event_family="device_unavailable",
        facts={"device": "Office-Laser", "status": "offline", "cause": "unknown"},
    )
    assert personality._compose_deterministic(req) == "Office-Laser is offline. The cause is not yet known."


def test_present_command_failed_with_error():
    req = PersonalityRequest(
        purpose="x", event_family="command_failed",
        facts={"command": "nixos-rebuild switch", "attempts": "2", "error": "flake.nix: No such file or directory"},
    )
    text = personality._compose_deterministic(req)
    assert text == "`nixos-rebuild switch` failed twice: `flake.nix: No such file or directory`."


def test_present_command_failed_plain_exit_code():
    req = PersonalityRequest(
        purpose="x", event_family="command_failed",
        facts={"command": "make build", "exit_code": "1", "elapsed_seconds": "12"},
    )
    text = personality._compose_deterministic(req)
    assert "`make build` failed" in text
    assert "exit 1" in text


def test_present_command_not_executed():
    req = PersonalityRequest(
        purpose="x", event_family="command_not_executed",
        facts={
            "reason": "/mnt/archive is not mounted",
            "planned_command": "rsync -a ~/notes/ /mnt/archive/notes/",
        },
    )
    text = personality._compose_deterministic(req)
    assert text == "/mnt/archive is not mounted, so `rsync -a ~/notes/ /mnt/archive/notes/` was not executed."


def test_present_task_succeeded():
    req = PersonalityRequest(
        purpose="x", event_family="task_succeeded",
        facts={"task": "nightly-backup", "duration_seconds": "142", "result_preview": "4.2GB written"},
    )
    text = personality._compose_deterministic(req)
    assert text == "Task nightly-backup finished in 142 seconds: 4.2GB written."


def test_present_task_failed_unknown_diagnosis():
    req = PersonalityRequest(
        purpose="x", event_family="task_failed",
        facts={"task": "db-migrate", "error": "connection refused: 127.0.0.1:5432", "diagnosis": "unknown"},
    )
    text = personality._compose_deterministic(req)
    assert text == "Task db-migrate failed: connection refused: 127.0.0.1:5432. The cause is not yet known."


def test_present_capability_unavailable():
    req = PersonalityRequest(
        purpose="x", event_family="capability_unavailable",
        facts={"capability": "calendar", "requested_action": "add milk tomorrow"},
    )
    assert personality._compose_deterministic(req) == "The calendar is unavailable, so I did not add milk tomorrow."


def test_present_reasoner_conclusion_verbatim():
    conclusion = "This might be a memory leak, but I'm not certain — could also be normal caching."
    req = PersonalityRequest(purpose="x", event_family="reasoner_conclusion", reasoner_conclusion=conclusion)
    assert personality._compose_deterministic(req) == conclusion


def test_present_procrastination_nudge_step_only():
    req = PersonalityRequest(
        purpose="x", event_family="procrastination_nudge",
        facts={"requested_next_step": "open the report and write one sentence"},
    )
    assert personality._compose_deterministic(req) == "Open the report and write one sentence."


def test_present_clarification_needed():
    req = PersonalityRequest(
        purpose="x", event_family="clarification_needed",
        facts={"request": "close it", "open_applications": "Brave, Thunderbird, a terminal, Discord"},
    )
    text = personality._compose_deterministic(req)
    assert text == "Which should I close: Brave, Thunderbird, a terminal, or Discord?"
    assert text.endswith("?")


def test_present_unknown_family_never_dumps_dict():
    req = PersonalityRequest(purpose="x", event_family="totally_unrecognized", facts={"a": "1", "b": "2"})
    text = personality._compose_deterministic(req)
    assert "a" not in text or text == ""
    assert "{" not in text


# ── Flavor eligibility: risk-aware, structural (item 2) ─────────────────────

@pytest.mark.parametrize("family", [
    "resource_observation", "activity_observation", "procrastination_nudge",
    "task_succeeded", "command_slow",
])
def test_flavor_eligible_families(family):
    assert family in personality._FLAVOR_ELIGIBLE_FAMILIES


@pytest.mark.parametrize("family", [
    "capability_unavailable", "command_not_executed", "clarification_needed",
    "critical_threshold", "task_failed", "reasoner_conclusion", "device_unavailable",
    "command_failed",
])
def test_flavor_ineligible_families(family):
    assert family not in personality._FLAVOR_ELIGIBLE_FAMILIES


def test_render_never_calls_coordinator_for_ineligible_family(monkeypatch):
    calls = {"n": 0}

    async def fake_render_personality_only(*a, **kw):
        calls["n"] += 1
        return "should never be called"

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(
        purpose="x", event_family="capability_unavailable",
        facts={"capability": "calendar", "requested_action": "add milk tomorrow"},
    )
    result = asyncio.run(personality.render(req))

    assert calls["n"] == 0
    assert result.reason == "deterministic_only"
    assert result.ok is True
    assert result.text == "The calendar is unavailable, so I did not add milk tomorrow."
    assert result.flavor is None


def test_render_never_calls_coordinator_for_critical(monkeypatch):
    calls = {"n": 0}

    async def fake_render_personality_only(*a, **kw):
        calls["n"] += 1
        return "should never be called"

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(
        purpose="x", event_family="critical_threshold", severity="critical",
        facts={"metric": "disk temperature", "value": "78", "unit": " C"},
    )
    result = asyncio.run(personality.render(req))

    assert calls["n"] == 0
    assert result.ok is True
    assert result.text == "Disk temperature is critical: 78 C."


def test_render_never_calls_coordinator_for_clarification(monkeypatch):
    calls = {"n": 0}

    async def fake_render_personality_only(*a, **kw):
        calls["n"] += 1
        return "should never be called"

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(
        purpose="x", event_family="clarification_needed",
        facts={"request": "close it", "open_applications": "Brave, Discord"},
    )
    result = asyncio.run(personality.render(req))

    assert calls["n"] == 0
    assert result.text.startswith("Which should I close")
    assert result.text.endswith("?")


def test_render_unknown_family_returns_not_ok_without_calling_coordinator(monkeypatch):
    calls = {"n": 0}

    async def fake_render_personality_only(*a, **kw):
        calls["n"] += 1
        return "unused"

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    result = asyncio.run(personality.render(PersonalityRequest(purpose="x", event_family="mystery_family")))

    assert calls["n"] == 0
    assert result.ok is False
    assert result.reason == "unknown_event_family"
    assert result.deterministic == ""


# ── Prompt construction: only flavor_cues reach the model, never facts ──────

def test_prompt_never_shows_facts():
    req = PersonalityRequest(
        purpose="x", event_family="resource_observation",
        facts={"app": "Brave", "memory_gb": "9.4", "tabs": "38"},
        flavor_cues={"subject": "a browser", "band": "elevated"},
    )
    prompt = personality._build_user_prompt(req)
    assert "9.4" not in prompt
    assert "38" not in prompt
    assert "Brave" not in prompt  # exact app name is a fact, not a flavor cue
    assert "a browser" in prompt
    assert "elevated" in prompt


def test_prompt_includes_flavor_cues():
    req = PersonalityRequest(
        purpose="x", flavor_cues={"subject": "a browser", "creature_hint": "badger"},
    )
    prompt = personality._build_user_prompt(req)
    assert "a browser" in prompt
    assert "badger" in prompt


def test_prompt_instructs_no_digits_and_no_outcome_claims():
    req = PersonalityRequest(purpose="x")
    prompt = personality._build_user_prompt(req)
    assert "no numbers" in prompt.lower() or "no digits" in prompt.lower()
    assert "succeeded" in prompt.lower() or "outcome" in prompt.lower()


def test_vulnerable_purpose_structural():
    from config import PERSONALITY_SYSTEM_PROMPT
    lowered = PERSONALITY_SYSTEM_PROMPT.lower()
    assert "loyal" in lowered


# ── render(): mocked coordinator boundary, flavor-eligible path ─────────────

def test_render_ok_composes_flavor_and_deterministic(monkeypatch):
    captured = {}

    async def fake_render_personality_only(system_prompt, user_prompt, purpose, deadline_seconds):
        captured["purpose"] = purpose
        return "restless again"

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(
        purpose="high_memory_observation", event_family="resource_observation",
        facts={"app": "Brave", "memory_gb": "9.1", "tabs": "20"},
        flavor_cues={"subject": "a browser"},
        severity="info",
    )
    result = asyncio.run(personality.render(req, purpose=Purpose.AMBIENT))

    assert result.ok is True
    assert "restless again" in result.text
    assert "9.1" in result.text  # deterministic portion carries the exact value
    assert captured["purpose"] == Purpose.AMBIENT


def test_render_empty_flavor_still_ok_and_shows_deterministic_alone(monkeypatch):
    async def fake_render_personality_only(*a, **kw):
        return ""

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(
        purpose="x", event_family="task_succeeded",
        facts={"task": "nightly-backup", "duration_seconds": "142"},
    )
    result = asyncio.run(personality.render(req))

    assert result.ok is True
    assert result.flavor == ""
    assert result.text == result.deterministic


def test_render_deterministic_always_present_on_coordinator_denial(monkeypatch):
    async def fake_render_personality_only(*a, **kw):
        raise CoordinatorDenied(Denial.DEADLINE_EXCEEDED, "deadline exceeded while running")

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(
        purpose="x", event_family="task_succeeded", facts={"task": "nightly-backup"},
    )
    result = asyncio.run(personality.render(req))

    assert result.ok is False
    assert result.text is None
    assert result.reason == "coordinator_denied:deadline_exceeded"
    assert "nightly-backup" in result.deterministic


def test_render_coordinator_denied_preempted(monkeypatch):
    async def fake_render_personality_only(*a, **kw):
        raise CoordinatorDenied(Denial.PREEMPTED, "cancelled for higher-priority work")

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(purpose="x", event_family="task_succeeded", facts={"task": "x"})
    result = asyncio.run(personality.render(req))

    assert result.ok is False
    assert result.reason == "coordinator_denied:preempted"


def test_render_unexpected_exception_does_not_raise(monkeypatch):
    async def fake_render_personality_only(*a, **kw):
        raise ValueError("boom")

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(purpose="x", event_family="task_succeeded", facts={"task": "x"})
    result = asyncio.run(personality.render(req))

    assert result.ok is False
    assert result.reason == "error"


def test_render_retries_once_on_leaked_protected_value(monkeypatch):
    calls = {"n": 0}

    async def fake_render_personality_only(system_prompt, user_prompt, purpose, deadline_seconds):
        calls["n"] += 1
        if calls["n"] == 1:
            return "the nightly-backup task went well"  # leaks the task name
        return "quiet and done"

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(
        purpose="x", event_family="task_succeeded", facts={"task": "nightly-backup"},
    )
    result = asyncio.run(personality.render(req))

    assert calls["n"] == 2
    assert result.ok is True
    assert "nightly-backup" in result.text  # via the deterministic portion
    assert "nightly-backup" not in result.flavor


def test_render_gives_up_after_max_retries(monkeypatch):
    async def always_leaks(system_prompt, user_prompt, purpose, deadline_seconds):
        return "the nightly-backup task went well"

    monkeypatch.setattr(personality, "render_personality_only", always_leaks)

    req = PersonalityRequest(
        purpose="x", event_family="task_succeeded", facts={"task": "nightly-backup"},
    )
    result = asyncio.run(personality.render(req))

    assert result.ok is False
    assert result.reason == "validation_failed"
    assert "nightly-backup" in result.deterministic


def test_render_does_not_inspect_model_residency(monkeypatch):
    calls = {"n": 0}

    async def fake_render_personality_only(*a, **kw):
        calls["n"] += 1
        return "fine either way"

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(purpose="x", event_family="task_succeeded", facts={"task": "x"})
    result = asyncio.run(personality.render(req))

    assert result.ok is True
    assert calls["n"] == 1


def test_render_nothing_to_present_when_presenter_empty(monkeypatch):
    """procrastination_nudge with no next step has nothing safe to present
    and nothing to attach flavor to — no model call, ok=False."""
    calls = {"n": 0}

    async def fake_render_personality_only(*a, **kw):
        calls["n"] += 1
        return "unused"

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    result = asyncio.run(personality.render(PersonalityRequest(purpose="x", event_family="procrastination_nudge")))

    assert calls["n"] == 0
    assert result.ok is False
    assert result.reason == "nothing_to_present"


# ── Real coordinator: game mode admits RESIDENT_PERSONALITY (item 15) ────────

@pytest.fixture(autouse=True)
def _reset_coordinator():
    coordinator._queue = []
    coordinator._current = None
    coordinator._scheduler_task = None
    coordinator._game_mode_check = lambda: False
    coordinator._lock = asyncio.Lock()
    coordinator._wakeup = asyncio.Event()
    yield
    coordinator._queue = []
    coordinator._current = None
    coordinator._scheduler_task = None
    coordinator._game_mode_check = lambda: False


def test_render_personality_only_admitted_during_game_mode(monkeypatch):
    coordinator.set_game_mode_check(lambda: True)

    async def fake_raw(system_prompt, user_prompt):
        return "Still here, even mid-game."

    monkeypatch.setattr(llm, "_render_personality_raw", fake_raw)

    async def _run():
        return await llm.render_personality_only("sys", "user", purpose=Purpose.AMBIENT)

    text = asyncio.run(_run())
    assert text == "Still here, even mid-game."


def test_ordinary_local_reasoning_denied_during_game_mode_for_contrast(monkeypatch):
    coordinator.set_game_mode_check(lambda: True)

    async def _run():
        events = []
        req = coordinator_module.InferenceRequest(
            request_class=RequestClass.ORDINARY_LOCAL_REASONING,
            purpose=Purpose.DIRECT, model="m",
            fn=lambda emit: _noop(),
        )
        async for ev in coordinator.submit(req):
            events.append(ev)
        return events

    events = asyncio.run(_run())
    assert events[-1].kind == "denied"
    assert events[-1].denial == Denial.GAME_MODE


async def _noop():
    return "unused"


# ── Observability: never log fact values / private prose (item 11) ──────────

def test_render_logging_never_includes_fact_values_or_rendered_text(monkeypatch, caplog):
    async def fake_render_personality_only(system_prompt, user_prompt, purpose, deadline_seconds):
        return "quietly humming along tonight"

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(
        purpose="periodic_observation", event_family="resource_observation",
        facts={"secret_fact_key": "super-sensitive-value-12345"},
    )
    with caplog.at_level("INFO", logger="huginn.personality"):
        result = asyncio.run(personality.render(req))

    assert result.ok is True
    log_text = "\n".join(r.message for r in caplog.records)
    assert "super-sensitive-value-12345" not in log_text
    assert "secret_fact_key" not in log_text


def test_render_logging_on_denial_includes_reason_not_content(monkeypatch, caplog):
    async def fake_render_personality_only(*a, **kw):
        raise CoordinatorDenied(Denial.PREEMPTED, "cancelled for higher-priority work")

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    with caplog.at_level("INFO", logger="huginn.personality"):
        result = asyncio.run(personality.render(
            PersonalityRequest(purpose="task_complete", event_family="task_succeeded", facts={"task": "x"})
        ))

    assert result.ok is False
    log_text = "\n".join(r.message for r in caplog.records)
    assert "preempted" in log_text


# ── Diagnostic-only style categorizer (not fed back into the prompt) ────────

def test_classify_style_predator_consumption():
    assert personality._classify_style("The lion is hoarding memory again.") == "predator_consumption"


def test_classify_style_plain_when_no_category_matches():
    assert personality._classify_style("Nothing much going on.") == "plain"


def test_classify_style_silent_for_empty_flavor():
    assert personality._classify_style("") == "silent"
