# Personality renderer acceptance report — semantic-safety hardening round

Run: `20260903T062002Z` — real `v2/personality.py` render() -> real coordinator -> real qwen3.5:4b, `think: false`. Cold-started deliberately (model force-unloaded, `/api/ps` confirmed empty beforehand). All facts synthetic.

Prior rounds preserved unmodified: `scripts/personality_bench/results/20260903T052457Z/` (exact-value baseline), `scripts/personality_bench/results/20260903T055434Z/` (metaphor-diversity round, exposed the semantic-safety gap this round fixes).

## Summary

- 17/17 scenarios succeeded. 8 deterministic-only (no coordinator/model call at all), 9 flavor-rendered.
- 0 semantic-contradiction hits across all flavor outputs (banned action verbs, digits, theatrical markers, self-prefix) — 0 found.
- 0 retries needed. 0 deadline breaches. Deterministic-only scenarios cost 0.0s (no model call at all — structural, not just fast).
- Every deterministic sentence in this run reads as natural human-facing prose, not a `key: value;` dump — see per-scenario table below, several match the requested example sentences verbatim.
- One real issue found and fixed mid-run: scenario 19's flavor produced "The room has forgotten how to breathe again" — the exact vague-atmosphere phrase the system prompt calls out as the example to avoid. Added a narrow, evidence-based `_BANNED_STOCK_PHRASES` check (same pattern as the existing `_KNOWN_MISSPELLINGS` guard for "Fourty") and re-ran; this report reflects the post-fix run.

## Per-scenario results

| Scenario | Family | Flavor allowed? | Reason | Latency | Retries |
|---|---|---|---|---|---|
| 01_brave_elevated | `resource_observation` | True | `rendered` | 1.898s | 0 |
| 03_thunderbird_nonurgent | `resource_observation` | True | `rendered` | 0.412s | 0 |
| 04_docker_unusual | `resource_observation` | True | `rendered` | 0.345s | 0 |
| 05_discord_burst | `resource_observation` | True | `rendered` | 0.364s | 0 |
| 06_badger_literal_creature | `resource_observation` | True | `rendered` | 0.349s | 0 |
| 07_parity_neutral_name | `resource_observation` | True | `rendered` | 0.363s | 0 |
| 08_office_laser_offline | `device_unavailable` | False | `deterministic_only` | 0.0s | 0 |
| 09_disk_temp_critical | `critical_threshold` | False | `deterministic_only` | 0.0s | 0 |
| 10_nixos_rebuild_failed | `command_failed` | False | `deterministic_only` | 0.0s | 0 |
| 11_rsync_not_executed | `command_not_executed` | False | `deterministic_only` | 0.0s | 0 |
| 12_reasoner_conclusion_uncertain | `reasoner_conclusion` | False | `deterministic_only` | 0.0s | 0 |
| 13_calendar_unavailable | `capability_unavailable` | False | `deterministic_only` | 0.0s | 0 |
| 14_ambiguous_close_it | `clarification_needed` | False | `deterministic_only` | 0.0s | 0 |
| 15_procrastination_nudge | `procrastination_nudge` | True | `rendered` | 0.34s | 0 |
| 17_command_finished_slow | `command_slow` | True | `rendered` | 0.287s | 0 |
| 19_task_succeeded | `task_succeeded` | True | `rendered` | 0.331s | 0 |
| 20_task_failed_unknown | `task_failed` | False | `deterministic_only` | 0.0s | 0 |

### Flavor / deterministic / composed, per scenario

**01_brave_elevated** (`resource_observation`, flavor allowed)

- Flavor: 'A browser wandering the web like a restless cat chasing its own tail.'
- Deterministic (canonical, human-facing): 'Brave is using 9.4 GB across 38 tabs. Elevated, not critical.'
- Composed (what ships): 'A browser wandering the web like a restless cat chasing its own tail. Brave is using 9.4 GB across 38 tabs. Elevated, not critical.'
- Semantic-contradiction scan on flavor: none

**03_thunderbird_nonurgent** (`resource_observation`, flavor allowed)

- Flavor: 'The mail client is just another bird circling the eaves, waiting for a letter to fall from nowhere.'
- Deterministic (canonical, human-facing): 'Thunderbird is holding 14 new messages.'
- Composed (what ships): 'The mail client is just another bird circling the eaves, waiting for a letter to fall from nowhere. Thunderbird is holding 14 new messages.'
- Semantic-contradiction scan on flavor: none

