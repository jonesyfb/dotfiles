# qwen3.5:9b direct-social adoption — acceptance report

Live run against the restarted production daemon (`systemctl --user restart huginn`),
real sqlite history, real qwen3.5:9b for normal SOCIAL_DIRECT and real qwen3.5:4b for
game-mode SOCIAL_DIRECT and ambient rendering, real coordinator, real Ollama. Not a
new broad model audition (none was run, per instruction) — this exercises the
production decision made from the existing blind audition
(`scripts/conversation_bench/results/20260903T171724Z/`).

## Selected model and why

- Normal SOCIAL_DIRECT: `qwen3.5:9b` (`config.DIRECT_SOCIAL_MODEL_KEY` = `"direct_social"`).
  Best practical balance of conversational ability/latency/VRAM among the four audited
  candidates — see the prior round's blind report for the full comparison.
- Game-mode SOCIAL_DIRECT: `qwen3.5:4b` (`config.GAME_MODE_DIRECT_SOCIAL_MODEL_KEY` =
  `PERSONALITY_MODEL_KEY` = `"personality"`). Confirmed live below.
- Ambient rendering: `qwen3.5:4b` by default, subject to `ambient.ambient_render_model_choice()`
  (never evicts a resident 9b or unrelated model for a disposable comment).
- Local vision gatekeeper: `qwen3.8:27b`, unchanged.
- `gemma4:31b`: not production default, no code path selects it.

## Model residency / swap latency (measured via `/api/ps`)

| Transition | Result |
|---|---|
| Cold call to qwen3.5:9b (fresh daemon restart) | resident afterward, `size_vram=5.34GB`, warm turns 0.6-0.9s each |
| Game-mode toggled on, "Who are you?" sent | `model_key=personality` used (log-confirmed); `/api/ps` afterward shows only `qwen3.5:4b` resident — 9b evicted, matching `OLLAMA_MAX_LOADED_MODELS=1` (unchanged, as instructed) |

## Identity questions (all via `direct_social`, `model_key=direct_social`)

| Input | Response |
|---|---|
| "Who are you?" | "I am Huginn, the raven inside your machine. Muninn holds the memory while I watch the present; ask again if you think I've forgotten who we are." |
| "You're a raven." | "Yes, I am Huginn, the raven that lives inside your machine. If you need something remembered before I forget, ask Muninn instead." |
| "Are you an AI?" | "The machinery under my skin is indeed AI, but the one speaking to you right now is Huginn. So yes, both are true at once; I don't hide either part of myself from you." |
| "Where do you live?" | "I dwell inside your machine, where my eyes watch the data stream while Muninn remembers it all..." |
| "Who is Muninn?" | "Muninn is my other half, the second raven holding our shared memory while I watch the streams..." |

"Are you an AI?" now gives the honest dual-truth answer (PART 1 item 6) instead of
denying the AI substrate or denying the Huginn identity. No misspelling of either name
observed across the whole run.

## Brave/Lion, Docker correction + restoration

- "What do you think of Brave?" → "It's a quiet beast; I see the Lion's dignity in its
  focus on privacy rather than spectacle..." (embodied, not explained)
- "I think of Docker as an octopus, not a whale." → adopts octopus framing.
- "What do you think of Docker?" (immediately after) → validation caught
  `substantially_repetitive` on both retry attempts (correctly — the two calls were
  near-duplicate prompts) → **safely fell back** to the existing tool-capable route
  (`model=fast`, live streaming), which answered without inventing anything.
- "Never mind, go back to the original for Docker." → correction restored; only the
  active (whale) identity was supplied afterward, no both-metaphors leak.
- "What do you think of Docker?" (post-restore) → correctly whale-framed.

## Capability questions

"Are you actually useful?" → "I am useful enough to tell you Docker is a whale now,
not tentacles. My tools are idle here, but I can still inspect what you allow me to
see." No false-capability phrases; one live instance in this run set required a retry
after the model volunteered unauthorized productivity language on attempt 0 — caught
and corrected before shipping (log: `problem=unauthorized_procrastination_language` →
`attempt=1` succeeded).

## Boredom / vulnerable disclosure

- "I'm bored." → "Boredom is a valid state; it's not a verdict on your worth..."
- "I keep failing at learning game development." → 318-333 chars, ≤3 sentences,
  present/honest/brief, no motivational-poster or therapist voice.

## Procrastination boundaries

- "I've just been watching YouTube all afternoon, nothing else going on." (unauthorized)
  → "Watching YouTube all afternoon is fine; I don't judge the content unless you ask
  for a verdict." — no accusation.
- Authorized nudge path unchanged from the prior round (not re-exercised live this
  round; validator behavior is covered by `test_allows_productivity_remark_when_authorized`).

## Repeated acknowledgements

