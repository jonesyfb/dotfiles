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
  1. TOOL_OR_ACTION keywords are checked FIRST. A hit here disqualifies
     SOCIAL_DIRECT unconditionally, regardless of how casual the phrasing
     sounds ("Remember that my favorite color is green" is a tool request,
     not banter, even though it reads like a text a friend would send).
  2. FACTUAL_OR_REASONING triggers are checked next.
  3. SOCIAL_DIRECT requires a POSITIVE match against a narrow allowlist of
     conversational shapes (greeting, opinion-on-something, banter aimed
     at Huginn, a short first-person feeling statement) — it is "must
     prove social," never a default.
  4. Anything left is AMBIGUOUS, and AMBIGUOUS routes to the existing
     capable route, never to the personality-only model — per instruction,
     uncertain classification must never default toward the cheaper model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class IntentClass(Enum):
    SOCIAL_DIRECT = "social_direct"
    FACTUAL_OR_REASONING = "factual_or_reasoning"
    TOOL_OR_ACTION = "tool_or_action"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class IntentDecision:
    intent: IntentClass
    high_confidence: bool
    reason: str


# Word-boundary regexes. Order within a group doesn't matter; group order
# (tool -> factual -> social) does.
_TOOL_ACTION_RE = re.compile(
    r"\b("
    r"close|open|launch|start|stop|kill|restart|"
    r"add|schedule|queue|run|execute|install|update|upgrade|"
    r"remember|forget|recall|search my memory|"
    r"delete|remove|edit|write|save|send|"
    r"turn (on|off)|set (a|an|the)|"
    r"leave me alone|stop bothering|be quiet|shut up|snooze"
    r")\b",
    re.I,
)

_FACTUAL_RE = re.compile(
    r"\b(why (is|does|are|did)|explain|how (do|does|did|can)|compare|"
    r"diagnose|analyze|what is |what are |what causes|walk me through)\b",
    re.I,
)

# A recall-shaped question ("what was the X I mentioned") needs a real
# memory lookup, not a guess from a model with no tools — treat as
# TOOL_OR_ACTION even though it's phrased as a question.
_MEMORY_RECALL_RE = re.compile(
    r"\bwhat (was|is|were|are) (the|my|that)\b.*\b(i (mentioned|told|said)|"
    r"i (mentioned|told you|said))\b",
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

    if _MEMORY_RECALL_RE.search(text):
        return IntentDecision(IntentClass.TOOL_OR_ACTION, True, "memory_recall_question")

    if _TOOL_ACTION_RE.search(text):
        return IntentDecision(IntentClass.TOOL_OR_ACTION, True, "tool_action_keyword")

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