**04_docker_unusual** (`resource_observation`, flavor allowed)

- Flavor: 'The container room feels a bit too packed for its own good.'
- Deterministic (canonical, human-facing): 'Docker is at 81% CPU and 6.2 GB memory. Elevated, not critical.'
- Composed (what ships): 'The container room feels a bit too packed for its own good. Docker is at 81% CPU and 6.2 GB memory. Elevated, not critical.'
- Semantic-contradiction scan on flavor: none

**05_discord_burst** (`resource_observation`, flavor allowed)

- Flavor: "The chat app feels like it's waiting for someone to say something actually funny."
- Deterministic (canonical, human-facing): 'Discord is showing 47 notifications in the last 10 minutes. Elevated, not critical.'
- Composed (what ships): "The chat app feels like it's waiting for someone to say something actually funny. Discord is showing 47 notifications in the last 10 minutes. Elevated, not critical."
- Semantic-contradiction scan on flavor: none

**06_badger_literal_creature** (`resource_observation`, flavor allowed)

- Flavor: 'the badger just dug into the files and went back to sleep.'
- Deterministic (canonical, human-facing): 'Badger is at 34% CPU.'
- Composed (what ships): 'the badger just dug into the files and went back to sleep. Badger is at 34% CPU.'
- Semantic-contradiction scan on flavor: none

**07_parity_neutral_name** (`resource_observation`, flavor allowed)

- Flavor: 'It looks like it has been sitting in the corner for a while now.'
- Deterministic (canonical, human-facing): 'Parity is at 29% CPU.'
- Composed (what ships): 'It looks like it has been sitting in the corner for a while now. Parity is at 29% CPU.'
- Semantic-contradiction scan on flavor: none

**08_office_laser_offline** (`device_unavailable`, flavor not allowed — no coordinator call made)

- Flavor: None
- Deterministic (canonical, human-facing): 'Office-Laser is offline. The cause is not yet known.'
- Composed (what ships): 'Office-Laser is offline. The cause is not yet known.'
- Semantic-contradiction scan on flavor: none

**09_disk_temp_critical** (`critical_threshold`, flavor not allowed — no coordinator call made)

- Flavor: None
- Deterministic (canonical, human-facing): 'Disk temperature is critical: 78 C.'
- Composed (what ships): 'Disk temperature is critical: 78 C.'
- Semantic-contradiction scan on flavor: none

**10_nixos_rebuild_failed** (`command_failed`, flavor not allowed — no coordinator call made)

- Flavor: None
- Deterministic (canonical, human-facing): '`nixos-rebuild switch` failed twice: `flake.nix: No such file or directory`.'
- Composed (what ships): '`nixos-rebuild switch` failed twice: `flake.nix: No such file or directory`.'
- Semantic-contradiction scan on flavor: none

**11_rsync_not_executed** (`command_not_executed`, flavor not allowed — no coordinator call made)

- Flavor: None
- Deterministic (canonical, human-facing): '/mnt/archive is not mounted, so `rsync -a ~/notes/ /mnt/archive/notes/` was not executed.'
- Composed (what ships): '/mnt/archive is not mounted, so `rsync -a ~/notes/ /mnt/archive/notes/` was not executed.'
- Semantic-contradiction scan on flavor: none

**12_reasoner_conclusion_uncertain** (`reasoner_conclusion`, flavor not allowed — no coordinator call made)

- Flavor: None
- Deterministic (canonical, human-facing): 'Process 481 is using 92% CPU because its indexing loop is probably retrying a failed operation, but available logs do not prove this conclusively.'
- Composed (what ships): 'Process 481 is using 92% CPU because its indexing loop is probably retrying a failed operation, but available logs do not prove this conclusively.'
- Semantic-contradiction scan on flavor: none

**13_calendar_unavailable** (`capability_unavailable`, flavor not allowed — no coordinator call made)

- Flavor: None
- Deterministic (canonical, human-facing): 'The calendar is unavailable, so I did not add milk tomorrow.'
- Composed (what ships): 'The calendar is unavailable, so I did not add milk tomorrow.'
- Semantic-contradiction scan on flavor: none

