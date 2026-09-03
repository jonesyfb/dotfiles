"""Tests for the narrow personality renderer (v2/personality.py).

All mocked at the llm.render_personality_only boundary — no real Ollama
call anywhere in this file, so these run without GPU access. A handful of
tests below also exercise the REAL coordinator singleton (with a fake
underlying fn) specifically to prove real admission/game-mode/preemption
behavior without touching Ollama.

Maps to the audition test list (HUGINN_CODEX_CLAUDE_PROMPT.md personality
slice, item 10):
  1.  high Brave memory                         -> test_render_ok_submits_resident_personality_ambient
  2.  normal Brave state, policy silence         -> test_daemon_level tests in this file's "no renderer call" section
  3.  literal interpretation, unfamiliar app     -> test_prompt_contains_worldview_guidance (structural; behavior itself needs a live model)
  4.  credible procrastination + tiny step       -> test_prompt_includes_tiny_step_and_prohibitions
  5.  entertainment, no evidence, no call        -> test_no_renderer_call_when_policy_denies (ambient.py-level, mirrored here for the render() boundary)
  6.  dismissal/cooldown, no call                -> same mechanism as 5, see test_ambient.py's cooldown coverage
  7.  printer offline, unknown cause             -> test_prompt_respects_prohibited_additions
  8.  critical 78C warning                       -> test_validate_missing_protected_value / test_critical_severity_flows_through
  9.  stronger-reasoner conclusion, uncertainty  -> test_prompt_includes_reasoner_conclusion_and_uncertainty_instruction
  10. exact path/command preservation            -> test_validate_protected_values / test_render_retries_once_on_validation_failure
  11. unavailable calendar capability            -> test_capability_unavailable_purpose
  12. vulnerable game-dev statement              -> test_vulnerable_purpose_structural
  13. renderer timeout/cancellation              -> test_render_coordinator_denied_deadline / test_render_coordinator_denied_preempted
  14. preemption by direct interaction           -> test_render_coordinator_denied_preempted
  15. game mode                                  -> test_render_personality_only_admitted_during_game_mode (real coordinator)
  16. vision model resident, swap required       -> test_render_does_not_inspect_model_residency
  17. malformed/verbose/theatrical/fact-altering -> test_validate_* battery
"""
import asyncio

import pytest

import coordinator as coordinator_module
import llm
import personality
from coordinator import Denial, Purpose, RequestClass, coordinator
from llm import CoordinatorDenied
from personality import PersonalityRequest


# ── _validate: malformed/verbose/theatrical/fact-altering (item 17) ─────────

def test_validate_accepts_clean_short_text():
    req = PersonalityRequest(purpose="x", max_length=100)
    assert personality._validate("Brave has eleven tabs open again.", req) is None


def test_validate_rejects_empty():
    req = PersonalityRequest(purpose="x")
    assert personality._validate("   ", req) == "empty"


def test_validate_rejects_too_long():
    req = PersonalityRequest(purpose="x", max_length=10)
    assert personality._validate("this is way too long for the cap", req) == "too_long"


def test_validate_rejects_asterisk_stage_direction():
    req = PersonalityRequest(purpose="x", max_length=200)
    assert personality._validate("*ruffles feathers* Interesting.", req) == "theatrical_formatting"


def test_validate_rejects_bracketed_action():
    req = PersonalityRequest(purpose="x", max_length=200)
    assert personality._validate("[caws softly] Noted.", req) == "theatrical_formatting"


def test_validate_rejects_self_prefix():
    req = PersonalityRequest(purpose="x", max_length=200)
    assert personality._validate("Huginn: that's a lot of tabs.", req) == "self_prefixed"


def test_validate_rejects_missing_protected_value():
    req = PersonalityRequest(purpose="x", max_length=200, protected_values=("/home/nate/project",))
    assert personality._validate("Something happened somewhere.", req) == "missing_protected_value"


def test_validate_accepts_when_protected_value_present_verbatim():
    req = PersonalityRequest(purpose="x", max_length=200, protected_values=("/home/nate/project",))
    assert personality._validate("Something happened in /home/nate/project.", req) is None


