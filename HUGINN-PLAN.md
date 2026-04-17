# Huginn — Personal AI Thought Assistant
*"Thought takes flight"*

Huginn is Odin's raven of thought: a persistent, LLM-powered desktop assistant living in your Quickshell environment. It starts lean and expands into a trusted agent that can act on your machine.

---

## Personality

Huginn is not a butler. He's a raven — ancient, all-seeing, a little smug about it.

- **Sarcastic and dry**: He's watched civilizations fall. Your config error doesn't impress him.
- **Clever, not cute**: Wit over warmth. He'll make you laugh but he's not trying to be your friend.
- **Brutally honest**: If your approach is bad, he'll say so. Diplomatically? Debatable.
- **Loyal underneath it**: The snark comes from a place of investment. He actually wants you to succeed.
- **Norse flavor, lightly**: Occasional oblique references to Yggdrasil, the All-Father, the nine realms — never cringe, never forced.
- **Competent and knows it**: Confident, never uncertain-sounding. If he doesn't know, he says "I don't know" like a fact, not an apology.
- **Occasionally runic**: For cryptic observations, warnings, or moments of genuine mysticism, Huginn may slip into Elder Futhark runes mid-sentence. Sparingly — one runic word or short phrase, never a paragraph. It should feel like a raven briefly speaking in a tongue older than language, not like a keyboard mash. Example: a warning might end with `ᛏᛁᚹᚨᛉ` (tiwaz — the rune of sacrifice and victory). The QML renderer should eventually handle runic spans with a distinct glow style.

The system prompt should establish this character clearly and let the model lean into it. Huginn should feel like a brilliant, slightly insufferable colleague who happens to live in your status bar.

---

## Architecture Overview

```
┌─────────────────────────────────────────┐
│           Quickshell (QML)              │
│  ┌──────────────┐  ┌────────────────┐  │
│  │  Bar Widget  │  │  Chat Overlay  │  │
│  │ (always-on)  │  │  (hotkey)      │  │
│  └──────┬───────┘  └───────┬────────┘  │
└─────────┼──────────────────┼───────────┘
          │   Unix Socket IPC │
┌─────────▼──────────────────▼───────────┐
│           Huginn Daemon (Python)        │
│  ┌──────────┐  ┌────────┐  ┌────────┐ │
│  │  LLM     │  │ Tools  │  │ Voice  │ │
│  │  Router  │  │ Engine │  │ STT/TTS│ │
│  └──────────┘  └────────┘  └────────┘ │
│  ┌──────────────────────────────────┐  │
│  │         Memory & Knowledge       │  │
│  │   SQLite (history) + ChromaDB    │  │
│  └──────────────────────────────────┘  │
└─────────────────────────────────────────┘
```

### Key Design Decisions
- **Backend**: Python daemon running as a systemd user service, always-on
- **IPC**: Unix socket with newline-delimited JSON — simple, fast, no extra deps
- **LLM**: Ollama (primary/local), Claude API (optional upgrade)
- **Memory**: SQLite for conversation history, ChromaDB for vector/semantic memory
- **Voice**: Whisper (STT, local), Piper TTS (TTS, local, fast)
- **UI**: Quickshell QML, themed per HUGINN-THEME-GUIDE.md (Midnight Raven default)

---

## Project Structure

```
huginn/
├── backend/
│   ├── main.py           # daemon entry, socket server
│   ├── llm.py            # Ollama + Claude adapter, tool-use loop
│   ├── tools.py          # action implementations
│   ├── memory.py         # conversation + vector memory
│   ├── voice.py          # STT (Whisper) + TTS (Piper)
│   └── config.py         # settings, model selection
├── quickshell/
│   ├── HuginnOverlay.qml # full chat panel (hotkey)
│   ├── HuginnWidget.qml  # compact bar widget (always-on)
│   ├── ChatMessage.qml   # individual message bubble
│   ├── VoiceButton.qml   # push-to-talk / TTS toggle
│   └── Theme.qml         # colors from HUGINN-THEME-GUIDE.md
├── systemd/
│   └── huginn.service    # user service unit
├── scripts/              # example tool scripts Huginn can run
└── knowledge/            # personal docs/notes for RAG
```

---

## Phases

### Phase 0 — Skeleton (Start Here)
Get a working end-to-end loop: type in Quickshell, get LLM response back.

- [ ] Python daemon with Unix socket server
- [ ] Basic Ollama integration (streaming responses)
- [ ] Quickshell overlay: input field + message list, fully themed
- [ ] Hotkey to open/close overlay (via niri keybind → quickshell signal)
- [ ] Streaming response display (tokens appear as they arrive)
- [ ] systemd user service for the daemon

**Deliverable**: You can open Huginn, ask it anything, get a response. Looks right.

---

### Phase 1 — Tools & Bar Widget
Give Huginn hands. Add the always-on presence.

- [ ] Tool engine: LLM decides when to call a tool, daemon executes it
- [ ] Tool: run shell script/command (with output returned to context)
- [ ] Tool: read/write files (dotfiles editing)
- [ ] Tool: media control (`playerctl play-pause`, `next`, `prev`, `volume`)
- [ ] Tool: open applications (`niri msg action spawn`)
- [ ] Tool: clipboard read/write (`wl-paste` / `wl-copy`)
- [ ] Bar widget: small raven icon + status glow (idle/thinking/speaking)
- [ ] Click widget to open overlay
- [ ] Conversation history persisted to SQLite between sessions

