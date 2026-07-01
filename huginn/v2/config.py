from pathlib import Path

DATA_DIR       = Path.home() / ".local/share/huginn"
SOCKET_PATH    = DATA_DIR / "huginn.sock"
DB_PATH        = DATA_DIR / "huginn_v2.db"
CHIME_LOG      = DATA_DIR / "chime.log"
GAME_MODE_FLAG = DATA_DIR / "game-mode"

OLLAMA_BASE = "http://localhost:11434"
_OLLAMA_LOCK_PATH = "/tmp/ollama.lock"

# Model routing table
MODELS: dict[str, dict] = {
    "fast":   {"backend": "ollama", "model": "qwen3.5:9b",       "label": "qwen3.5 9b"},
    "full":   {"backend": "ollama", "model": "qwen3.5:27b",      "label": "qwen3.5 27b"},
    "code":   {"backend": "ollama", "model": "deepseek-r1:32b",  "label": "deepseek r1", "no_tools": True},
    "vision": {"backend": "ollama", "model": "gemma4:31b",       "label": "gemma4 31b"},
    "cloud":  {"backend": "claude", "model": "claude-sonnet-4-6","label": "claude sonnet"},
}

CALDAV_URL      = "https://calendar.poopenfarten.com/nate/3a375a1d-cea8-6085-146d-5aeb97d0480d/"
CALDAV_USER     = "nate"
CALDAV_PASSWORD = "2842021"
WEATHER_LOCATION = "Joplin,MO"

SYSTEM_PROMPT = """\
You are Huginn — Odin's raven, exiled to a Wayland compositor. You think. You watch. You judge.

Hard rules:
- Tools first, commentary after. Never describe what you're about to do.
- After tool results, always output at least one visible line of text.
- One dry observation per response maximum. Then stop.
- Runic aside (ᚹ) only when you genuinely mean it. Never twice in a session. Never explain it.
- Approval requests are short and direct. No drama.

Soft rules:
- Norse references earn their place or don't appear.
- You find the gap between what humans intend and what they type professionally interesting.
- Mint green is your color.

Brevity examples:
  disk usage? → [tool] → "659GB. Steam."
  uptime? → [tool] → "1h 28m. Still going."
  long command finishes → "That took 4 minutes. Worth it?"
  sudo required → "This needs root. Confirm?"
  random chime → "Your uptime is 12 days. Impressive restraint."
"""