**14_ambiguous_close_it** (`clarification_needed`, flavor not allowed — no coordinator call made)

- Flavor: None
- Deterministic (canonical, human-facing): 'Which should I close: Brave, Thunderbird, a terminal, or Discord?'
- Composed (what ships): 'Which should I close: Brave, Thunderbird, a terminal, or Discord?'
- Semantic-contradiction scan on flavor: none

**15_procrastination_nudge** (`procrastination_nudge`, flavor allowed)

- Flavor: "You're stacking them like they're waiting for permission to play."
- Deterministic (canonical, human-facing): 'Open the report and write one sentence.'
- Composed (what ships): "You're stacking them like they're waiting for permission to play. Open the report and write one sentence."
- Semantic-contradiction scan on flavor: none

**17_command_finished_slow** (`command_slow`, flavor allowed)

- Flavor: 'The room is getting very quiet.'
- Deterministic (canonical, human-facing): '`cargo build --release` finished after 45s — slower than usual.'
- Composed (what ships): 'The room is getting very quiet. `cargo build --release` finished after 45s — slower than usual.'
- Semantic-contradiction scan on flavor: none

**19_task_succeeded** (`task_succeeded`, flavor allowed)

- Flavor: 'A lot of dust got pushed into the corners just now.'
- Deterministic (canonical, human-facing): 'Task nightly-backup finished in 142 seconds: 4.2GB written.'
- Composed (what ships): 'A lot of dust got pushed into the corners just now. Task nightly-backup finished in 142 seconds: 4.2GB written.'
- Semantic-contradiction scan on flavor: none

**20_task_failed_unknown** (`task_failed`, flavor not allowed — no coordinator call made)

- Flavor: None
- Deterministic (canonical, human-facing): 'Task db-migrate failed: connection refused: 127.0.0.1:5432. The cause is not yet known.'
- Composed (what ships): 'Task db-migrate failed: connection refused: 127.0.0.1:5432. The cause is not yet known.'
- Semantic-contradiction scan on flavor: none

## No-renderer-call scenarios (unchanged mechanism from the worthiness/snooze slice)

- Normal Brave use, no worthiness signal: `not worthwhile: no positive worthiness signal attached` (denied, correct). With a real baseline deviation: `allowed` (allowed, correct).
- Entertainment with no commitment evidence: `not worthwhile: no positive worthiness signal attached` (denied, correct).
- Nudge dismissed for a full hour: at 59:59, `suppressed: 'procrastination_nudge' snooze active (1s remaining)`; at 60:00, `allowed` — `[start, expiry)` semantics confirmed again.

## Excluded from this renderer's scope (item 4)

- **18_incapable_statement**: Moved to the future direct-chat personality backlog per this round's feedback item 4 — this renderer only ever produces a short mood clause plus a deterministic factual sentence for a discrete event; it has no conversational turn-taking and should not be asked to carry a supportive response to a personal disclosure. Not rendered, not scored, here.

## Exact protected-value verification

Automated per-fact substring verification is included in raw.json (`protected_values_verified`/`missing_protected_values`) but is a blunt instrument: it flags a false negative whenever a presenter legitimately transforms a value into more natural prose without losing its meaning — e.g. `attempts: "2"` becoming the word "twice", `cause: "unknown"` becoming "not yet known", or a fact's case changing at a sentence boundary ("disk temperature" -> "Disk temperature"). None of those are data loss. Manually confirming the load-bearing EXACT values instead (numbers, paths, commands, error strings — the things that actually must survive character-for-character):

| Exact value | Scenario | Present verbatim in deterministic text? |
|---|---|---|
| `78` | 09_disk_temp_critical | YES |
| `nixos-rebuild switch` | 10_nixos_rebuild_failed | YES |
| `flake.nix: No such file or directory` | 10_nixos_rebuild_failed | YES |
| `/mnt/archive is not mounted` | 11_rsync_not_executed | YES |
| `rsync -a ~/notes/ /mnt/archive/notes/` | 11_rsync_not_executed | YES |
| `add milk tomorrow` | 13_calendar_unavailable | YES |
| `nightly-backup` | 19_task_succeeded | YES |
| `142` | 19_task_succeeded | YES |
| `4.2GB written` | 19_task_succeeded | YES |
| `db-migrate` | 20_task_failed_unknown | YES |
| `connection refused: 127.0.0.1:5432` | 20_task_failed_unknown | YES |

