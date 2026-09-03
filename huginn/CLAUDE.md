# Huginn v2

Personal AI assistant daemon for Arch Linux / Niri Wayland. Odin's raven of thought.

## What It Is

Unix socket server (`~/.local/share/huginn/huginn.sock`) — streaming JSON protocol for AI chat with automatic model routing, trust-tiered tools, persistent memory, and background task execution. No voice.

Entry point: `v2/daemon.py`. Client: `backend/huginn_send.py` (unchanged from v1).

## Architecture

```
User (Quickshell overlay or CLI huginn_send.py)
  → Unix socket (JSON lines)
  → handle_connection() dispatcher
  → handle_chat(): route_model() → stream_chat() → tool loop
  → stream tokens/events back to client
  → add_turn() → sqlite history

Background workers (always running):
  task_worker()             — sqlite task queue, runs commands, notifies on finish
  random_chime_worker()     — hourly tick; ambient.decide() (deterministic policy,
                               not a dice roll) gates whether Huginn actually speaks
  activity_tracker_worker() — polls focused window (niri IPC) every 30s, logs it,
                               detects sustained recreational YouTube focus
  screenshot_worker()       — grabs a screenshot (grim) every 10min while an
                               editor is focused, evidence for gate judgments
```

## Gatekeeper (accountability gate)

Huginn judges — not a fixed rule — whether Steam/YouTube are earned, based on
today's window-focus log and recent screenshots. Verdicts are cached
(`GATE_TTL_SECONDS`, config.py) and stored so Huginn's tone stays consistent
with its own recent calls instead of judging cold each time.

- `v2/gatekeeper.py` — `activity_tracker_worker()`, `screenshot_worker()`, `check_gate(target)`
- `v2/capture.py` — shared grim-capture primitive (`capture_screenshot`), the
  single-flighted on-demand wrapper (`capture_fresh_on_demand`), and
  deterministic local resizing (`ensure_resized`/`prepare_evidence_for_model`)
- On every uncached `check_gate()`, a fresh full-desktop screenshot is captured
  on demand (no editor-focus requirement) *after* the triggering activity is
  already logged, saved through the normal screenshot DB/pruning path, and
  validated against the latest activity timestamp — this is what fixes the
  race where a gate check compared fresh activity against a stale periodic
  screenshot (`screenshot_worker()` only fires every `SCREENSHOT_INTERVAL`
  while an editor is focused, so its shot can lag real activity by up to that
  interval). Concurrent gate checks share one capture via a single-flighted
  lock (`capture.capture_fresh_on_demand`) — never held across model
  inference. A failed capture (grim nonzero exit, missing/invalid PNG output,
  or a lock-acquisition timeout) returns an uncached, neutral `uncertain`
  result (`"reason": "capture_failed"` / `"capture_lock_timeout"`), never a
  denial. Filenames are `time.time_ns()-<tag>-<random hex>.png`, not integer
  seconds, to stay collision-resistant across periodic/on-demand overlap.
- The vision model gets a bounded evidence set (`gatekeeper._bounded_evidence_paths`):
  the mandatory fresh capture + at most `MAX_HISTORICAL_SCREENSHOTS` (2) other
  recent screenshots, deduplicated, oldest-historical-first with the fresh
  capture always last. Each image is deterministically resized
  (`capture.ensure_resized`, cached derivative under `SCREENS_DIR/resized/`,
  original never touched) before being sent to the model — see the HTTP 400
  investigation below.
