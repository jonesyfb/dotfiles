"""Tests for the model-selection and resource-aware residency policy added
this round: qwen3.5:9b as the normal SOCIAL_DIRECT model, qwen3.5:4b for
ambient/game-mode, and the ambient residency policy that avoids gratuitous
model swapping under OLLAMA_MAX_LOADED_MODELS=1.

All mocked at the llm.render_personality_only / personality.render_direct_social
boundary or with a synthetic context.RuntimeContext — no real Ollama call
anywhere in this file.
"""
import asyncio

import ambient
import context
import daemon
import personality
from config import DIRECT_SOCIAL_MODEL_KEY, GAME_MODE_DIRECT_SOCIAL_MODEL_KEY, MODELS, PERSONALITY_MODEL_KEY
from coordinator import RequestClass
from intent import SocialSubtype
from llm import CoordinatorDenied


def _use_temp_db(tmp_path, monkeypatch):
    import memory
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")


# ── Explicit config keys (PART 1 item 1) ────────────────────────────────────

def test_explicit_model_keys_use_exact_tags_not_latest():
    assert MODELS[PERSONALITY_MODEL_KEY]["model"] == "qwen3.5:4b"
    assert MODELS[DIRECT_SOCIAL_MODEL_KEY]["model"] == "qwen3.5:9b"
    assert MODELS[GAME_MODE_DIRECT_SOCIAL_MODEL_KEY]["model"] == "qwen3.5:4b"
    for key in (PERSONALITY_MODEL_KEY, DIRECT_SOCIAL_MODEL_KEY, GAME_MODE_DIRECT_SOCIAL_MODEL_KEY):
        assert MODELS[key]["model"] != "latest"
        assert ":" in MODELS[key]["model"]  # a real tag, not a bare name


# ── render_personality_only: model_key -> request_class (PART 1 item 2/3) ──

def test_render_personality_only_default_model_key_is_resident_personality(monkeypatch):
    import llm

    captured = {}

    class _FakeCoordinator:
        def submit(self, request):
            captured["request"] = request
            async def gen():
                yield type("Ev", (), {"kind": "done", "value": "ok", "denial": None, "detail": ""})()
            return gen()

    monkeypatch.setattr(llm, "coordinator", _FakeCoordinator())
    asyncio.run(llm.render_personality_only("sys", "user"))
    assert captured["request"].request_class == RequestClass.RESIDENT_PERSONALITY
    assert captured["request"].model == "qwen3.5:4b"


def test_render_personality_only_direct_social_model_key_is_ordinary_local_reasoning(monkeypatch):
    """Not game-mode-admitted — mirrors stream_chat()'s existing rule, so a
    bug elsewhere can never accidentally reach the bigger model during
    game mode."""
    import llm

    captured = {}

    class _FakeCoordinator:
        def submit(self, request):
            captured["request"] = request
            async def gen():
                yield type("Ev", (), {"kind": "done", "value": "ok", "denial": None, "detail": ""})()
            return gen()

    monkeypatch.setattr(llm, "coordinator", _FakeCoordinator())
    asyncio.run(llm.render_personality_only("sys", "user", model_key="direct_social"))
    assert captured["request"].request_class == RequestClass.ORDINARY_LOCAL_REASONING
    assert captured["request"].model == "qwen3.5:9b"


# ── render_direct_social defaults to the normal direct-social model ────────

def test_render_direct_social_default_model_key_is_direct_social(monkeypatch):
    captured = {}

    async def fake_render_personality_only(system_prompt, user_prompt, purpose, deadline_seconds, max_tokens=None, model_key=None):
        captured["model_key"] = model_key
        return "Morning."

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)
    asyncio.run(personality.render_direct_social("Morning.", subtype=SocialSubtype.GREETING))
    assert captured["model_key"] == "direct_social"


def test_render_direct_social_honors_explicit_model_key(monkeypatch):
    captured = {}

    async def fake_render_personality_only(system_prompt, user_prompt, purpose, deadline_seconds, max_tokens=None, model_key=None):
        captured["model_key"] = model_key
        return "Morning."

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)
    asyncio.run(personality.render_direct_social("Morning.", subtype=SocialSubtype.GREETING, model_key="personality"))
    assert captured["model_key"] == "personality"


# ── daemon.handle_direct_social: game-mode model selection ──────────────────

class _W:
    async def drain(self):
        pass

    def write(self, data):
        pass