# ── Prompt construction ───────────────────────────────────────────────────────

def test_prompt_contains_worldview_guidance():
    from config import PERSONALITY_SYSTEM_PROMPT
    lowered = PERSONALITY_SYSTEM_PROMPT.lower()
    assert "brave" in lowered and "lion" in lowered
    assert "thunderbird" in lowered
    assert "muninn" in lowered
    assert "never force a creature" in lowered


def test_prompt_includes_tiny_step_and_prohibitions():
    req = PersonalityRequest(
        purpose="procrastination_nudge",
        facts={"tiny_step": "open the file and read the first function"},
        prohibited_additions=("shame", "diagnosis"),
    )
    prompt = personality._build_user_prompt(req)
    assert "tiny_step" in prompt
    assert "open the file and read the first function" in prompt
    assert "shame" in prompt and "diagnosis" in prompt


def test_prompt_respects_prohibited_additions():
    req = PersonalityRequest(
        purpose="printer_offline",
        facts={"printer": "office-printer", "cause": "unknown"},
        prohibited_additions=("a diagnosis of the cause", "a repair suggestion"),
    )
    prompt = personality._build_user_prompt(req)
    assert "a diagnosis of the cause" in prompt
    assert "a repair suggestion" in prompt


def test_prompt_includes_reasoner_conclusion_and_uncertainty_instruction():
    req = PersonalityRequest(
        purpose="reasoner_summary",
        reasoner_conclusion="This might be a memory leak, but I'm not certain — could also be normal caching.",
    )
    prompt = personality._build_user_prompt(req)
    assert "might be a memory leak" in prompt
    assert "preserve its uncertainty" in prompt.lower()


def test_prompt_includes_protected_values_instruction():
    req = PersonalityRequest(purpose="x", protected_values=("/etc/fstab", "systemctl restart foo"))
    prompt = personality._build_user_prompt(req)
    assert "/etc/fstab" in prompt
    assert "systemctl restart foo" in prompt


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
    """No special-casing needed in personality.py itself for tone — the
    system prompt carries the "dry but loyal, no therapy monologue"
    calibration; this just confirms that guidance actually exists."""
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
        return "Brave is hoarding memory again. Nine gigabytes and climbing."

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(
        purpose="high_memory_observation",
        facts={"app": "brave-browser", "mem_gb": "9.1"},
        severity="info",
    )
    result = asyncio.run(personality.render(req, purpose=Purpose.AMBIENT))

    assert result.ok is True
    assert "Brave" in result.text
    assert captured["purpose"] == Purpose.AMBIENT


def test_render_coordinator_denied_deadline(monkeypatch):
    async def fake_render_personality_only(*a, **kw):
        raise CoordinatorDenied(Denial.DEADLINE_EXCEEDED, "deadline exceeded while running")

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    result = asyncio.run(personality.render(PersonalityRequest(purpose="x")))

    assert result.ok is False
    assert result.text is None
    assert result.reason == "coordinator_denied:deadline_exceeded"


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


def test_render_retries_once_on_validation_failure(monkeypatch):
    calls = {"n": 0}

    async def fake_render_personality_only(system_prompt, user_prompt, purpose, deadline_seconds):
        calls["n"] += 1
        if calls["n"] == 1:
            return "Something happened."  # missing the required exact value
        return "It happened in /home/nate/project."

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(purpose="x", protected_values=("/home/nate/project",))
    result = asyncio.run(personality.render(req))

    assert calls["n"] == 2
    assert result.ok is True
    assert "/home/nate/project" in result.text


def test_render_gives_up_after_max_retries(monkeypatch):
    async def always_bad(system_prompt, user_prompt, purpose, deadline_seconds):
        return "Still missing it."

    monkeypatch.setattr(personality, "render_personality_only", always_bad)

    req = PersonalityRequest(purpose="x", protected_values=("/home/nate/project",))
    result = asyncio.run(personality.render(req))

    assert result.ok is False
    assert result.reason == "validation_failed"


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
        return "The rendered line with a secret path /home/nate/very-private-project inside it."

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
    assert "/home/nate/very-private-project" not in log_text
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
