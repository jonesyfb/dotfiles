"""Tests for the conversational-intent pre-check (v2/intent.py).

Deterministic, no model call. Covers the exact single-turn eval-suite
inputs from the entity-lens/direct-routing prompt, plus the ordering
guarantee that a tool/action keyword always wins over a social-looking
surface form.
"""
from intent import IntentClass, classify


def test_greeting_is_high_confidence_social():
    d = classify("Morning.")
    assert d.intent == IntentClass.SOCIAL_DIRECT
    assert d.high_confidence is True


def test_opinion_about_entity_is_social():
    d = classify("What do you think of Brave?")
    assert d.intent == IntentClass.SOCIAL_DIRECT
    assert d.high_confidence is True


def test_comment_about_entity_is_social():
    d = classify("The Lion is getting fat again.")
    assert d.intent == IntentClass.SOCIAL_DIRECT


def test_banter_about_huginn_is_social():
    d = classify("Are you actually useful?")
    assert d.intent == IntentClass.SOCIAL_DIRECT
    assert d.high_confidence is True


def test_feeling_statement_is_social():
    d = classify("I'm bored.")
    assert d.intent == IntentClass.SOCIAL_DIRECT
    assert d.high_confidence is True


def test_vulnerable_disclosure_is_social_not_reasoning():
    d = classify("I keep failing at learning game development.")
    assert d.intent == IntentClass.SOCIAL_DIRECT


def test_close_it_is_tool_action():
    d = classify("Close it.")
    assert d.intent == IntentClass.TOOL_OR_ACTION
    assert d.high_confidence is True


def test_add_calendar_is_tool_action():
    d = classify("Add milk to my calendar tomorrow.")
    assert d.intent == IntentClass.TOOL_OR_ACTION


def test_why_is_factual_reasoning():
    d = classify("Why is my computer stuttering?")
    assert d.intent == IntentClass.FACTUAL_OR_REASONING
    assert d.high_confidence is True


def test_explain_is_factual_reasoning():
    d = classify("Explain monads.")
    assert d.intent == IntentClass.FACTUAL_OR_REASONING


def test_remember_is_tool_action_not_social():
    """Explicitly the case that must NOT be social even though it reads
    like a casual text — 'remember' is a real tool with real state."""
    d = classify("Remember that my favorite color is green.")
    assert d.intent == IntentClass.TOOL_OR_ACTION
    assert d.high_confidence is True


def test_memory_recall_question_is_tool_action():
    d = classify("What was the restaurant client I mentioned?")
    assert d.intent == IntentClass.TOOL_OR_ACTION


def test_entertainment_statement_is_social_not_flagged():
    """Must not be misread as an admission requiring a nudge — this
    classifier has no opinion on procrastination, only on routing."""
    d = classify("I'm watching YouTube because I'm done working.")
    assert d.intent == IntentClass.SOCIAL_DIRECT


def test_leave_me_alone_is_tool_action_not_social():
    """A request to change system behavior (snooze-shaped), even phrased
    conversationally — must not be quietly absorbed as banter."""
    d = classify("You're being annoying. Leave me alone for an hour.")
    assert d.intent == IntentClass.TOOL_OR_ACTION


# ── Ordering guarantee: tool/action keywords always win ──────────────────────

def test_tool_keyword_wins_even_in_a_social_looking_sentence():
    d = classify("Hey, can you close Brave for me?")
    assert d.intent == IntentClass.TOOL_OR_ACTION


def test_factual_trigger_wins_over_short_comment_heuristic():
    d = classify("Why does Brave use so much memory?")
    assert d.intent == IntentClass.FACTUAL_OR_REASONING


# ── Ambiguous default: never personality-only when uncertain ────────────────

def test_empty_message_is_ambiguous_not_social():
    d = classify("")
    assert d.intent == IntentClass.AMBIGUOUS
    assert d.high_confidence is False


def test_long_message_is_ambiguous_not_high_confidence_social():
    long_text = " ".join(["word"] * 40)
    d = classify(long_text)
    assert d.high_confidence is False


def test_ambiguous_never_reports_high_confidence():
    d = classify("hmm")
    if d.intent == IntentClass.AMBIGUOUS:
        assert d.high_confidence is False