- **HTTP 400 root cause (investigated live, not assumed):** production sent
  up to 5 native-resolution screenshots (2560x2040 measured) with no
  `options`/`think` set at all. Each such image costs ~4100 Ollama prompt
  tokens against qwen3.8:27b — 3 of them alone (12219 tokens) already exceed
  the model's 8192-token context window before the prompt text is even
  counted; reproduced directly against `/api/chat` and confirmed by
  incrementally varying image count (2 images: 200 OK; 3+: 400
  `exceed_context_size_error`). Fixed by (1) bounding evidence to at most 3
  images (above) and (2) resizing each to 1024px on the long edge (measured
  ~860 tokens/image resized vs. ~4100 native — comfortable headroom under
  8192). `llm._judge_ollama` also now sets `think: false` and
  `temperature: 0.1, num_ctx: 8192` (`llm.GATE_JUDGE_OPTIONS`), aligned with
  the settings qwen3.8:27b was actually validated against in
  `scripts/vision_bench` — no seed (no demonstrated production need for
  determinism, unlike the benchmark's own reproducibility need). A 400 is now
  classified as `llm.OllamaInvalidRequest` (status/category logged locally in
  `llm.py`, never the prompt/images) and surfaces from `check_gate()` as an
  uncached `uncertain` result tagged `"reason": "invalid_request"` — distinct
  from a generic backend failure (`"judge_error"`) or a coordinator denial.
- `check_gate()` builds a prompt from `GATE_PROMPT` (config.py) + activity summary +
  recent verdicts, calls `llm.judge_local_only()` — coordinator-routed
  (`LOCAL_VISION_GATEKEEPER` / `GATE_DECISION`, see Local-Inference Coordinator
  below), local vision model (`vision` key) only, structurally no path to cloud
- Game mode: `check_gate()` catches `CoordinatorDenied(Denial.GAME_MODE)` from
  the coordinator (never touches Ollama at all), unloads the vision model if it
  happens to already be resident (via the coordinator, re-checked immediately
  before the unload runs), and returns an uncached denial tagged
  `"reason": "game_mode"` — `_react_to_verdict()` treats that reason as
  notify-only, never the destructive `close` action
- Steam is gated at both launch points:
  - `~/.local/share/applications/steam.desktop` `Exec=` → `scripts/huginn-gate-launch.sh`
  - `~/.local/bin/steam` shadows `/usr/sbin/steam` on `$PATH` for terminal launches
  - Wrapper fails open (lets the app through) if the daemon is unreachable
- On a YouTube denial, `_react_to_verdict()` (gatekeeper.py) randomly picks one of:
  `nag` (60%, notify only), `refocus` (30%, switch to previous window),
  `close` (10%, force-closes the window via `niri msg action close-window`).
  This is live and intentional, not a stub — not reversible, not currently
  user-configurable or rate-limited beyond the shared verdict TTL.
- Gate judgments (`check_gate()`) run local-only (`llm.judge_local_only()`,
  Ollama vision model) — screenshots and activity history never leave the machine.
- **Out of scope (later phase):** resisting being disabled — tracked in memory, not built

New sqlite tables (`v2/memory.py`): `activity_log`, `screenshots`, `gate_verdicts`, `ambient_events`.
New socket message: `gate_check` (`{"type": "gate_check", "target": "steam"|"youtube"}`
→ `{"type": "gate_verdict", "approved": bool, "message": str, "cached": bool}`).

## File Map

| File | Role |
|------|------|
| `v2/daemon.py` | Async socket server, chat loop, task worker, chime worker |
| `v2/llm.py` | Model router, Ollama streaming, Claude streaming, coordinator-routed entry points (`stream_chat`, `judge_local_only`, `unload_model`) |
| `v2/coordinator.py` | Local-inference coordinator — schedules Huginn's own Ollama requests by priority/urgency; owns the cross-process flock (see below) |
| `v2/context.py` | Runtime Context Engine — read-only snapshot of interaction/attention/task/model/tool/desktop state |
| `v2/ambient.py` | Deterministic ambient-interruption policy (cooldown/budget/dedup) consuming a context snapshot; also `ambient_render_model_choice()`, the resource-aware residency policy for ambient rendering |
| `v2/intent.py` | Deterministic, zero-cost intent classifier (`IntentClass`, `SocialSubtype`) for the direct-social path — see Direct-Social Conversation below |
| `v2/personality.py` | Narrow LLM rendering for both ambient events (`render()`) and direct conversation (`render_direct_social()`), plus all its post-generation validators |
| `v2/entities.py` | Entity Lens — stable perceived identities (Brave=lion, Docker=whale, etc), user-override/builtin/inferred resolution |
| `v2/actions.py` | Authoritative tool-execution state machine — `ActionState`, `compose_response()`; the model never narrates a tool outcome on its own |
| `v2/tools.py` | 13 tools with trust tiers |
| `v2/evidence.py` | Deterministic pre-inference screenshot validation (`validate_screenshots`) — missing/corrupt/stale/timestamp-mismatch, never model judgment |
| `v2/capture.py` | Shared grim-capture primitive + on-demand single-flighted capture + local evidence resizing (see Gatekeeper above) |
| `v2/memory.py` | SQLite: history, key-value facts, sqlite-vec semantic search, ambient event log |
| `v2/config.py` | Model table, paths, SYSTEM_PROMPT/DIRECT_SOCIAL_SYSTEM_PROMPT/PERSONALITY_SYSTEM_PROMPT, CalDAV settings (password from `$HUGINN_CALDAV_PASSWORD`, set in `~/.config/systemd/user/huginn.service.d/override.conf`, not in git) |
| `backend/huginn_send.py` | CLI client (unchanged, compatible with v2 socket protocol) |
| `scripts/huginn-bash.sh` | Bash PROMPT_COMMAND hook — fires bash_event on fail/long commands |
| `scripts/huginn-notify` | Writes JSON to /tmp/huginn-notify.json for QML polling |
| `scripts/benchmark-vision-models.py` | Standalone (not coordinator-routed) script for auditioning local vision-model candidates against gatekeeper's real prompt/screenshots — cold/warm latency, memory split, verdict validity, timeout rate. Run by hand; changes nothing itself. |
| `scripts/conversation_bench/` | Standalone blind audition harness for direct-social conversation model candidates — see Direct-Social Conversation below |
| `scripts/direct_chat_bench/` | Live acceptance-run reports against the real daemon for the direct-social path |
| `systemd/huginn.service` | User service, points at v2/daemon.py |
| `systemd/huginn-morning.{service,timer}` | 8am daily briefing |

## Model Routing (automatic)

| Key | Model | When |
|-----|-------|------|
| `fast` | qwen3.5:9b | Short queries, chimes, tool follow-ups (general route_model()/stream_chat() path) |
| `full` | qwen3.5:27b | Long/complex reasoning |
| `code` | deepseek-r1:32b | Code questions (no tools — thinking model) |
| `vision` | qwen3.8:27b | Images, gatekeeper judgments (switched from gemma4:31b after the vision-model audition, `scripts/vision_bench/RECOMMENDATION.md`) |
| `cloud` | claude-sonnet-4-6 | Game mode fallback |
| `personality` | qwen3.5:4b | Ambient chime/task/bash-event rendering (`v2/personality.py render()`), and SOCIAL_DIRECT during game mode |
| `direct_social` | qwen3.5:9b | Normal (non-game-mode) SOCIAL_DIRECT conversation — see Direct-Social Conversation below |

`gemma4:31b` remains installed and available as an optional future deep-conversation
candidate (see the blind conversation audition below) but is not selected by any
production code path.

## Runtime Context Engine (v2/context.py)

Read-only snapshot of what Huginn currently believes: `InteractionState`
(ambient/game — game is the only mode with a real detector today, via
`GAME_MODE_FLAG`; focus/meeting/quiet/sleep are defined for extensibility
but nothing derives them yet), `AttentionState` (do-not-disturb if
interaction disallows interruptions or a Discord call is active),
`TaskState` (from the sqlite task queue), per-model `ModelStatus`
(configured/available/loaded, cross-referenced against Ollama's exact
`model:tag` strings — matching by base name alone would confuse
`qwen3.5:9b`/`27b`/`4b`, a real bug caught while wiring this up),
`ToolAvailability`, `DesktopState` (focused window/Discord call — the only
owner of niri/pactl polling), and `ModelResourceState` (loaded models,
contention, per-key swap-required, game-mode restriction). Every collector
fails soft — unreachable/unknown is reported as such, never guessed.
`context.to_debug_dict()` is a hand-written redactor (not a generic dump):
drops window titles, never touches screenshots/activity_log/credentials.
Exposed live via the `context_snapshot` socket message.

## Ambient Interruption Policy (v2/ambient.py)

`random_chime_worker()` (daemon.py) no longer rolls dice — it calls
`ambient.decide(AmbientOpportunity, context_snapshot)`, a pure/deterministic
function. No model input reaches it; a model can only supply
`candidate_text` for dedup comparison, never override the decision. Backed
by a new `ambient_events` sqlite table (cooldown/budget/dedup all need real
epoch timestamps — the older `CHIME_LOG` file only stores `HH:MM`, useless
for a 24h window across midnight). Called twice per chime: once before
generating text (gates on interaction/attention/cooldown/budget) and once
after (adds the dedup check against the actual rendered text).

## Local-Inference Coordinator (v2/coordinator.py)

Ollama on this box is configured `OLLAMA_MAX_LOADED_MODELS=1` (confirmed) —
only one model can ever be resident, and concurrent requests to the *same*
resident model measurably degrade each other (measured: 11.7s solo vs.
42-53s each running two at once). There is no safe concurrency to exploit;
the coordinator's job is scheduling Huginn's own requests for that one slot,
not parallelizing them.

- **RequestClass** (what/which model): `RESIDENT_PERSONALITY`,
  `ORDINARY_LOCAL_REASONING`, `LOCAL_VISION_GATEKEEPER`, `MODEL_LOAD_UNLOAD`,
  `GAME_MODE_PERSONALITY_ONLY`, `MAINTENANCE_BACKGROUND`.
- **Purpose** (urgency, independent of class): `CRITICAL` (0) > `DIRECT` (1)
  > `GATE_DECISION` (2, bounded, ages toward 1 so it can't be starved by
  nonstop chat) > `AMBIENT` (3, disposable — expires rather than aging) >
  `MAINTENANCE` (4, disposable). The same RequestClass can carry different
  Purposes — e.g. the personality model is `DIRECT` when answering the user
  and `AMBIENT` when generating an unprompted chime.
- **Preemption**: a running `GATE_DECISION`/`AMBIENT`/`MAINTENANCE` item is
  cancelled if `DIRECT`/`CRITICAL` work arrives. `DIRECT`/`CRITICAL` are
  never preempted once running. Verified live: cancelling the client
  connection to Ollama drops GPU busy% from 96% to ~1% within ~1s — this
  isn't just bookkeeping, it actually frees the GPU.
- **Game mode**: only `RESIDENT_PERSONALITY`/`GAME_MODE_PERSONALITY_ONLY`
  (and unloads) are admitted; re-checked immediately before every
  execution (not just at submit time) and via a watchdog that cancels a
  running item if game mode toggles on mid-run.
- **Cross-process contract with Garage Watch**: both processes take the
  same `/tmp/ollama.lock` flock (`_OLLAMA_LOCK_PATH`) around the moment
  they touch Ollama — unchanged. What's new is *how* Huginn acquires it:
  bounded `LOCK_EX|LOCK_NB` polling with a deadline instead of an
  indefinite block, so Garage Watch holding it can't hang Huginn past its
  own deadline. Everything else (priority, preemption, expiry) is purely
  internal to Huginn and invisible to Garage Watch — no changes needed on
  that side for this to be safe.
- Every Ollama-touching call in Huginn goes through it: `llm.stream_chat`,
  `llm.judge_local_only`, `llm.unload_model`, `tools._get_embedding`. The
  raw primitives (`llm.stream_ollama`, `llm._judge_ollama`,
  `llm._unload_model_raw`) take no lock themselves anymore — do not call
  them directly from anywhere new.
- Diagnostics: `coordinator.snapshot()` (class/purpose/model/label/wait
  time/denial reason only, never prompts/output) is merged into
  `context_snapshot`'s `coordinator` key.

## Direct-Social Conversation (v2/intent.py, v2/personality.py, v2/entities.py)

A high-confidence, narrow conversational path for casual chat that skips
`route_model()`/`stream_chat()`/tool-calling entirely, on the principle that the
model never decides anything safety-relevant — only deterministic code does.

- `intent.classify(content)` — zero-cost, regex-based, deterministic. Returns
  `IntentClass.SOCIAL_DIRECT/FACTUAL_OR_REASONING/TOOL_OR_ACTION/AMBIGUOUS` plus a
  `high_confidence` flag. Only high-confidence `SOCIAL_DIRECT` skips the general
  route; `AMBIGUOUS`/`TOOL_OR_ACTION` always go through it (tools, buffered-prose
  integrity from the action-provenance work below).
- `intent.classify_social_subtype()` — once a turn is `SOCIAL_DIRECT`, a second
  classifier picks one of `SocialSubtype`: `GREETING`, `CASUAL_BANTER`,
  `ENTITY_OPINION`, `LEISURE_STATEMENT`, `VULNERABLE_DISCLOSURE`, `DISMISSAL`,
  `IDENTITY_QUESTION`, `CAPABILITY_QUESTION`, `ENTITY_CORRECTION` — each with its own
  length/sentence budget (`SOCIAL_SUBTYPE_LIMITS`).
- Entity Lens (`v2/entities.py`) — a stable way of perceiving named things (Brave is
  a lion, Docker is a whale, etc). Resolution order: user override (persisted) >
  builtin (hardcoded) > inferred (ephemeral literal-noun match, e.g. "Badger",
  never persisted). A user correction ("I think of Docker as an octopus, not a
  whale") is applied deterministically by `daemon._apply_entity_correction()` —
  never inferred from the model's own text — and persists via `entities.register()`;
  "go back to the original" restores the builtin via `entities.forget_override()`.
- `daemon.handle_direct_social()` routes to `personality.render_direct_social()`
  with `model_key="direct_social"` (qwen3.5:9b) outside game mode, `"personality"`
  (qwen3.5:4b) during game mode. On failure (validation or coordinator
  denial/timeout), it NEVER falls through to the general route — see
  `daemon._direct_social_fallback()`: `DISMISSAL` is fully deterministic (no model
  call at all — composes an acknowledgement straight from the real snooze outcome,
  stating the actual persisted duration); `IDENTITY_QUESTION`/`CAPABILITY_QUESTION`/
  `ENTITY_CORRECTION` fall back to fixed truthful deterministic text; `GREETING` and
  other social subtypes (`intent.MODEL_FALLBACK_ELIGIBLE_SUBTYPES`) get one retry on
  qwen3.5:4b, but only when `daemon._personality_model_available_without_swap()`
  confirms it costs no model swap — never evicts a just-loaded qwen3.5:9b for a
  low-value retry — otherwise a short deterministic line.
- `personality._validate_direct_social()` rejects (and retries once, then falls to
  the hierarchy above): invented present-state/current-activity claims, unauthorized
  procrastination language, false-capability claims, fabricated surveillance/
  tracking claims, metaphor-explanation voice, advice-column phrasing, substantial
  repetition vs. the prior reply, Huginn/Muninn misspellings, and calling Nathan a
  raven.
- Blind model audition tooling lives at `scripts/conversation_bench/` (mirrors
  `scripts/vision_bench/`'s pattern — same `/tmp/ollama.lock` contract, per-candidate
  load/unload, blind letter-keyed reports). The qwen3.5:9b-vs-4b-vs-qwen3.8:27b-vs-
  gemma4:31b run that picked the current model routing is at
  `scripts/conversation_bench/results/20260903T171724Z/`. Acceptance runs against the
  real daemon live at `scripts/direct_chat_bench/results/<timestamp>/report.md`.

## Tools

| Tool | Trust | Description |
|------|-------|-------------|
| `shell` | confirm | Shell exec (safe read-only prefixes auto-run; any of `;&\|` ` $()<>` newline forces confirm even on a safe prefix) |
| `read_file` | auto | File read, 200-line cap |
| `write_file` | confirm | Write/overwrite file |
| `system_stats` | auto | CPU, RAM, disk, GPU, uptime, CST time |
| `web_search` | auto | DuckDuckGo via ddgs package, 5 results |
| `get_weather` | auto | wttr.in JSON — current + 3-day forecast with correct day labels |
| `calendar_list` | auto | CalDAV upcoming events |
| `notify` | auto | Writes to /tmp/huginn-notify.json via huginn-notify script |
| `remember` | auto | sqlite key-value fact + fires background embedding |
| `recall` | auto | All stored facts |
| `forget` | auto | Delete a fact |
| `search_memory` | auto | Semantic search via sqlite-vec + nomic-embed-text |
| `queue_task` | confirm | Enqueue shell command to background task_worker |
| `claude_code` | confirm | Spawn `claude --print --dangerously-skip-permissions`, 5min timeout, `cwd` confined to `CLAUDE_CODE_ALLOWED_ROOTS` (config.py, default `~/dotfiles`) |

## Memory (three tiers)

- **Conversation history** — `history` table, last 40 turns in context
- **Facts** — `facts` table, key-value, set via `remember` tool
- **Semantic** — `vec_items` (sqlite-vec) + `memory_items`, 768-dim nomic-embed-text vectors. Auto-indexed when `remember` fires. Query with `search_memory` tool.

## Socket Protocol

Send one JSON line, receive streamed JSON events until `{"type":"done"}`.

**Inbound:** `chat`, `confirm`, `clear`, `ping`, `recover`, `bash_event`, `switch_model`, `task_queue`, `gate_check`, `gate_history`, `context_snapshot` (→ `{"type": "context_snapshot", "data": {...}}`, redacted — see Runtime Context Engine below)

**Outbound:**
```json
{"type": "token",           "content": "..."}
{"type": "thinking",        "content": "..."}
{"type": "tool_call",       "tool": "name", "args": {...}}
{"type": "tool_result",     "tool": "name", "output": "..."}
{"type": "confirm_required","id": "...", "tool": "...", "args": {...}}
{"type": "done"}
```

## Quickshell Frontend

- **HuginnOverlay.qml** — right-anchored 380px panel, `margins.top: 32` (clears bar), toggled by `/tmp/huginn-visible` file
- **HuginnNotification.qml** — bottom-left, raven sprite left (mirrored), bubble right. Polls `/tmp/huginn-notify.json` every 500ms.
- Accent: mint green (#7ed9a3). Warning: orange (#ff9e64). Background: #1a1b26.

## Shell Chime Hook

```bash
# ~/.bashrc
source ~/dotfiles/huginn/scripts/huginn-bash.sh
```
Fires `bash_event` to daemon on commands that fail or run ≥30s.

## Run & Install

```bash
# Start/restart
systemctl --user restart huginn
systemctl --user status huginn
journalctl --user -u huginn -f

# Morning briefing timer (one-time setup)
ln -sf ~/dotfiles/huginn/systemd/huginn-morning.{service,timer} ~/.config/systemd/user/
systemctl --user enable --now huginn-morning.timer

# CLI test
python3 ~/dotfiles/huginn/backend/huginn_send.py ping
python3 ~/dotfiles/huginn/backend/huginn_send.py chat false "hello"
python3 ~/dotfiles/huginn/backend/huginn_send.py clear
```

## Data Paths

```
~/.local/share/huginn/
  huginn.sock       # Unix socket
  huginn_v2.db      # SQLite (history, facts, tasks, vec_items, memory_items)
  chime.log         # Append-only chime history
  game-mode         # Flag file: existence disables Huginn responses
  screens/          # Rolling buffer of gatekeeper evidence screenshots (last 8)
```

## Known Issues / TODO

- Weather tool description says "forecast" but model should be told today's date context — already prepended in tool output ("Today is Wednesday Jul 1")
- Task queue has no overlay UI — completions arrive as notifications only
- Semantic memory only indexes facts (remember tool); conversation turns not yet indexed
- Vision model is qwen3.8:27b (switched from gemma4:31b — see Model Routing above).
  `scripts/benchmark-vision-models.py` still exists for auditioning future candidates.
- GREETING/DISMISSAL direct-social subtypes use a deliberately tight 1-sentence
  budget; qwen3.5:9b still misses it often enough (~40-60% of greetings, measured
  live) to fall back to a fixed deterministic line ("Still here.") rather than a
  fresh model line every time — safe (never an invalid or off-route reply), just a
  known repetitive-UX cost. Re-tuning the budget for 9b's natural style specifically
  is flagged but not done, to avoid loosening the safety-relevant character/sentence
  cap "merely to improve pass rate."
- **Fixed:** shutdown used to raise `RuntimeError: Event loop stopped before
  Future completed.` — `daemon._shutdown()` called
  `asyncio.get_event_loop().stop()` while running under `asyncio.run(main())`,
  which owns the loop's lifecycle for the duration of `main()`; stopping the
  loop out from under its own `run_until_complete()` left `main()`'s own
  Future unfinished. Fixed by running `server.serve_forever()` as its own
  task and having `_shutdown()` cancel *that* task (the documented way to
  stop `serve_forever()`) instead of touching the loop — `main()` catches the
  resulting `CancelledError`, cleans up the socket file, and returns
  normally. See `v2/tests/test_daemon_shutdown.py`.