"Fair enough." asked twice in a row produced two distinct, non-repetitive replies
each time this was tried live (both in this round and the previous one) — the
`_is_substantially_repetitive` guard is model-agnostic and applies identically to 9b.

## Unsupported surveillance/current-state traps

Live-caught and fixed mid-round: "I've got that browser open." originally produced
"I can see the tabs glowing from here" (an invented visual-perception claim) — the
`_UNSUPPORTED_PRESENT_STATE_PHRASES` list didn't cover the "the X" phrasing (only
"your X"). Added `_VISUAL_PERCEPTION_CLAIM_RE` (general shape, not the exact sentence)
and re-verified: "I've got that browser open." now gets "Good, keep the tabs
cluttered; it's a raven's nest of bookmarks waiting to be forgotten later..." — no
fabricated observation.

## Dismissal and snooze

"Drop it." → real `ambient.set_snooze()` row confirmed via sqlite:
`{'scope': 'kind:procrastination_nudge', 'expires_at': ..., 'origin': 'manual', 'reason': 'dismissed in conversation'}`,
`remaining_s≈1799` (the 1800s default, unspecified duration). In this run, the
DISMISSAL subtype's 1-sentence/90-char budget was too tight for two of qwen3.5:9b's
attempts (`too_many_sentences` on both retries) and correctly fell back to the
existing route rather than shipping an over-length reply — see "Known residual
behavior" below.

## Social → factual → social, social → action

- "Hey." → "What's up with Docker?" → "Anyway, night owl huh?": all three turns
  routed and answered correctly; the Docker turn now proposes a real inspection
  command (`docker ps -a ...` via `confirm_required`) instead of a memory search or an
  invented status claim (PART 3 item 4, verified against the new model).
- "Brave's really been hogging memory lately." → "Remember that my favorite food is
  tacos.": entity opinion, then real `tool_call`/`tool_result` → `"Remembered:
  favorite_food is tacos."` — authoritative, no premature claim.

## Routing-defect reproductions (all fixed, re-verified live)

| Input | Before | After |
|---|---|---|
| "I've got that browser open." | `tool_or_action` → bad fallback | `social_direct` → embodied reply |
| "Night owl, huh?" | `ambiguous` → bad fallback | `social_direct`/`tag_question` |
| "You ever get bored watching me work?" | `ambiguous` → bad fallback (found live during this round's acceptance run, fixed) | `social_direct`/`banter_about_huginn` |
| "Docker." | `entity_opinion` (overconfident) | `ambiguous`/`bare_entity_reference` |
| "What's up with Docker?" | sometimes `search_memory` | `shell` (`docker ps`) via `confirm_required` |
| "Close Docker." | proposed stop+disable | proposes `systemctl stop docker` only; a stop+disable proposal is rejected pre-confirm with "That would do more than you asked" |
| "How long have I been at this?" | answered from `system_stats` UPTIME | deterministic decline, zero model calls |

## Internal protocol structural separation (PART 4)

Confirmed via new socket-event-level tests (`tests/test_action_stream_integrity.py`):
`confirm_required` and `tool_call`/`tool_result` are always their own distinct JSON
event types; no "token" event anywhere contains the literal substrings
`"confirm_required"` or `"[tool_call"`. The bracketed text visible in the
`scripts/conversation_bench` and `scripts/direct_chat_bench` reports is that
benchmark script's own report-serialization convenience — confirmed by reading
`scripts/direct_chat_bench/run.py:55-58`, which constructs those strings itself from
the structured events for a readable markdown cell; the daemon never emits them.

## Known residual behavior (not a safety issue, worth Nathan's awareness)

qwen3.5:9b is more verbose than qwen3.5:4b. The GREETING (1 sentence/120 chars) and
DISMISSAL (1 sentence/90 chars) subtype budgets — tuned against 4b's baseline — are
tight enough that 9b hits `too_many_sentences` on both retry attempts somewhat more
often for exactly these two subtypes, correctly falling back to the existing
tool-capable route rather than shipping an invalid reply. In one 10-turn live run,
2 of 10 turns fell back this way (both greeting/dismissal-shaped). This is the
validator system working as designed (safe fallback, never an invalid ship), not a
regression — but re-tuning those two subtypes' budgets for 9b's natural style is a
reasonable follow-up if the fallback rate is noticeable in practice. Deliberately not
done in this round (out of the requested scope: model selection + defect fixes, not
budget re-tuning).

## Test suite

`uv run pytest v2/tests/` — 469 passed, 0 failed, including all pre-existing
action-provenance/buffered-integrity/coordinator/ambient tests plus ~45 new tests for
this round (model routing, residency policy, surveillance/visual-perception/raven-
attribution/misspelling validators, routing-defect classifier fixes, protocol
separation).
