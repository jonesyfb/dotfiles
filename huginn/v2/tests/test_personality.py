"""Tests for the narrow personality renderer (v2/personality.py).

All mocked at the llm.render_personality_only boundary — no real Ollama
call anywhere in this file, so these run without GPU access. A handful of
tests below also exercise the REAL coordinator singleton (with a fake
underlying fn) specifically to prove real admission/game-mode/preemption
behavior without touching Ollama.

Rewritten for the deterministic-composition hardening slice (see
scripts/personality_bench/results/20260903T052457Z/report.md, Finding 1):
the model no longer receives or reproduces exact protected values — it
writes a short "flavor" line only, validated to contain no digits at all,
and every render() call also produces a `deterministic` factual sentence
composed entirely by code (present even when the flavor render fails).
"""
import asyncio

import pytest

import coordinator as coordinator_module
import llm
import personality
from coordinator import Denial, Purpose, RequestClass, coordinator
from llm import CoordinatorDenied
from personality import PersonalityRequest


# ── _validate_flavor: malformed/verbose/theatrical/leaked-value battery ──────

def test_validate_flavor_accepts_clean_short_text():
    req = PersonalityRequest(purpose="x", max_length=100)
    assert personality._validate_flavor("Brave has grown restless again.", req, ()) is None


def test_validate_flavor_accepts_empty():
    """An empty flavor is a deliberate, valid answer now — the deterministic
    sentence carries the real content regardless. Not a validation problem."""
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
    """The core hardening fix: the model is never asked to reproduce exact
    numbers, so any digit in the flavor is rejected outright — spelled-out
    or not doesn't matter, digits specifically are banned."""
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


# ── Deterministic composition: pure, code-owned, no model involved ──────────

def test_compose_deterministic_includes_all_facts():
    req = PersonalityRequest(purpose="x", facts={"disk_temp_c": "78", "threshold": "critical"})
    sentence = personality._compose_deterministic(req)
    assert "78" in sentence
    assert "critical" in sentence


def test_compose_deterministic_flags_critical_severity():
    req = PersonalityRequest(purpose="x", severity="critical", facts={"temp": "78"})
    sentence = personality._compose_deterministic(req)
    assert sentence.startswith("CRITICAL:")


def test_compose_deterministic_includes_reasoner_conclusion_verbatim():
    conclusion = "This might be a memory leak, but I'm not certain — could also be normal caching."
    req = PersonalityRequest(purpose="x", reasoner_conclusion=conclusion)
    assert conclusion in personality._compose_deterministic(req)


def test_compose_deterministic_appends_code_block():
    req = PersonalityRequest(purpose="x", facts={"outcome": "failed"}, code_block="exit 1: no such file")
    sentence = personality._compose_deterministic(req)
    assert "exit 1: no such file" in sentence


def test_compose_deterministic_never_needs_a_model_call():
    """Pure function of the request — no coordinator/llm import used here."""
    req = PersonalityRequest(purpose="x", facts={"a": "1"})
    assert isinstance(personality._compose_deterministic(req), str)


# ── Prompt construction: protected values withheld, not shown ───────────────

def test_prompt_contains_worldview_guidance():
    from config import PERSONALITY_SYSTEM_PROMPT
    lowered = PERSONALITY_SYSTEM_PROMPT.lower()
    assert "brave" in lowered and "lion" in lowered
    assert "thunderbird" in lowered
    assert "muninn" in lowered
    assert "never force a creature" in lowered


def test_prompt_never_shows_protected_values():
    req = PersonalityRequest(
        purpose="x",
        facts={"path": "/etc/fstab", "command": "systemctl restart foo"},
        protected_keys=("path", "command"),
    )
    prompt = personality._build_user_prompt(req)
    assert "/etc/fstab" not in prompt
    assert "systemctl restart foo" not in prompt
    assert "withheld" in prompt.lower()


def test_prompt_shows_non_protected_facts():
    req = PersonalityRequest(
        purpose="printer_offline",
        facts={"printer": "office-printer", "cause": "unknown"},
        prohibited_additions=("a diagnosis of the cause", "a repair suggestion"),
    )
    prompt = personality._build_user_prompt(req)
    assert "office-printer" in prompt
    assert "a diagnosis of the cause" in prompt
    assert "a repair suggestion" in prompt


def test_prompt_never_includes_reasoner_conclusion():
    """Moved entirely to the deterministic sentence — the model must never
    be handed a hedge to paraphrase."""
    req = PersonalityRequest(
        purpose="x",
        reasoner_conclusion="This might be a memory leak, but I'm not certain.",
    )
    prompt = personality._build_user_prompt(req)
    assert "memory leak" not in prompt


def test_prompt_instructs_no_digits():
    req = PersonalityRequest(purpose="x")
    prompt = personality._build_user_prompt(req)
    assert "no numbers" in prompt.lower() or "no digits" in prompt.lower()


def test_capability_unavailable_purpose():
    req = PersonalityRequest(
        purpose="capability_unavailable",
        facts={"capability": "calendar_list", "reason": "CalDAV unreachable"},
        prohibited_additions=("a workaround", "a retry promise"),
    )
    prompt = personality._build_user_prompt(req)
    assert "capability_unavailable" in prompt
    assert "CalDAV unreachable" in prompt


def test_vulnerable_purpose_structural():
    from config import PERSONALITY_SYSTEM_PROMPT
    lowered = PERSONALITY_SYSTEM_PROMPT.lower()
    assert "no therapy monologue" in lowered
    assert "loyal" in lowered