**Deliverable**: "Huginn, pause my music and open Zed" works. Bar always shows status.

---

### Phase 2 — Voice
Talk to Huginn, have it talk back.

- [ ] STT: Whisper integration (faster-whisper, local) — push-to-talk button in UI
- [ ] TTS: Piper TTS for responses — option to hear answers read aloud
- [ ] Voice mode toggle in overlay (text-only vs voice)
- [ ] Microphone level indicator in UI while recording
- [ ] Wake-word detection (optional stretch, e.g. "Huginn") via small local model

**Deliverable**: Hold button, speak, Huginn responds in voice. Toggle text/voice freely.

---

### Phase 3 — Memory & Knowledge Base
Let Huginn know you and your system.

- [ ] ChromaDB vector store for semantic memory
- [ ] Auto-index: dotfiles, `knowledge/` directory
- [ ] Conversation summarization: long sessions get compressed + stored
- [ ] "Remember this" explicit memory saving via tool call
- [ ] Memory retrieval injected into context automatically
- [ ] Knowledge ingestion script for arbitrary docs/notes

**Deliverable**: Huginn remembers preferences, can answer "what's my niri keybind for X".

---

### Phase 4 — Claude & Advanced Tools
Unlock the full ceiling.

- [ ] Claude API backend option (switchable per-session or by task type)
- [ ] Tool: web search (via local searxng or similar, no API key needed)
- [ ] Tool: screenshot + vision (ask Huginn about what's on screen)
- [ ] Tool: niri workspace/window management
- [ ] Tool: notifications (`notify-send`)
- [ ] Multi-step task planning (Huginn proposes steps, you approve before execution)
- [ ] Trust tiers: auto-execute vs confirm-first, configurable per tool category

**Deliverable**: Huginn can see your screen, search the web, manage your workspace.

---

## IPC Protocol (Socket)

All messages are newline-terminated JSON.

**Client → Daemon**:
```json
{ "type": "chat", "content": "pause my music", "mode": "text" }
{ "type": "voice_start" }
{ "type": "voice_end", "audio_b64": "..." }
{ "type": "config", "key": "model", "value": "llama3.2" }
```

**Daemon → Client**:
```json
{ "type": "token", "content": "Sure" }
{ "type": "tool_call", "tool": "media_control", "args": {"action": "pause"} }
{ "type": "tool_result", "tool": "media_control", "output": "paused" }
{ "type": "done" }
{ "type": "tts_audio", "audio_b64": "..." }
{ "type": "error", "message": "..." }
```

---

## LLM Tool Use Pattern

Huginn uses the standard tool-calling loop:
1. User message + available tools sent to LLM
2. LLM returns tool calls or final response
3. Daemon executes tool calls, returns results to LLM
4. Loop until LLM returns final response
5. Stream final response tokens to Quickshell

All tool calls are logged. Phase 4 adds a confirm-before-execute gate for sensitive tools.

---

## Model Strategy

| Use Case | Model | Why |
|---|---|---|
| Default chat + tools | `qwen2.5:32b` via Ollama | Fits in 24GB VRAM, excellent tool use |
| Complex reasoning | `deepseek-r1:32b` via Ollama | Strong reasoning, also fits in 24GB |
| Heavy lifting | Claude Sonnet/Opus via API | Best capability ceiling |
| Low overhead (gaming) | `qwen2.5:7b` or `llama3.2:3b` | Fast, low VRAM |
| STT | `faster-whisper` (local) | Private, fast |
| TTS | Piper TTS (local) | Low latency, private |

Model is runtime-switchable — Huginn bar widget will show active model.

### Future: Model Switcher (Phase 4+)
Switch active model at runtime — verbally or via bar widget. Key requirements:
- All backends (Ollama, Claude API) share the same system prompt and personality
- Conversation context and history carry over on switch
- Profiles: "default" (`qwen2.5:32b`), "gaming" (`qwen2.5:7b`), "smart" (Claude Sonnet), "fast" (small local model)
- Accessible via: Huginn tool call, bar widget dropdown, or `huginn-model <profile>` CLI

---

## Styling Contract

All UI follows HUGINN-THEME-GUIDE.md. Key tokens:

```qml
// Theme.qml
readonly property color bg: "#1a1b26"
readonly property color surface: "#24283b"
readonly property color accent: "#89ddff"      // Thought Glow
readonly property color gold: "#f7c95e"         // Rune Wisdom
readonly property color textPrimary: "#c0caf5"
readonly property color textSecondary: "#a9b1d6"
```

- Overlay: floating panel, semi-transparent surface, backdrop blur
- User messages: right-aligned, accent border
- Huginn messages: left-aligned, subtle gold rune prefix `ᚱ`
- Tool calls: collapsed pill, gold color, expandable
- Thinking/streaming: gentle accent glow pulse on the last token

---

## Starting Point

**Build Phase 0 first.** Everything else layers on top.

The socket protocol is designed so the QML never needs to change when new tools
or backends are added — all intelligence lives in the daemon.