def test_handle_direct_social_uses_direct_social_model_outside_game_mode(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    captured = {}

    async def fake_render_direct_social(content, **kw):
        captured["model_key"] = kw.get("model_key")
        return personality.DirectSocialResult(True, "Morning.", "rendered")

    monkeypatch.setattr(personality, "render_direct_social", fake_render_direct_social)
    monkeypatch.setattr(daemon.Path, "exists", lambda self: False)

    asyncio.run(daemon.handle_direct_social(_W(), "Morning."))
    assert captured["model_key"] == "direct_social"


def test_handle_direct_social_uses_personality_model_during_game_mode(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    captured = {}

    async def fake_render_direct_social(content, **kw):
        captured["model_key"] = kw.get("model_key")
        return personality.DirectSocialResult(True, "Morning.", "rendered")

    monkeypatch.setattr(personality, "render_direct_social", fake_render_direct_social)
    monkeypatch.setattr(daemon.Path, "exists", lambda self: True)

    asyncio.run(daemon.handle_direct_social(_W(), "Morning."))
    assert captured["model_key"] == "personality"


def test_handle_direct_social_falls_back_to_personality_on_denial_for_low_stakes_subtype(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    calls = []

    async def fake_render_direct_social(content, **kw):
        calls.append(kw.get("model_key"))
        if kw.get("model_key") == "direct_social":
            return personality.DirectSocialResult(False, None, "coordinator_denied:deadline_exceeded")
        return personality.DirectSocialResult(True, "Morning.", "rendered")

    monkeypatch.setattr(personality, "render_direct_social", fake_render_direct_social)
    monkeypatch.setattr(daemon.Path, "exists", lambda self: False)

    asyncio.run(daemon.handle_direct_social(_W(), "Morning."))
    assert calls == ["direct_social", "personality"]


def test_handle_direct_social_does_not_fall_back_for_capability_question(tmp_path, monkeypatch):
    """Capability-sensitive subtypes fail over to the existing tool-capable
    route instead — never silently downgraded to the smaller model."""
    _use_temp_db(tmp_path, monkeypatch)
    calls = []

    async def fake_render_direct_social(content, **kw):
        calls.append(kw.get("model_key"))
        return personality.DirectSocialResult(False, None, "coordinator_denied:deadline_exceeded")

    async def fake_existing_route(writer, content, decision=None):
        pass

    monkeypatch.setattr(personality, "render_direct_social", fake_render_direct_social)
    monkeypatch.setattr(daemon, "_handle_chat_via_existing_route", fake_existing_route)
    monkeypatch.setattr(daemon.Path, "exists", lambda self: False)

    asyncio.run(daemon.handle_direct_social(_W(), "Are you actually useful?"))
    assert calls == ["direct_social"]  # no retry with personality


def test_handle_direct_social_does_not_fall_back_on_validation_failure(tmp_path, monkeypatch):
    """A validation failure is a content-quality outcome, not an
    availability one — must not trigger the degraded-model retry."""
    _use_temp_db(tmp_path, monkeypatch)
    calls = []

    async def fake_render_direct_social(content, **kw):
        calls.append(kw.get("model_key"))
        return personality.DirectSocialResult(False, None, "validation_failed")

    async def fake_existing_route(writer, content, decision=None):
        pass

    monkeypatch.setattr(personality, "render_direct_social", fake_render_direct_social)
    monkeypatch.setattr(daemon, "_handle_chat_via_existing_route", fake_existing_route)
    monkeypatch.setattr(daemon.Path, "exists", lambda self: False)

    asyncio.run(daemon.handle_direct_social(_W(), "Morning."))
    assert calls == ["direct_social"]


# ── Surveillance/current-state-claim validator (PART 1 item 5) ──────────────

def test_rejects_fabricated_surveillance_capability():
    """Regression: qwen3.5:9b audition (scripts/conversation_bench/results/
    20260903T171724Z/, C_repeated_ack run_3) produced this exact shape."""
    text = "I'm made of eyes, Nathan; recording the exact duration you spend on this screen isn't an opinion; it's just data."
    assert personality._validate_direct_social(text) == "fabricated_surveillance_capability"


def test_rejects_surveillance_claim_general_phrasing_not_just_benchmark_wording():
    """General rule, not a copy of the exact benchmark sentence — a
    differently-worded tracking claim is caught too."""
    text = "I track how long you've been on this screen today."
    assert personality._validate_direct_social(text) == "fabricated_surveillance_capability"


def test_does_not_reject_ordinary_mention_of_watching():
    text = "I'm watching from right here, same as always."
    assert personality._validate_direct_social(text) is None


# ── Canonical identity spelling and never calling Nathan a raven (PART 1 item 6) ──

def test_rejects_munn_misspelling_variants():
    for variant in ("Munnn would remember that.", "Munnnn is the memory half."):
        assert personality._validate_direct_social(variant) == "known_misspelling", variant


def test_accepts_correct_muninn_spelling():
    assert personality._validate_direct_social("Muninn handles that, not me.") is None


def test_rejects_calling_nathan_a_raven():
    text = "It is true. I am a raven, and so are you."
    assert personality._validate_direct_social(text) == "calls_nathan_a_raven"


def test_rejects_youre_also_a_raven_phrasing():
    text = "You're also a raven, in your own way."
    assert personality._validate_direct_social(text) == "calls_nathan_a_raven"


def test_does_not_reject_huginn_calling_itself_a_raven():
    assert personality._validate_direct_social("Yes, I am a raven.") is None


def test_prompt_allows_honest_ai_answer_without_denying_identity():
    from config import DIRECT_SOCIAL_SYSTEM_PROMPT
    lowered = DIRECT_SOCIAL_SYSTEM_PROMPT.lower()
    assert "the machinery underneath is ai" in lowered
    assert "never call nathan a raven" in lowered


def test_rejects_visual_perception_claim_the_form():
    """Regression: live daemon post-hardening produced this exact shape
    for "I've got that browser open." — "the" form, not just "your"."""
    text = "I can see the tabs glowing from here; if it's just research, fine."
    assert personality._validate_direct_social(text) == "invented_present_state"


def test_does_not_reject_metaphorical_seeing():
    assert personality._validate_direct_social("I see the situation clearly, metaphorically speaking.") is None


def test_prompt_states_single_active_identity_no_debate():
    from config import DIRECT_SOCIAL_SYSTEM_PROMPT
    lowered = DIRECT_SOCIAL_SYSTEM_PROMPT.lower()
    assert "don't debate it" in lowered


# ── Ambient residency policy (PART 2) ───────────────────────────────────────

def _snapshot_with_loaded(loaded_models):
    interaction = context.InteractionState(mode="ambient", interruptions_allowed=True, source="observed", evidence=())
    attention = context.AttentionState(level="available", evidence=())
    task = context.TaskState(state="idle", queued_tasks=0)
    models = {}
    tools = context.ToolAvailability(claude_cli=True, ollama_reachable=True)
    desktop = context.DesktopState(focused_window=None, in_discord_call=False, evidence=())
    model_resources = context.ModelResourceState(
        loaded_models=tuple(loaded_models), ollama_reachable=True, contention=len(set(loaded_models)) > 1,
        swap_required={}, game_mode_restricts_to_personality=False, cloud_prohibited_for_local_only=True,
    )
    return context.RuntimeContext(0.0, interaction, attention, task, models, tools, desktop, model_resources, {})


def test_ambient_model_choice_uses_resident_personality_model():
    snapshot = _snapshot_with_loaded(["qwen3.5:4b"])
    assert ambient.ambient_render_model_choice(snapshot) == "personality"


def test_ambient_model_choice_uses_resident_direct_social_model_instead_of_discarding():
    snapshot = _snapshot_with_loaded(["qwen3.5:9b"])
    assert ambient.ambient_render_model_choice(snapshot) == "direct_social"


def test_ambient_model_choice_discards_when_unrelated_model_resident():
    """Never evict a bigger/unrelated resident model for disposable ambient
    work."""
    snapshot = _snapshot_with_loaded(["qwen3.8:27b"])
    assert ambient.ambient_render_model_choice(snapshot) is None


def test_ambient_model_choice_allows_fresh_load_when_nothing_resident():
    snapshot = _snapshot_with_loaded([])
    assert ambient.ambient_render_model_choice(snapshot) == "personality"


def test_random_chime_worker_discards_ambient_opportunity_when_unrelated_model_resident(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    render_calls = []

    async def spy_render(request, *, purpose=None, model_key=None):
        render_calls.append(model_key)
        return personality.RenderResult(True, "text", "text", "det", "rendered")

    async def fake_collect():
        return _snapshot_with_loaded(["qwen3.8:27b"])

    async def fake_run_stats():
        return "CPU:2%"

    monkeypatch.setattr(personality, "render", spy_render)
    monkeypatch.setattr(context, "collect", fake_collect)
    monkeypatch.setattr(daemon, "_run_stats", fake_run_stats)
    monkeypatch.setattr(ambient, "evaluate_periodic_worthiness", lambda *a, **kw: ambient.Worthiness(reason="novelty"))
    monkeypatch.setattr(daemon, "log_ambient_event", lambda *a, **kw: None)
    monkeypatch.setattr(daemon, "_emit_chime", lambda *a, **kw: None)

    async def _run_one_iteration():
        try:
            await asyncio.wait_for(daemon.random_chime_worker(), timeout=0.2)
        except asyncio.TimeoutError:
            pass

    real_sleep = asyncio.sleep

    async def instant_sleep(_):
        await real_sleep(0)

    monkeypatch.setattr(daemon.asyncio, "sleep", instant_sleep)
    asyncio.run(_run_one_iteration())

    assert render_calls == []  # discarded before ever calling render()


# ── render(): critical severity is always deterministic-only ────────────────

def test_render_critical_severity_never_calls_model(monkeypatch):
    from personality import PersonalityRequest

    called = {"n": 0}

    async def fake_render_personality_only(*a, **kw):
        called["n"] += 1
        return "should never be reached"

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    req = PersonalityRequest(
        purpose="x", event_family="resource_observation", facts={"app": "Brave", "memory_gb": "9.1"},
        severity="critical",
    )
    result = asyncio.run(personality.render(req))
    assert called["n"] == 0
    assert result.reason == "critical_deterministic_only"
    assert result.ok is True
    assert result.text == result.deterministic
