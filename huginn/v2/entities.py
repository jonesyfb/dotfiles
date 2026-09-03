"""
Entity lens: a stable, configurable way of perceiving named machine
entities (apps, processes, concepts) consistently across both the
model-authored flavor clause and the code-owned deterministic sentence.

Resolution order, always: user-defined (persisted) > builtin (hardcoded,
this file) > inferred (ephemeral, recomputed every call, NEVER persisted).
A speculative identity invented for "Badger" this hour must not survive
to next hour just because it was invented once — only a user override
persists.

This module is pure data + lookup. It never calls a model, never touches
the coordinator, and has no opinion about whether an identity SHOULD be
used in a given sentence — personality.py's validation enforces
permitted/forbidden domains at generation time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import memory


@dataclass(frozen=True)
class EntityIdentity:
    canonical_name: str
    aliases: tuple[str, ...] = ()
    archetype: "str | None" = None          # e.g. "lion", "thunderbird", "whale" — None means no forced creature
    collective_form: "str | None" = None    # e.g. "a pride" — used only where plural framing genuinely fits
    permitted_domains: tuple[str, ...] = ()  # keys into _DOMAIN_VOCAB the flavor may draw from
    forbidden_domains: tuple[str, ...] = ()  # keys into _DOMAIN_VOCAB the flavor must never draw from
    confidence: float = 1.0
    source: str = "builtin"                 # "builtin" | "user" | "inferred"


# Shared vocabulary for domain enforcement AND (via personality._classify_style)
# diagnostic categorization — one source of truth for "what words belong to
# what register" so a forbidden-domain check and a diagnostic label always
# agree with each other.
DOMAIN_VOCAB = {
    "predator_consumption": ("lion", "pride", "cub", "hunt", "prey", "eat", "feast", "hoard", "den", "roar", "claw", "mouth", "devour"),
    "weather_omen": ("storm", "omen", "sky", "thunder", "cloud", "wind", "weather", "portent"),
    "machinery_noise": ("gear", "engine", "hum", "grind", "clank", "machine", "noise", "buzz", "whirr", "static"),
    "territory_navigation": ("realm", "territory", "map", "border", "path", "route", "navigate", "compass", "shore"),
    "messages_bureaucracy": ("inbox", "mail", "form", "queue", "paperwork", "ledger", "office", "memo", "clerk"),
    "sleep_memory_ritual": ("sleep", "dream", "ritual", "rest", "wake", "vigil", "remember", "forget", "muninn"),
    "mischief_rivalry": ("mischief", "trick", "rival", "sneak", "prank", "gossip", "quarrel", "bicker"),
    "aquatic_cargo": ("whale", "cargo", "vessel", "hull", "harbor", "tide", "dock", "container", "ship"),
}

# Deliberate canonical anchors — stable worldview, not mandatory nicknames.
# Plain application names remain valid when clarity is better; nothing here
# forces the model to use the archetype in every sentence.
_BUILTIN_IDENTITIES: dict[str, EntityIdentity] = {
    "brave": EntityIdentity(
        canonical_name="Brave", aliases=("brave-browser", "lion"), archetype="lion",
        collective_form="a pride", permitted_domains=("predator_consumption", "territory_navigation"),
        forbidden_domains=(), source="builtin",
    ),
    "thunderbird": EntityIdentity(
        canonical_name="Thunderbird", aliases=(), archetype="thunderbird",
        permitted_domains=("weather_omen", "messages_bureaucracy"),
        forbidden_domains=("predator_consumption",), source="builtin",
    ),
    "docker": EntityIdentity(
        canonical_name="Docker", aliases=(), archetype="whale",
        permitted_domains=("aquatic_cargo", "machinery_noise", "territory_navigation"),
        forbidden_domains=("predator_consumption",), source="builtin",
    ),
    "discord": EntityIdentity(
        canonical_name="Discord", aliases=(), archetype=None,
        permitted_domains=("mischief_rivalry", "weather_omen"),
        forbidden_domains=("predator_consumption",), source="builtin",
    ),
    "muninn": EntityIdentity(
        canonical_name="Muninn", aliases=("memory", "recall", "search_memory"), archetype="raven",
        permitted_domains=("sleep_memory_ritual",), forbidden_domains=(), source="builtin",
    ),
    "huginn": EntityIdentity(
        canonical_name="Huginn", aliases=(), archetype="raven",
        permitted_domains=("sleep_memory_ritual",), forbidden_domains=(), source="builtin",
    ),
}

# Small, deliberately non-exhaustive dictionary of literal animal/object
# nouns — used ONLY to derive a temporary, low-confidence, never-persisted
# interpretation for an unrecognized name that plainly IS that word
# ("Badger"). A name not in here (e.g. "Parity") gets no archetype and
# stays plain, which is correct, not a gap.
_LITERAL_NOUNS = {
    "badger", "fox", "wolf", "hawk", "owl", "otter", "beaver", "panda",
    "raccoon", "crow", "sparrow", "falcon", "weasel", "ferret", "mole",
    "heron", "stag", "boar", "eagle", "swan", "lynx",
}


def _norm(name: str) -> str:
    return name.strip().lower()


def resolve(name: str) -> "EntityIdentity | None":
    """User override > builtin > ephemeral literal-name inference > None
    (stay plain). Never raises; an unresolvable name is a normal outcome,
    not an error."""
    key = _norm(name)
    if not key:
        return None

    user = memory.get_entity_identity(key)
    if user is not None:
        return EntityIdentity(
            canonical_name=user["canonical_name"],
            aliases=tuple(user.get("aliases", ())),
            archetype=user.get("archetype"),
            collective_form=user.get("collective_form"),
            permitted_domains=tuple(user.get("permitted_domains", ())),
            forbidden_domains=tuple(user.get("forbidden_domains", ())),
            confidence=user.get("confidence", 1.0),
            source="user",
        )

    if key in _BUILTIN_IDENTITIES:
        return _BUILTIN_IDENTITIES[key]
    for identity in _BUILTIN_IDENTITIES.values():
        if key in (a.lower() for a in identity.aliases):
            return identity

    if key in _LITERAL_NOUNS:
        return EntityIdentity(
            canonical_name=name.strip(), archetype=key, confidence=0.6, source="inferred",
        )

    return None


def cues_for(identity: "EntityIdentity | None") -> dict:
    """The flavor_cues fragment a caller should merge in for a resolved
    identity — the ONLY channel through which archetype/collective_form
    reach the model. Never includes anything from request.facts."""
    if identity is None:
        return {}
    cues: dict = {"subject": identity.canonical_name}
    if identity.archetype:
        cues["archetype"] = identity.archetype
    if identity.collective_form:
        cues["collective_form"] = identity.collective_form
    return cues


def register(identity: EntityIdentity) -> None:
    """User-defined override — persisted, always resolved first. This is a
    plain function, not a tool; nothing in this slice wires it to chat or
    tool-calling (same boundary as ambient.set_snooze in the prior slice)."""
    key = _norm(identity.canonical_name)
    memory.set_entity_identity(key, {
        "canonical_name": identity.canonical_name,
        "aliases": list(identity.aliases),
        "archetype": identity.archetype,
        "collective_form": identity.collective_form,
        "permitted_domains": list(identity.permitted_domains),
        "forbidden_domains": list(identity.forbidden_domains),
        "confidence": identity.confidence,
    })


def forget_override(canonical_name: str) -> None:
    memory.delete_entity_identity(_norm(canonical_name))


def all_identities() -> dict:
    """Inspectable registry dump — builtins plus any persisted user
    overrides — for debugging/config review, not used at render time."""
    out = {k: v for k, v in _BUILTIN_IDENTITIES.items()}
    out.update({k: resolve(k) for k in memory.all_entity_identity_keys()})
    return out


_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")


def _strip_possessive(word: str) -> str:
    """"Brave's" / "Docker's" -> "Brave" / "Docker" — casual mentions are
    overwhelmingly possessive ("Brave's hogging memory"), and the naive
    word regex otherwise keeps the trailing 's, which never resolves to
    anything (observed live: scripts/direct_chat_bench, "Brave's really
    been hogging memory" produced no entity note at all)."""
    if word.lower().endswith("'s"):
        return word[:-2]
    if word.endswith("'"):
        return word[:-1]
    return word


def extract_mentions(text: str) -> list[str]:
    """Which entities does this text actually mention? Only returns names
    that resolve to SOME identity (builtin, user, or literal-noun
    inference) — this is deliberately conservative so flavor_cues never
    carries an entity note for something that doesn't resolve to anything
    (e.g. "Parity" correctly produces no mention)."""
    seen: list[str] = []
    seen_keys: set[str] = set()
    for raw_word in _WORD_RE.findall(text):
        word = _strip_possessive(raw_word)
        key = _norm(word)
        if key in seen_keys or len(word) < 3:
            continue
        if resolve(word) is not None:
            seen.append(word)
            seen_keys.add(key)
    return seen
