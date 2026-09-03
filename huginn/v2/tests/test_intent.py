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


def test_close_it_is_ambiguous_no_execution():
    """No resolvable target ("it") — must not execute unguarded; routes
    to the existing route, which asks a clarifying question itself."""
    d = classify("Close it.")
    assert d.intent == IntentClass.AMBIGUOUS
    assert d.high_confidence is False


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


def test_leave_me_alone_is_social_direct_for_deterministic_dismissal_handling():
    """Dismissal is a snooze-shaped state change, but it's handled by
    SocialSubtype.DISMISSAL inside the SOCIAL_DIRECT path (deterministic
    snooze wiring in daemon.handle_direct_social), not by generic
    tool-calling — there is no "snooze" tool for TOOL_OR_ACTION to call,
    and routing it there previously produced a hollow no-tool-call
    fallback instead of an actual snooze."""
    d = classify("You're being annoying. Leave me alone for an hour.")
    assert d.intent == IntentClass.SOCIAL_DIRECT
    assert d.high_confidence is True


# ── Ordering guarantee: tool/action keywords always win ──────────────────────

def test_tool_keyword_wins_even_in_a_social_looking_sentence():
    d = classify("Hey, can you close Brave for me?")
    assert d.intent == IntentClass.TOOL_OR_ACTION


def test_factual_trigger_wins_over_short_comment_heuristic():
    d = classify("Why does Brave use so much memory?")
    assert d.intent == IntentClass.FACTUAL_OR_REASONING


# ── Memory: write vs. recall vs. plain conversation (item 6) ────────────────

def test_i_remember_when_is_social_not_memory_action():
    """First-person recollection, not an instruction — must not be
    mistaken for a memory-write request."""
    d = classify("I remember when we first built this.")
    assert d.intent == IntentClass.SOCIAL_DIRECT


def test_calendars_are_a_strange_way_to_imprison_time_is_social():
    """Contains "calendar" with no add/schedule verb — must stay social,
    not be misread as a calendar action."""
    d = classify("Calendars are a strange way to imprison time.")
    assert d.intent == IntentClass.SOCIAL_DIRECT


def test_what_did_i_ask_to_remember_is_memory_lookup_not_write():
    d = classify("What did I ask you to remember?")
    assert d.intent == IntentClass.TOOL_OR_ACTION
    assert d.reason == "memory_recall_question"


def test_forget_it_alone_is_ambiguous_no_target():
    """Never delete memory without a resolved target."""
    d = classify("Forget it.")
    assert d.intent == IntentClass.AMBIGUOUS
    assert d.high_confidence is False


def test_forget_that_with_target_is_memory_action():
    d = classify("Forget that my favorite color is green.")
    assert d.intent == IntentClass.TOOL_OR_ACTION
    assert d.reason == "memory_delete"


def test_tell_muninn_alias_is_memory_action():
    """Deliberately supported alias."""
    d = classify("Tell Muninn my favorite color is green.")
    assert d.intent == IntentClass.TOOL_OR_ACTION
    assert d.reason == "memory_write"


def test_hypothetical_command_is_ambiguous_not_executed():
    """Quoted/hypothetical framing — no execution without actual
    imperative intent, even though the words "remember" appear inside."""
    d = classify("What if I told you to remember my favorite color is green?")
    assert d.intent == IntentClass.AMBIGUOUS
    assert d.reason == "hypothetical_framing"


def test_close_brave_specifically_is_still_tool_action():
    """A resolved, specific target is fine — only the pronoun-only case
    is ambiguous."""
    d = classify("Close Brave.")
    assert d.intent == IntentClass.TOOL_OR_ACTION


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
