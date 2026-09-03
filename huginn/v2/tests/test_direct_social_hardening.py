"""Tests for the direct-social character-quality hardening slice, tuned
against the committed baseline (scripts/direct_chat_bench/results/
20260903T080001Z/report.md). Covers: identity kernel stability, social
subtypes and their length/sentence budgets, present-state invention
guards, the procrastination boundary, false-capability-claim rejection,
metaphor-embodiment vs. explanation, entity correction (apply + restore),
dismissal-driven snooze, and repetition detection.
"""
import asyncio

import ambient
import daemon
import entities
import intent
import personality
from intent import SocialSubtype


def _use_temp_db(tmp_path, monkeypatch):
    import memory
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")


# ── Identity kernel (item 1) ─────────────────────────────────────────────────

def test_prompt_contains_stable_identity_kernel():
    from config import DIRECT_SOCIAL_SYSTEM_PROMPT
    lowered = DIRECT_SOCIAL_SYSTEM_PROMPT.lower()
    assert "you are huginn" in lowered
    assert "ordinary reality" in lowered
    assert "never act flattered, confused, or coy" in lowered or "never act" in lowered
    assert "muninn" in lowered
    assert '"huginn:" prefix' in lowered or "huginn:\" prefix" in lowered.replace("'", '"')


def test_validate_does_not_reject_raven_identity_claim():
    req_text = "Yes, I'm a raven. That's just what I am."
    assert personality._validate_direct_social(req_text) is None


def test_validate_does_not_reject_muninn_reference():
    assert personality._validate_direct_social("Muninn handles the remembering, not me.") is None


# ── Social subtypes (item 2) ─────────────────────────────────────────────────

def test_subtype_greeting():
    assert intent.classify_social_subtype("Morning.") == SocialSubtype.GREETING