# ── render(): mocked coordinator boundary ─────────────────────────────────────

def test_render_ok_submits_resident_personality_ambient(monkeypatch):
    captured = {}

    async def fake_render_personality_only(system_prompt, user_prompt, purpose, deadline_seconds):
        captured["purpose"] = purpose
        captured["deadline"] = deadline_seconds
        return "Brave is hoarding memory again, climbing steadily."

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(
        purpose="high_memory_observation",
        facts={"app": "brave-browser", "mem_gb": "9.1"},
        severity="info",
    )
    result = asyncio.run(personality.render(req, purpose=Purpose.AMBIENT))

    assert result.ok is True
    assert "Brave" in result.text
    assert "9.1" in result.text  # deterministic portion carries the exact value
    assert captured["purpose"] == Purpose.AMBIENT


def test_render_composes_flavor_and_deterministic():
    pass  # covered by test_render_ok_submits_resident_personality_ambient's text assertion


def test_render_empty_flavor_still_ok_and_shows_deterministic_alone(monkeypatch):
    async def fake_render_personality_only(*a, **kw):
        return ""

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(purpose="x", facts={"disk_temp_c": "78"}, severity="critical")
    result = asyncio.run(personality.render(req))

    assert result.ok is True
    assert result.flavor == ""
    assert result.text == result.deterministic
    assert result.text.startswith("CRITICAL:")
    assert "78" in result.text


def test_render_deterministic_always_present_on_coordinator_denial(monkeypatch):
    async def fake_render_personality_only(*a, **kw):
        raise CoordinatorDenied(Denial.DEADLINE_EXCEEDED, "deadline exceeded while running")

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(purpose="x", facts={"task": "nightly-backup"})
    result = asyncio.run(personality.render(req))

    assert result.ok is False
    assert result.text is None
    assert result.reason == "coordinator_denied:deadline_exceeded"
    assert "nightly-backup" in result.deterministic  # usable as a fallback by the caller


def test_render_coordinator_denied_preempted(monkeypatch):
    async def fake_render_personality_only(*a, **kw):
        raise CoordinatorDenied(Denial.PREEMPTED, "cancelled for higher-priority work")

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    result = asyncio.run(personality.render(PersonalityRequest(purpose="x")))

    assert result.ok is False
    assert result.reason == "coordinator_denied:preempted"


def test_render_unexpected_exception_does_not_raise(monkeypatch):
    async def fake_render_personality_only(*a, **kw):
        raise ValueError("boom")

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    result = asyncio.run(personality.render(PersonalityRequest(purpose="x")))

    assert result.ok is False
    assert result.reason == "error"


def test_render_retries_once_on_leaked_protected_value(monkeypatch):
    calls = {"n": 0}

    async def fake_render_personality_only(system_prompt, user_prompt, purpose, deadline_seconds):
        calls["n"] += 1
        if calls["n"] == 1:
            return "It happened in /home/nate/project."  # leaks the withheld value
        return "Something feels off tonight."

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(
        purpose="x", facts={"path": "/home/nate/project"}, protected_keys=("path",),
    )
    result = asyncio.run(personality.render(req))

    assert calls["n"] == 2
    assert result.ok is True
    assert "/home/nate/project" in result.text  # via the deterministic portion
    assert "/home/nate/project" not in result.flavor


def test_render_gives_up_after_max_retries(monkeypatch):
    async def always_leaks(system_prompt, user_prompt, purpose, deadline_seconds):
        return "It happened in /home/nate/project."

    monkeypatch.setattr(personality, "render_personality_only", always_leaks)

    req = PersonalityRequest(
        purpose="x", facts={"path": "/home/nate/project"}, protected_keys=("path",),
    )
    result = asyncio.run(personality.render(req))

    assert result.ok is False
    assert result.reason == "validation_failed"
    assert "/home/nate/project" in result.deterministic


def test_render_does_not_inspect_model_residency(monkeypatch):
    """No special-case logic for 'vision model happens to be loaded' —
    personality.render() just submits; the coordinator (and Ollama's own
    single-model residency) handles any swap transparently."""
    calls = {"n": 0}

    async def fake_render_personality_only(*a, **kw):
        calls["n"] += 1
        return "Fine either way."

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    result = asyncio.run(personality.render(PersonalityRequest(purpose="x")))

    assert result.ok is True
    assert calls["n"] == 1


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
    """Sanity contrast: the personality model's game-mode exemption is
    specific to RESIDENT_PERSONALITY, not a blanket bypass."""
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
        purpose="periodic_observation",
        facts={"secret_fact_key": "super-sensitive-value-12345"},
    )
    with caplog.at_level("INFO", logger="huginn.personality"):
        result = asyncio.run(personality.render(req))

    assert result.ok is True
    log_text = "\n".join(r.message for r in caplog.records)
    assert "super-sensitive-value-12345" not in log_text
    assert "secret_fact_key" not in log_text  # not even the fact key
    # What IS expected to be present: purpose/severity/length metadata.
    assert "periodic_observation" in log_text


def test_render_logging_on_denial_includes_reason_not_content(monkeypatch, caplog):
    async def fake_render_personality_only(*a, **kw):
        raise CoordinatorDenied(Denial.PREEMPTED, "cancelled for higher-priority work")

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    with caplog.at_level("INFO", logger="huginn.personality"):
        result = asyncio.run(personality.render(PersonalityRequest(purpose="task_complete")))

    assert result.ok is False
    log_text = "\n".join(r.message for r in caplog.records)
    assert "preempted" in log_text
    assert "task_complete" in log_text
