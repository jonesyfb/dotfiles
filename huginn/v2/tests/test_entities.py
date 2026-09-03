"""Tests for the entity lens (v2/entities.py).

Covers: stable builtin identities, forbidden-domain assignment, literal-name
inference for unrecognized-but-meaningful names, neutral names staying
plain, user-override priority, no persistence of speculative identities,
and mention extraction used to pass one consistent identity to both flavor
and deterministic presentation.
"""
import entities
from entities import EntityIdentity


def _use_temp_db(tmp_path, monkeypatch):
    import memory
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")


# ── Stable builtin identities ────────────────────────────────────────────────

def test_brave_resolves_to_lion_consistently(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    for _ in range(5):
        identity = entities.resolve("Brave")
        assert identity.canonical_name == "Brave"
        assert identity.archetype == "lion"
        assert identity.collective_form == "a pride"
        assert identity.source == "builtin"


def test_brave_resolves_case_insensitively(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    assert entities.resolve("brave").archetype == "lion"
    assert entities.resolve("BRAVE").archetype == "lion"


def test_docker_forbids_predator_consumption_domain(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    identity = entities.resolve("Docker")
    assert identity.archetype == "whale"
    assert "predator_consumption" in identity.forbidden_domains
    assert "lion" not in [w for domain in identity.permitted_domains for w in entities.DOMAIN_VOCAB[domain]]


def test_thunderbird_literal_interpretation(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    identity = entities.resolve("Thunderbird")
    assert identity.archetype == "thunderbird"


def test_discord_is_not_automatically_an_animal(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    identity = entities.resolve("Discord")
    assert identity.archetype is None
    assert "predator_consumption" in identity.forbidden_domains


def test_muninn_covers_memory_aliases(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    identity = entities.resolve("memory")
    assert identity.canonical_name == "Muninn"


# ── Unknown entities: literal inference vs. staying plain ──────────────────

def test_badger_natural_temporary_inference(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    identity = entities.resolve("Badger")
    assert identity is not None
    assert identity.archetype == "badger"
    assert identity.source == "inferred"
    assert identity.confidence < 1.0


def test_parity_remains_plain(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    identity = entities.resolve("Parity")
    assert identity is None


def test_unknown_name_fails_soft(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    assert entities.resolve("") is None
    assert entities.resolve("XyzzyNotAThing") is None


def test_inferred_identity_never_persisted(tmp_path, monkeypatch):
    """Resolving Badger twice must not cause it to become a permanent
    builtin/user identity merely because it was inferred once."""
    _use_temp_db(tmp_path, monkeypatch)
    import memory
    first = entities.resolve("Badger")
    assert first.source == "inferred"
    assert memory.get_entity_identity("badger") is None  # never written to disk
    second = entities.resolve("Badger")
    assert second.source == "inferred"  # still inferred fresh, not "user"


# ── User override priority ──────────────────────────────────────────────────

def test_user_override_takes_priority_over_builtin(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    entities.register(EntityIdentity(canonical_name="Docker", archetype="octopus", source="user"))
    identity = entities.resolve("Docker")
    assert identity.archetype == "octopus"
    assert identity.source == "user"


def test_user_override_takes_priority_over_inference(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    entities.register(EntityIdentity(canonical_name="Badger", archetype=None, source="user"))
    identity = entities.resolve("Badger")
    assert identity.source == "user"
    assert identity.archetype is None  # user explicitly said: no archetype


def test_forget_override_restores_builtin(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    entities.register(EntityIdentity(canonical_name="Docker", archetype="octopus", source="user"))
    entities.forget_override("Docker")
    identity = entities.resolve("Docker")
    assert identity.archetype == "whale"
    assert identity.source == "builtin"


# ── Registry inspectability ──────────────────────────────────────────────────

def test_registry_is_inspectable(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    entities.register(EntityIdentity(canonical_name="Parity", archetype="a metronome", source="user"))
    registry = entities.all_identities()
    assert "brave" in registry
    assert "parity" in registry
    assert registry["parity"].archetype == "a metronome"


# ── Consistent identity across flavor and presenter (item: no subject mismatch) ─

def test_cues_for_carries_canonical_name_and_archetype(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    identity = entities.resolve("Brave")
    cues = entities.cues_for(identity)
    assert cues["subject"] == "Brave"
    assert cues["archetype"] == "lion"
    assert cues["collective_form"] == "a pride"


def test_cues_for_none_identity_is_empty():
    assert entities.cues_for(None) == {}


def test_same_identity_object_feeds_both_flavor_cues_and_presenter_subject(tmp_path, monkeypatch):
    """The exact value that would go into the deterministic sentence
    (identity.canonical_name) is the same string handed to the model via
    cues_for — no separate, potentially-diverging naming path."""
    _use_temp_db(tmp_path, monkeypatch)
    identity = entities.resolve("Brave")
    cues = entities.cues_for(identity)
    assert cues["subject"] == identity.canonical_name


# ── Mention extraction: conservative, only resolvable names ─────────────────

def test_extract_mentions_finds_known_entities(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    mentions = entities.extract_mentions("Brave is slow and Docker is loud")
    assert "Brave" in mentions
    assert "Docker" in mentions


def test_extract_mentions_skips_unresolvable_words(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    mentions = entities.extract_mentions("Parity and Xyzzy are both mysteries")
    assert mentions == []


def test_extract_mentions_finds_literal_inference_candidates(tmp_path, monkeypatch):
    _use_temp_db(tmp_path, monkeypatch)
    mentions = entities.extract_mentions("Badger just started up")
    assert "Badger" in mentions


def test_extract_mentions_strips_possessive(tmp_path, monkeypatch):
    """Regression: "Brave's really been hogging memory" produced no entity
    note at all before this fix — the naive word regex kept the trailing
    's, which never resolves (scripts/direct_chat_bench live eval)."""
    _use_temp_db(tmp_path, monkeypatch)
    mentions = entities.extract_mentions("Brave's really been hogging memory lately.")
    assert "Brave" in mentions
    mentions2 = entities.extract_mentions("Docker's being a whale again.")
    assert "Docker" in mentions2


# ── No model access to protected factual values (structural, not this module's
# job to enforce directly — verified here at the boundary this module owns) ──

def test_entity_identity_never_carries_raw_facts(tmp_path, monkeypatch):
    """EntityIdentity has no field for arbitrary facts — structurally, an
    identity can only ever be name/archetype/domains, never a fact dict."""
    fields = EntityIdentity.__dataclass_fields__
    assert "facts" not in fields
    assert "value" not in fields