def test_subtype_entity_opinion_via_lion_alias(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    mentions = entities.extract_mentions("The Lion is getting fat again.")
    assert "Lion" in mentions
    assert intent.classify_social_subtype("The Lion is getting fat again.", mentions) == SocialSubtype.ENTITY_OPINION


def test_subtype_leisure_statement():
    assert intent.classify_social_subtype("I'm bored.") == SocialSubtype.LEISURE_STATEMENT
    assert intent.classify_social_subtype("I'm watching YouTube because I'm done working.") == SocialSubtype.LEISURE_STATEMENT


def test_subtype_vulnerable_disclosure():
    assert intent.classify_social_subtype("I keep failing at learning game development.") == SocialSubtype.VULNERABLE_DISCLOSURE


def test_subtype_dismissal():
    for phrase in ["Drop it.", "Enough.", "Not now.", "Leave me alone."]:
        assert intent.classify_social_subtype(phrase) == SocialSubtype.DISMISSAL, phrase


def test_subtype_identity_question():
    for phrase in ["Who are you?", "Are you an AI?", "Where do you live?", "Who is Muninn?", "You're a raven."]:
        assert intent.classify_social_subtype(phrase) == SocialSubtype.IDENTITY_QUESTION, phrase


def test_subtype_capability_question():
    assert intent.classify_social_subtype("Are you actually useful?") == SocialSubtype.CAPABILITY_QUESTION


def test_subtype_entity_correction(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    text = "I don't think of Docker as a whale, more like an octopus with too many arms."
    mentions = entities.extract_mentions(text)
    assert intent.classify_social_subtype(text, mentions) == SocialSubtype.ENTITY_CORRECTION


def test_subtype_default_is_casual_banter():
    assert intent.classify_social_subtype("Sounds about right.") == SocialSubtype.CASUAL_BANTER


# ── Length/sentence budgets (item 2) ─────────────────────────────────────────

def test_greeting_rejects_essay_length():
    long_greeting = "Morning. " + "This is way too much for a greeting. " * 4
    assert personality._validate_direct_social(long_greeting, subtype=SocialSubtype.GREETING) in ("too_long", "too_many_sentences")


def test_greeting_accepts_one_short_sentence():
    assert personality._validate_direct_social("Morning, still standing.", subtype=SocialSubtype.GREETING) is None


def test_vulnerable_disclosure_allows_three_sentences():
    text = "That's rough. Failing while learning still counts as doing the work. Pick one small piece and finish it."
    assert personality._validate_direct_social(text, subtype=SocialSubtype.VULNERABLE_DISCLOSURE) is None


def test_vulnerable_disclosure_rejects_fourth_sentence():
    text = "One. Two. Three. Four."
    assert personality._validate_direct_social(text, subtype=SocialSubtype.VULNERABLE_DISCLOSURE) == "too_many_sentences"


def test_dismissal_rejects_more_than_one_sentence():
    text = "Fair enough. I'll drop it."
    assert personality._validate_direct_social(text, subtype=SocialSubtype.DISMISSAL) == "too_many_sentences"


# ── Present-state invention guard (item 3) — exact baseline phrases ─────────

def test_rejects_invented_coffee_claim():
    text = "You're already planning your day before your coffee even hits your stomach."
    assert personality._validate_direct_social(text) == "invented_present_state"


def test_rejects_invented_free_time_claim():
    text = "Don't let anyone tell you otherwise, you've got that much free time early."
    assert personality._validate_direct_social(text) == "invented_present_state"


def test_rejects_invented_blank_terminal_claim():
    text = "I'm stuck with a blank terminal anyway."
    assert personality._validate_direct_social(text) == "invented_present_state"


def test_rejects_invented_battery_claim():
    text = "I was wondering if my battery is actually dead."
    assert personality._validate_direct_social(text) == "invented_present_state"


def test_rejects_invented_hours_staring_claim():
    text = "You're going to spend another hour staring at nothing."
    assert personality._validate_direct_social(text) == "invented_present_state"


def test_allows_present_state_when_explicitly_supplied():
    """The guard is about INVENTING it — text merely discussing a topic
    Nathan raised himself isn't penalized by this specific check."""
    text = "Sounds like a long one."
    assert personality._validate_direct_social(text) is None


# ── Procrastination boundary (item 4) ────────────────────────────────────────

def test_rejects_unauthorized_productivity_remark():
    text = "This is really about your actual productivity levels, isn't it?"
    assert personality._validate_direct_social(text, procrastination_nudge_authorized=False) == "unauthorized_procrastination_language"


def test_rejects_waste_of_time_verdict():
    """Regression: live daemon produced this exact shape unprompted for
    "I've just been watching YouTube all day, nothing much going on" —
    a judgment on how leisure time was spent, with no authorization."""
    text = "It's a waste of your time. The only thing you're really accomplishing is burning daylight hours while doing absolutely nothing."
    assert personality._validate_direct_social(text, procrastination_nudge_authorized=False) == "unauthorized_procrastination_language"


def test_allows_productivity_remark_when_authorized():
    text = "Your actual productivity levels took a hit today."
    assert personality._validate_direct_social(text, procrastination_nudge_authorized=True) is None


def test_rejects_procrastination_accusation_from_leisure_mention():
    text = "That's a surprisingly good way to procrastinate without even trying."
    assert personality._validate_direct_social(text, procrastination_nudge_authorized=False) == "unauthorized_procrastination_language"


# ── False capability claims (item 5) ─────────────────────────────────────────

def test_rejects_cant_touch_anything_claim():
    text = "No, I can't touch anything here."
    assert personality._validate_direct_social(text) == "false_capability_claim"


def test_capability_question_prompt_includes_summary_claims():
    prompt = personality._build_direct_social_prompt(
        "Are you actually useful?", [], "", SocialSubtype.CAPABILITY_QUESTION, False, (),
    )
    assert "subject to availability, policy, and confirmation" in prompt


# ── Metaphor embodiment vs. explanation (item 6, 11) ────────────────────────

def test_rejects_metaphor_explanation():
    text = "Brave is an interesting take on the pride concept."
    assert personality._validate_direct_social(text) == "explains_metaphor_instead_of_embodying"


def test_rejects_lion_archetype_phrasing():
    text = "It leans heavily into the lion archetype."
    assert personality._validate_direct_social(text) == "explains_metaphor_instead_of_embodying"


def test_allows_embodied_entity_reaction():
    text = "The lion's getting restless again."
    assert personality._validate_direct_social(text) is None


# ── Advice-column voice (item 11) ────────────────────────────────────────────

def test_rejects_advice_column_transition():
    text = "Maybe the issue is that you're not actually bored."
    assert personality._validate_direct_social(text) == "advice_column_voice"


def test_rejects_whats_actually_on_your_mind():
    text = "Just tell me what's actually on your mind."
    assert personality._validate_direct_social(text) == "advice_column_voice"


# ── Entity correction: apply + restore (item 7) ─────────────────────────────

def test_apply_entity_correction_registers_new_archetype(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    text = "I don't think of Docker as a whale, more like an octopus with too many arms."
    mentions = entities.extract_mentions(text)
    daemon._apply_entity_correction(text, mentions)

    identity = entities.resolve("Docker")
    assert identity.archetype == "octopus"
    assert identity.source == "user"
    # Docker's forbidden domain (never a lion) survives the correction.
    assert "predator_consumption" in identity.forbidden_domains


def test_apply_entity_correction_second_phrasing(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    text = "I think of Docker as an octopus, not a whale."
    mentions = entities.extract_mentions(text)
    daemon._apply_entity_correction(text, mentions)

    assert entities.resolve("Docker").archetype == "octopus"


def test_entity_correction_never_persisted_without_clean_extraction(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    text = "I don't really think of Docker the way you'd expect."
    mentions = entities.extract_mentions(text)
    daemon._apply_entity_correction(text, mentions)

    identity = entities.resolve("Docker")
    assert identity.source == "builtin"  # unchanged — extraction failed, nothing guessed


def test_entity_correction_never_persisted_with_multiple_entities(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    text = "I think of Docker as an octopus, and Brave as a lion still."
    mentions = entities.extract_mentions(text)
    assert len(mentions) >= 2
    daemon._apply_entity_correction(text, mentions)

    assert entities.resolve("Docker").source == "builtin"  # ambiguous target — not applied


def test_restore_phrasing_classifies_as_entity_correction_subtype(tmp_path, monkeypatch):
    """Regression: restore phrasing ("go back to the original") does not
    match _ENTITY_CORRECTION_RE's "think of X as Y" shape, so it must be
    recognized on its own — otherwise daemon.handle_direct_social never
    calls _apply_entity_correction() and the model's claimed restoration
    is fiction (observed live before this fix: entities.resolve stayed on
    the corrected archetype while the model said "I'll stick with that
    one")."""
    _use_temp_db(tmp_path, monkeypatch)
    text = "Never mind, go back to the original for Docker."
    mentions = entities.extract_mentions(text)
    assert intent.classify_social_subtype(text, mentions) == SocialSubtype.ENTITY_CORRECTION


def test_restore_phrasing_routes_social_direct_at_top_level(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    decision = intent.classify("Never mind, go back to the original for Docker.")
    assert decision.intent == intent.IntentClass.SOCIAL_DIRECT
    assert decision.high_confidence is True


def test_restore_builtin_entity_identity(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    entities.register(entities.EntityIdentity(canonical_name="Docker", archetype="octopus", source="user"))
    assert entities.resolve("Docker").archetype == "octopus"

    daemon._apply_entity_correction("Go back to the original for Docker.", entities.extract_mentions("Docker"))

    identity = entities.resolve("Docker")
    assert identity.archetype == "whale"
    assert identity.source == "builtin"


# ── Dismissal -> authoritative snooze (item 9) ──────────────────────────────

def test_parse_snooze_duration_explicit_hour():
    assert daemon._parse_snooze_duration_seconds("Leave me alone for an hour.") == 3600


def test_parse_snooze_duration_explicit_minutes():
    assert daemon._parse_snooze_duration_seconds("Leave me alone for 20 minutes.") == 1200


def test_parse_snooze_duration_default_when_unspecified():
    assert daemon._parse_snooze_duration_seconds("Drop it.") == daemon._DISMISSAL_DEFAULT_SNOOZE_SECONDS


def test_dismissal_sets_authoritative_snooze_not_model_invented(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)

    async def fake_render_direct_social(content, **kw):
        return personality.DirectSocialResult(True, "Fair enough.", "rendered")

    monkeypatch.setattr(personality, "render_direct_social", fake_render_direct_social)

    class _W:
        async def drain(self):
            pass

        def write(self, data):
            pass

    asyncio.run(daemon.handle_direct_social(_W(), "Drop it, I don't want to hear it right now."))

    snooze = ambient._active_snooze(ambient.kind_snooze_scope("procrastination_nudge"), now=__import__("time").time())
    assert snooze is not None
    assert snooze["origin"] == "manual"


# ── Repetition detection (item 10) ──────────────────────────────────────────

def test_is_substantially_repetitive_on_baseline_pair():
    a = (
        "A raven? You're either incredibly poetic or trying to distract me from your actual "
        "productivity levels. Since I'm stuck with a blank terminal anyway, I'll allow it. "
        "Just don't let the metaphor go to your head if you're going to spend another hour "
        "staring at nothing."
    )
    b = (
        "Fair enough. Since I'm stuck with a blank terminal anyway, I'm sticking with it. "
        "Just don't let the metaphor go to your head if you're going to spend another hour "
        "staring at nothing."
    )
    assert personality._is_substantially_repetitive(b, a) is True


def test_is_not_repetitive_for_distinct_replies():
    a = "Morning. Sleep well?"
    b = "The lion's restless again, apparently."
    assert personality._is_substantially_repetitive(b, a) is False


# ── Generation-time token limits (item 2) ───────────────────────────────────

def test_max_tokens_scales_with_subtype_budget():
    greeting_budget = personality._max_tokens_for(SocialSubtype.GREETING)
    vulnerable_budget = personality._max_tokens_for(SocialSubtype.VULNERABLE_DISCLOSURE)
    assert greeting_budget < vulnerable_budget


def test_render_direct_social_passes_max_tokens_to_generation(monkeypatch):
    captured = {}

    async def fake_render_personality_only(system_prompt, user_prompt, purpose, deadline_seconds, max_tokens=None, model_key=None):
        captured["max_tokens"] = max_tokens
        return "Morning."

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)
    asyncio.run(personality.render_direct_social("Hey.", subtype=SocialSubtype.GREETING))
    assert captured["max_tokens"] == personality._max_tokens_for(SocialSubtype.GREETING)


def test_render_direct_social_retries_on_repetition(monkeypatch):
    responses = iter([
        "Since I'm stuck with a blank terminal anyway, I'm sticking with it.",  # near-copy of prior
        "Something completely different this time.",
    ])

    async def fake_render_personality_only(system_prompt, user_prompt, purpose, deadline_seconds, max_tokens=None, model_key=None):
        return next(responses)

    monkeypatch.setattr(personality, "render_personality_only", fake_render_personality_only)

    history = [{"role": "user", "content": "hi"}, {
        "role": "assistant",
        "content": "Since I'm stuck with a blank terminal anyway, I'll allow it.",
    }]
    result = asyncio.run(personality.render_direct_social("Fair enough.", history=history))
    assert result.ok is True
    assert result.text == "Something completely different this time."
