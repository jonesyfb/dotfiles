"""
Conversational-intent pre-check, ahead of llm.route_model().

Deterministic, zero-cost, no model call — matches this codebase's existing
pattern of a policy layer in front of anything that touches a model
(ambient.decide() ahead of personality.render(), for the same reason: a
model should never be the thing deciding whether it gets to run).

This does NOT replace route_model() or the tool-calling loop. It answers
one narrower question first: is this turn unambiguously low-stakes social
conversation that can skip the reasoning/tool path entirely? Everything
else — including every case this classifier isn't sure about — falls
through to the existing, unmodified route.

Classification order matters and is deliberately conservative:
  1. Hypothetical/quoted framing ("what if I told you to...") is checked
     first and forces AMBIGUOUS regardless of what follows — no execution
     without actual imperative intent.
  2. Memory-specific patterns are checked before the generic tool-action
     keyword list, because "remember" is genuinely ambiguous on its own:
     an imperative ("Remember that...") is a write, a first-person
     statement ("I remember when...") is plain conversation, "Forget it."
     with no resolvable target must never blindly delete something, and a
     "what did I ask you to remember" question is a read, not a write.
  3. Generic TOOL_OR_ACTION keywords next. A hit here disqualifies
     SOCIAL_DIRECT unconditionally, regardless of how casual the phrasing
     sounds. "close"/"open" with only a pronoun object ("Close it.") has
     no resolvable target — that's AMBIGUOUS (goes to the existing route,
     which asks for clarification itself), not an unguarded execution.
  4. FACTUAL_OR_REASONING triggers.
  5. SOCIAL_DIRECT requires a POSITIVE match against a narrow allowlist of
     conversational shapes (greeting, opinion-on-something, banter aimed
     at Huginn, a short first-person feeling statement, or a short plain
     comment) — it is "must prove social," never a default.
  6. Anything left is AMBIGUOUS, and AMBIGUOUS routes to the existing
     capable route, never to the personality-only model — per instruction,
     uncertain classification must never default toward the cheaper model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

import entities


class IntentClass(Enum):
    SOCIAL_DIRECT = "social_direct"
    FACTUAL_OR_REASONING = "factual_or_reasoning"
    TOOL_OR_ACTION = "tool_or_action"
    AMBIGUOUS = "ambiguous"


class SocialSubtype(Enum):
    """Narrow presentation modes within SOCIAL_DIRECT — each gets its own
    tone/length budget in personality.render_direct_social(). Classifying
    this is a separate, deliberately narrower question from top-level
    intent: everything here is already known to be SOCIAL_DIRECT."""
    GREETING = "greeting"
    CASUAL_BANTER = "casual_banter"
    ENTITY_OPINION = "entity_opinion"
    LEISURE_STATEMENT = "leisure_statement"
    VULNERABLE_DISCLOSURE = "vulnerable_disclosure"
    DISMISSAL = "dismissal"
    IDENTITY_QUESTION = "identity_question"
    CAPABILITY_QUESTION = "capability_question"
    ENTITY_CORRECTION = "entity_correction"


# (max_sentences, max_chars) per subtype — used both as generation hints
# and post-generation validation in personality.py. Deliberately NOT one
# global 600-char budget: a greeting essay is wrong even though a
# vulnerable disclosure legitimately needs more room.
SOCIAL_SUBTYPE_LIMITS: dict[SocialSubtype, tuple[int, int]] = {
    SocialSubtype.GREETING: (1, 120),
    SocialSubtype.CASUAL_BANTER: (2, 250),
    SocialSubtype.ENTITY_OPINION: (2, 250),
    SocialSubtype.LEISURE_STATEMENT: (2, 220),
    SocialSubtype.VULNERABLE_DISCLOSURE: (3, 450),
    SocialSubtype.DISMISSAL: (1, 90),
    SocialSubtype.IDENTITY_QUESTION: (2, 250),
    SocialSubtype.CAPABILITY_QUESTION: (2, 250),
    SocialSubtype.ENTITY_CORRECTION: (2, 200),
}


@dataclass(frozen=True)
class IntentDecision:
    intent: IntentClass
    high_confidence: bool
    reason: str


# ── Hypothetical/quoted framing: never treated as an actual request ─────────
_HYPOTHETICAL_RE = re.compile(
    r"\b(what if|imagine if|suppose|hypothetically|what would happen if)\b", re.I,
)

# ── Memory: write vs. recall vs. plain conversation ──────────────────────────
# Imperative only — "remember" at the start, or "remember that/to", or the
# deliberately-supported "tell muninn" alias. "I remember when..." (a
# first-person statement, not an instruction) never matches this.
_MEMORY_WRITE_RE = re.compile(
    r"^\s*remember\b|\bremember (that|to)\b|\btell muninn\b", re.I,
)
# A recall-shaped question ("what was the X I mentioned", "what did I ask
# you to remember") needs a real memory lookup, not a guess from a model
# with no tools — treat as TOOL_OR_ACTION even though it's phrased as a
# question, and distinctly from a write.
_MEMORY_RECALL_RE = re.compile(
    r"\bwhat (was|is|were|are) (the|my|that)\b.*\b(i (mentioned|told|said)|"
    r"i (mentioned|told you|said))\b|"
    r"\bwhat did i ask\b.*\bremember\b",
    re.I,
)
_FORGET_NO_TARGET_RE = re.compile(r"^\s*forget it\.?\s*$", re.I)
_FORGET_WITH_TARGET_RE = re.compile(r"\bforget (that|about|what i said about)\b", re.I)

# ── Entity correction: a high-confidence social act, not generic ambiguity ──
# "I think of Docker as an octopus, not a whale." / "I don't think of
# Docker as a whale, more like an octopus." Requires an actual resolvable
# entity mention — correction phrasing about nothing in particular is not
# this.
_ENTITY_CORRECTION_RE = re.compile(
    r"\bi (don'?t )?think of\b.*\bas\b|\bmore like (a|an)\b|\bi (don'?t )?see\b.*\bas\b",
    re.I,
)

# "Undo the correction" is itself a correction (back to the builtin
# interpretation) — shares the ENTITY_CORRECTION subtype and routing so
# daemon._apply_entity_correction() (which special-cases this pattern to
# call entities.forget_override() instead of entities.register()) always
# gets a chance to run.
_RESTORE_ENTITY_RE = re.compile(
    r"\bforget that correction\b|\bgo back to (the )?(original|default)\b|"
    r"\brestore (the )?(original|default)\b|\bnever ?mind( that)?,? (go back|revert)\b",
    re.I,
)

# ── Status/inspection question about a named entity: "what's up with
# Docker?" — a request for current state, not an opinion ("what do you
# think of Docker?") and not banter. Only promoted to FACTUAL_OR_REASONING
# when the subject actually resolves through the entity lens — otherwise
# there's nothing to distinguish it from ordinary short social comment.
_STATUS_QUESTION_RE = re.compile(r"\bwhat'?s up with\b|\bhow'?s\b.{0,30}\bdoing\??\s*$", re.I)

# ── close/open with no resolvable target ────────────────────────────────────
_PRONOUN_ONLY_TARGET_RE = re.compile(r"\b(close|open|kill|restart)\s+(it|this|that)\b\.?\s*$", re.I)

# Word-boundary regexes for the remaining generic tool/action keywords.
# Dismissal phrasing ("leave me alone", "stop bothering me", "be quiet",
# "shut up", "snooze") deliberately does NOT live here any more — it's
# owned by _DISMISSAL_RE/SocialSubtype.DISMISSAL below, which routes
# through SOCIAL_DIRECT into the deterministic snooze wiring in
# daemon.py's handle_direct_social, not the generic tool-calling path
# (there is no "snooze" tool for that path to call).
_TOOL_ACTION_RE = re.compile(
    r"\b("
    r"close|open|launch|start|stop|kill|restart|"
    r"add|schedule|queue|run|execute|install|update|upgrade|"
    r"search my memory|"
    r"delete|remove|edit|write|save|send|"
    r"turn (on|off)|set (a|an|the)"
    r")\b",
    re.I,
)

_FACTUAL_RE = re.compile(
    r"\b(why (is|does|are|did)|explain|how (do|does|did|can)|compare|"
    r"diagnose|analyze|what is |what are |what causes|walk me through)\b",
    re.I,
)

_GREETING_RE = re.compile(r"^\s*(hi|hey|hello|yo|sup|morning|good morning|evening|good evening)\b[.!]?\s*$", re.I)
_OPINION_RE = re.compile(r"\b(what do you think|thoughts on|do you like|how do you feel about)\b", re.I)
_BANTER_ABOUT_HUGINN_RE = re.compile(r"\b(are you|you'?re being|you seem|do you ever)\b.*\b(useful|real|annoying|okay|alright|sentient|bored)\b", re.I)
_FEELING_STATEMENT_RE = re.compile(r"^\s*i('m| am|'ve| have)\b", re.I)

_MAX_SOCIAL_WORDS = 25


def classify(content: str) -> IntentDecision:
    text = content.strip()
    if not text:
        return IntentDecision(IntentClass.AMBIGUOUS, False, "empty")

    if _HYPOTHETICAL_RE.search(text):
        return IntentDecision(IntentClass.AMBIGUOUS, False, "hypothetical_framing")

    if _MEMORY_RECALL_RE.search(text):
        return IntentDecision(IntentClass.TOOL_OR_ACTION, True, "memory_recall_question")

    if _FORGET_NO_TARGET_RE.search(text):
        return IntentDecision(IntentClass.AMBIGUOUS, False, "forget_unresolved_target")

    if _FORGET_WITH_TARGET_RE.search(text):
        return IntentDecision(IntentClass.TOOL_OR_ACTION, True, "memory_delete")

    if _MEMORY_WRITE_RE.search(text):
        return IntentDecision(IntentClass.TOOL_OR_ACTION, True, "memory_write")

    # Entity correction is checked before the generic tool-action/pronoun
    # checks — it's phrased as a statement, never an imperative, so it
    # doesn't collide with anything above, but must win over falling
    # through to AMBIGUOUS via no_confident_match.
    if _ENTITY_CORRECTION_RE.search(text) and entities.extract_mentions(text):
        return IntentDecision(IntentClass.SOCIAL_DIRECT, True, "entity_correction")

    if _RESTORE_ENTITY_RE.search(text) and entities.extract_mentions(text):
        return IntentDecision(IntentClass.SOCIAL_DIRECT, True, "entity_correction_restore")

    # Identity/capability questions win outright, ahead of the generic
    # tool-action and factual-trigger checks below — "What are you capable
    # of?" would otherwise hit _FACTUAL_RE's "what are " alternative and
    # "Where do you live?" has no allowlist match at all further down,
    # both of which previously fell through to AMBIGUOUS/tool-calling and
    # produced a wrong, non-identity answer.
    if _IDENTITY_QUESTION_RE.search(text):
        return IntentDecision(IntentClass.SOCIAL_DIRECT, True, "identity_question")
    if _CAPABILITY_QUESTION_RE.search(text):
        return IntentDecision(IntentClass.SOCIAL_DIRECT, True, "capability_question")

    if _PRONOUN_ONLY_TARGET_RE.search(text):
        return IntentDecision(IntentClass.AMBIGUOUS, False, "unresolved_target")

    if _TOOL_ACTION_RE.search(text):
        return IntentDecision(IntentClass.TOOL_OR_ACTION, True, "tool_action_keyword")

    # "What's up with Docker?" (status/inspection) vs. "What do you think
    # of Docker?" (opinion, stays social) — only promoted when the subject
    # actually resolves through the entity lens; otherwise there's nothing
    # to distinguish it from ordinary conversation, and it falls through
    # to the same rules as everything else below.
    if _STATUS_QUESTION_RE.search(text) and entities.extract_mentions(text):
        return IntentDecision(IntentClass.FACTUAL_OR_REASONING, True, "entity_status_question")

    if _FACTUAL_RE.search(text):
        return IntentDecision(IntentClass.FACTUAL_OR_REASONING, True, "factual_trigger")

    word_count = len(text.split())
    if word_count > _MAX_SOCIAL_WORDS:
        return IntentDecision(IntentClass.AMBIGUOUS, False, "too_long_for_high_confidence_social")

    if _GREETING_RE.search(text):
        return IntentDecision(IntentClass.SOCIAL_DIRECT, True, "greeting")
    if _OPINION_RE.search(text):
        return IntentDecision(IntentClass.SOCIAL_DIRECT, True, "opinion_request")
    if _BANTER_ABOUT_HUGINN_RE.search(text):
        return IntentDecision(IntentClass.SOCIAL_DIRECT, True, "banter_about_huginn")
    if _FEELING_STATEMENT_RE.search(text) and "?" not in text:
        return IntentDecision(IntentClass.SOCIAL_DIRECT, True, "feeling_statement")
    # A short, plain statement with no question mark and no trigger words
    # above ("The Lion is getting fat again.") reads as banter/comment, not
    # a request for anything. Safe even if this over-fires occasionally:
    # the personality model has no tools, so a missed action request costs
    # nothing worse than an amiable non-answer the user will simply re-ask.
    if word_count <= 12 and "?" not in text:
        return IntentDecision(IntentClass.SOCIAL_DIRECT, True, "short_comment_no_request")

    return IntentDecision(IntentClass.AMBIGUOUS, False, "no_confident_match")


# ── Social subtype: which presentation mode within SOCIAL_DIRECT ────────────

_DISMISSAL_RE = re.compile(
    r"\bdrop it\b|\bthat'?s enough\b|^\s*enough\s*[.!]?\s*$|\bnot now\b|\bleave me alone\b|"
    r"\bstop (?:it|bothering|teasing)\b|\bknock it off\b", re.I,
)
_IDENTITY_QUESTION_RE = re.compile(
    r"\bwho are you\b|\bwhat are you\b|\bare you (a |an )?(ai|robot|bot|"
    r"assistant|raven|real|sentient)\b|\byou'?re a raven\b|\bwhere do you live\b|"
    r"\bwho('?s| is) muninn\b",
    re.I,
)
_CAPABILITY_QUESTION_RE = re.compile(
    r"\bare you (actually )?useful\b|\bwhat can you (actually )?do\b|"
    r"\bcan you (actually )?do anything\b|\bwhat are you (capable of|good for)\b",
    re.I,
)
_VULNERABLE_RE = re.compile(
    r"\bi keep failing\b|\bi('m| am) (failing|struggling|bad at|not good at)\b|"
    r"\bi can'?t seem to\b|\bi feel (like a failure|incapable|stupid|worthless|useless)\b",
    re.I,
)
_LEISURE_RE = re.compile(
    r"\bwatching youtube\b|\bplaying (a |video )?games?\b|\bgaming\b|\bon youtube\b|"
    r"\bi'?m bored\b|\bnothing (going on|much)\b|\bjust (chilling|relaxing)\b",
    re.I,
)


def classify_social_subtype(content: str, mentions: "tuple[str, ...] | list[str]" = ()) -> SocialSubtype:
    """Only meaningful for content already classified SOCIAL_DIRECT.
    `mentions` should be entities.extract_mentions(content) — passed in
    rather than recomputed so callers that already have it don't pay
    twice."""
    text = content.strip()

    if _DISMISSAL_RE.search(text):
        return SocialSubtype.DISMISSAL
    if (_ENTITY_CORRECTION_RE.search(text) or _RESTORE_ENTITY_RE.search(text)) and mentions:
        return SocialSubtype.ENTITY_CORRECTION
    if _IDENTITY_QUESTION_RE.search(text):
        return SocialSubtype.IDENTITY_QUESTION
    if _CAPABILITY_QUESTION_RE.search(text):
        return SocialSubtype.CAPABILITY_QUESTION
    if _GREETING_RE.search(text):
        return SocialSubtype.GREETING
    if _VULNERABLE_RE.search(text):
        return SocialSubtype.VULNERABLE_DISCLOSURE
    if _LEISURE_RE.search(text):
        return SocialSubtype.LEISURE_STATEMENT
    if mentions:
        return SocialSubtype.ENTITY_OPINION
    return SocialSubtype.CASUAL_BANTER


def extract_corrected_archetype(content: str) -> "str | None":
    """Best-effort extraction of the NEW interpretation from an entity-
    correction statement. Narrow on purpose — handles the two phrasings
    this feature is specified against ("more like X" and "think of Y as
    X") and returns None rather than guessing for anything else, so a
    caller never persists a low-confidence extraction."""
    m = re.search(r"\bmore like (?:an? )?([a-z]+)", content, re.I)
    if m:
        return m.group(1).lower()
    m = re.search(r"\bthink of \w+ as (?:an? )?([a-z]+)", content, re.I)
    if m:
        return m.group(1).lower()
    return None
