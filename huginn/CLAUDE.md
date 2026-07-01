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
  task_worker()        — sqlite task queue, runs commands, notifies on finish
  random_chime_worker()— hourly @ 25% chance, dry system observation via notify
```

## File Map

| File | Role |
|------|------|
| `v2/daemon.py` | Async socket server, chat loop, task worker, chime worker |
| `v2/llm.py` | Model router, Ollama streaming, Claude streaming |
| `v2/tools.py` | 13 tools with trust tiers |
| `v2/memory.py` | SQLite: history, key-value facts, sqlite-vec semantic search |
| `v2/config.py` | Model table, paths, SYSTEM_PROMPT, credentials |
| `backend/huginn_send.py` | CLI client (unchanged, compatible with v2 socket protocol) |
| `scripts/huginn-bash.sh` | Bash PROMPT_COMMAND hook — fires bash_event on fail/long commands |
| `scripts/huginn-notify` | Writes JSON to /tmp/huginn-notify.json for QML polling |
| `systemd/huginn.service` | User service, points at v2/daemon.py |
| `systemd/huginn-morning.{service,timer}` | 8am daily briefing |

## Model Routing (automatic)

| Key | Model | When |
|-----|-------|------|
| `fast` | qwen3.5:9b | Short queries, chimes, tool follow-ups |
| `full` | qwen3.5:27b | Long/complex reasoning |
| `code` | deepseek-r1:32b | Code questions (no tools — thinking model) |
| `vision` | gemma4:31b | Images |
| `cloud` | claude-sonnet-4-6 | Game mode fallback |

## Tools

| Tool | Trust | Description |
|------|-------|-------------|
| `shell` | confirm | Shell exec (safe read-only prefixes auto-run) |
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
| `claude_code` | confirm | Spawn `claude --print --dangerously-skip-permissions`, 5min timeout |

## Memory (three tiers)

- **Conversation history** — `history` table, last 40 turns in context
- **Facts** — `facts` table, key-value, set via `remember` tool
- **Semantic** — `vec_items` (sqlite-vec) + `memory_items`, 768-dim nomic-embed-text vectors. Auto-indexed when `remember` fires. Query with `search_memory` tool.

## Socket Protocol

Send one JSON line, receive streamed JSON events until `{"type":"done"}`.

**Inbound:** `chat`, `confirm`, `clear`, `ping`, `recover`, `bash_event`, `switch_model`, `task_queue`

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
```

## Known Issues / TODO

- Weather tool description says "forecast" but model should be told today's date context — already prepended in tool output ("Today is Wednesday Jul 1")
- Task queue has no overlay UI — completions arrive as notifications only
- Semantic memory only indexes facts (remember tool); conversation turns not yet indexed
