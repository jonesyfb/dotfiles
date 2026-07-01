# Huginn

Personal AI assistant daemon for Arch Linux / Niri Wayland. Named after Odin's raven of thought.

## What It Is

Unix socket server (`~/.local/share/huginn/huginn.sock`) that exposes a streaming JSON protocol for AI chat with:
- Multi-model support: Ollama (local) + Claude API (remote)
- Semantic knowledge base over dotfiles + notes (ChromaDB)
- Persistent memory + conversation history (SQLite)
- 16 desktop automation tools (file ops, media, theme switching, etc.)
- Optional voice I/O (Whisper STT + Piper TTS)
- Trust-tiered tool execution (auto vs confirm-before-run)

Entry point: `backend/daemon.py`. Client: `backend/huginn_send.py`.

## Architecture

```
User (Quickshell UI or CLI)
  → Unix socket (JSON protocol)
  → HuginnDaemon.handle_message()
  → load memories + knowledge (parallel)
  → LLM call (Ollama or Claude)
  → tool call loop (max 6 rounds)
  → stream tokens/events back to client
  → save to SQLite history
```

## File Map

| File | Role |
|------|------|
| `backend/daemon.py` | Async socket server, message router, chat loop |
| `backend/llm.py` | LLM abstraction — Ollama streaming + Claude SDK |
| `backend/tools.py` | 16 tools with trust tiers |
| `backend/memory.py` | SQLite: chat history + key-value memories |
| `backend/knowledge.py` | ChromaDB semantic search over dotfiles/notes |
| `backend/voice.py` | Whisper STT + Piper TTS |
| `backend/theme.py` | Multi-app theme switcher (Kitty, Niri, Vim) |
| `backend/config.py` | Profiles, paths, constants |
| `backend/huginn_send.py` | CLI client |
| `scripts/huginn-toggle.sh` | Toggle visibility flag for Quickshell overlay |
| `scripts/huginn-theme.sh` | CLI wrapper for theme.py |
| `systemd/huginn.service` | User systemd unit |

## LLM Profiles

Defined in `config.py`. Switch with `huginn_send.py switch_model <profile>`.

| Profile | Model | Backend |
|---------|-------|---------|
| `fast` (default) | qwen3.5:9b | Ollama |
| `default` | qwen3.5:27b | Ollama |
| `vision` | gemma4:31b | Ollama |
| `reason` | deepseek-r1:32b | Ollama (no tools, think-block parsing) |
| `smart` | claude-sonnet-4-6 | Anthropic |
| `opus` | claude-opus-4-7 | Anthropic |
| `haiku` | claude-haiku-4-5-20251001 | Anthropic |

## Socket Protocol

Send newline-delimited JSON, receive streamed JSON events until `done`.

**Inbound message types**: `chat`, `voice_file`, `confirm`, `switch_model`, `clear`, `ping`

**Outbound event types**:
```json
{"type": "token",          "content": "..."}
{"type": "thinking",       "content": "..."}
{"type": "tool_call",      "tool": "name", "args": {...}}
{"type": "tool_result",    "tool": "name", "output": "..."}
{"type": "confirm_required","id": "...", "tool": "...", "args": {...}}
{"type": "done"}
```

## Tools

| Tool | Trust | Description |
|------|-------|-------------|
| `run_command` | confirm | Shell exec, 30s timeout, 3KB output cap |
| `read_file` | auto | File read, 5KB cap |
| `write_file` | confirm | Write/overwrite file |
| `media_control` | auto | playerctl: play/pause/next/prev/volume/status |
| `open_app` | auto | Spawn via niri or direct exec |
| `get_clipboard` | auto | wl-paste |
| `set_clipboard` | auto | wl-copy |
| `index_knowledge` | auto | Index file/dir into ChromaDB |
| `remember` | auto | Save key-value to SQLite |
| `forget` | auto | Delete memory by key |
| `notify` | auto | notify-send desktop notification |
| `niri_action` | auto | Compositor control (workspaces, windows, config reload) |
| `web_search` | auto | DuckDuckGo, returns title/URL/snippet |
| `screenshot` | auto | Capture + analyze with Claude vision (fallback: llava) |
| `switch_model` | auto | Switch active profile |
| `switch_theme` | auto | Apply theme via theme.py |

Trust tiers: `auto` = execute immediately; `confirm` = pause and await client approval (60s timeout).

## Knowledge Base

ChromaDB at `~/.local/share/huginn/chroma/`. Indexed on daemon boot in background thread.

- Indexed paths: `~/dotfiles/` + `~/dotfiles/huginn/knowledge/`
- Indexed extensions: `.md .txt .py .qml .kdl .conf .vim .toml .json .sh .zsh .bash .fish`
- Chunk size: 400 chars, 80-char overlap
- Query: top 4 results, L2 distance threshold 1.4
- Re-index on demand: `index_knowledge` tool or `huginn_send.py` → tool call

## Memory

SQLite at `~/.local/share/huginn/huginn.db`.

- `messages` table: full chat history (role, content, tool_calls JSON)
- `memories` table: persistent key-value facts
- Loads last 20 messages on daemon boot
- Auto-compresses history at 30+ messages: summarizes older messages, keeps last 10 verbatim

## Theme System

4 themes: `midnight` (default), `obsidian`, `ember`, `verdant`. Each is a JSON in `themes/`.

`theme.py` updates live:
- **Kitty**: writes `~/.config/kitty/huginn-theme.conf`, reloads via `kitten @set-colors --all`
- **Niri**: regex-patches focus ring gradient in `~/dotfiles/niri/config.kdl`, reloads config
- **Vim**: writes `~/.config/huginn/huginn-theme.vim`, sourced at startup

## Data Paths

```
~/.local/share/huginn/
  huginn.sock      # Unix socket
  huginn.db        # SQLite
  chroma/          # ChromaDB

~/.config/huginn/
  huginn-theme.vim # Vim theme (generated)

~/.config/kitty/
  huginn-theme.conf # Kitty theme (generated)
```

## Environment

- `ANTHROPIC_API_KEY` — Required for Claude profiles; Ollama profiles work without it

## Install & Run

```bash
# Install
cd ~/dotfiles/huginn && ./install.sh

# Manual start
uv run --project ~/dotfiles/huginn python3 ~/dotfiles/huginn/backend/daemon.py

# Systemd
systemctl --user start huginn
systemctl --user status huginn
journalctl --user -u huginn -f
```

## Client Usage

```bash
huginn_send.py ping                            # Check daemon status
huginn_send.py chat false "message"            # Chat, no TTS
huginn_send.py chat true "message"             # Chat + TTS
huginn_send.py voice_file /tmp/audio.wav true  # Transcribe + chat + TTS
huginn_send.py switch_model smart              # Switch to Claude Sonnet
huginn_send.py confirm <id> true               # Approve tool execution
huginn_send.py clear                           # Wipe history
```

## Voice Setup (optional)

```bash
yay -S piper-tts-bin
mkdir -p ~/.local/share/piper && cd ~/.local/share/piper
wget https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json
```

## Key Constants (config.py)

- `MAX_TOOL_ROUNDS = 6` — Max LLM tool-call iterations per message
- `HISTORY_LIMIT = 20` — Messages loaded on boot
- `COMPRESS_THRESHOLD = 30` — Trigger history summarization
- `KEEP_RECENT = 10` — Messages preserved verbatim during compression
- Whisper model: `base.en`
- Piper model: `en_US-lessac-medium`