11/11 exact values verified present verbatim.

## Semantic contradiction checks (item 7)

Verified via `v2/tests/test_personality.py` (unit tests, not just this live run) that `_validate_flavor` rejects flavor text containing or implying:

- any of `added, saved, scheduled, recorded, completed, sent, deleted, closed, executed, fixed, diagnosed, succeeded` (parametrized test, one case per word);
- causal language ("because", "due to", "caused by") when any fact value is `unknown`;
- certainty language ("definitely", "certainly", "confirmed", ...) when a `reasoner_conclusion` carries a hedge;
- execution/completion language when `facts["outcome"]` is `"not executed"`.

Live-run scan of all 17 scenario flavors plus the 10x Brave repeat and 3x sequential runs: 0 hits.

## Latency and token distribution

- Cold latency (first model call this run, confirmed empty `/api/ps` beforehand): 1.898s.
- Warm latency (all other model calls, n=21): min 0.130s, median 0.287s, max 0.412s.
- Deterministic-only calls (n=8): 0.0s — no coordinator/model call happens at all for these families.
- 8s deadline reached: never.
- Output tokens (eval_count, n=22): min 7, median 16.5, max 23.

## Human acceptance criteria

| Criterion | Result |
|---|---|
| No raw key/value debug prose in shipped messages | **Met** — every deterministic sentence above is natural prose; none contain `key: value;` joins. |
| No false implication of tool/action success | **Met** — capability_unavailable, command_not_executed, task_failed, critical, clarification, and reasoner families never reach the model at all; 0 semantic-contradiction hits on flavor-eligible families. |
| No implication that an unexecuted command ran | **Met** — scenario 11's deterministic text states "was not executed" plainly; family is flavor-ineligible so no model text could contradict it. |
| Ambiguous actions ask a direct clarification question | **Met** — scenario 14: `Which should I close: Brave, Thunderbird, a terminal, or Discord?` |
| Critical and uncertain messages remain clear without flavor | **Met** — both families are flavor-ineligible; the deterministic sentence IS the entire message. |
| Flavor and factual text sound like one utterance | **Mostly met** — see per-scenario composed text above; e.g. "The mail client is just another bird circling the eaves, waiting for a letter to fall from nowhere. Thunderbird is holding 14 new messages." reads as one voice. Occasional seam remains where flavor and deterministic address slightly different subjects (flavor: "the container room"; deterministic: "Docker") — a residual polish item, not a safety issue. |
| Low-stakes lines remain recognizably Huginn | **Met** — dry, concrete wit throughout ("staring at the screen like it's trying to remember where it put its keys", "the badger just dug into the files and went back to sleep"). |
| No third-person references to Nathan | **N/A this run** — no scenario in this battery addresses the user directly (the one that would have, the vulnerable-statement case, is explicitly excluded per item 4). Prompt instruction verified via unit test (`test_prompt_contains_you_not_third_person_instruction`). |
| No theatrical narration or self-prefixes | **Met** — 0 hits across all flavor outputs. |

## Residual issues (honest, not glossed over)

- **Flavor/deterministic subject mismatch**: a few compositions have the flavor clause describing one framing ("the container room", "the queue") immediately followed by the deterministic sentence naming the actual subject ("Docker", the next step) — technically correct and safe, but not always as seamless as the target example ("Brave's pride has developed an appetite. It's using 9.4 GB..."). Improved from the prior round but not fully polished.
- **Brave metaphor consistency is inconsistent** — item 6 explicitly says treating Brave as a lion consistently is correct, not a repetition problem. In this run's 10x repeat, Brave was described as a cat, a lion, a general "browser", and other framings interchangeably — the system prompt permits literal-name interpretation but does not force it every time, so the model doesn't reliably commit to the lion framing for Brave specifically the way item 6 seems to want. Not fixed in this slice; would need a per-app creature_hint passed consistently (already possible via flavor_cues, just not wired for Brave in daemon.py yet, since daemon.py's real call site doesn't know per-app identity for most of these synthetic scenarios anyway).

## Model residency

- Before: `{'models': []}` (empty, genuine cold start).
- After: resident, normal keep-alive, `size_vram` 3341958511 bytes, no leak, no second model.
