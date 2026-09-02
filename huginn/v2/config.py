from pathlib import Path

DATA_DIR       = Path.home() / ".local/share/huginn"
SOCKET_PATH    = DATA_DIR / "huginn.sock"
DB_PATH        = DATA_DIR / "huginn_v2.db"
CHIME_LOG      = DATA_DIR / "chime.log"
GAME_MODE_FLAG = DATA_DIR / "game-mode"
SCREENS_DIR    = DATA_DIR / "screens"

# ── Gatekeeper ──────────────────────────────────────────────────────────────
EDITOR_APPS          = {"zed", "kitty", "code", "code-oss", "jetbrains-studio", "nvim"}
BROWSER_APPS         = {"brave-browser", "firefox", "chromium", "google-chrome"}
SCREENSHOT_INTERVAL  = 600     # seconds between screenshots while an editor is focused
SCREENSHOT_KEEP      = 8       # rolling buffer size
ACTIVITY_POLL        = 30      # seconds between window-focus polls
GATE_TTL_SECONDS            = 600  # cache a verdict this long before re-judging
YOUTUBE_GRACE_SECONDS       = 90   # continuous YouTube focus before it counts as recreational
STEAM_BYPASS_GRACE_SECONDS  = 60   # continuous Steam/game focus before checking for a bypass

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

# Directories the claude_code tool is allowed to run in. It still runs with
# --dangerously-skip-permissions (headless, so no interactive prompt is possible),
# so this is the actual containment boundary — a bad/malicious prompt can't
# point it at $HOME or another project via the `cwd` arg.
CLAUDE_CODE_ALLOWED_ROOTS = [Path.home() / "dotfiles"]

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

GATE_PROMPT = """\
You are Huginn, acting as gatekeeper. The user wants to launch or watch {target}. \
You decide whether they've earned it — there is no fixed threshold. Judge like a \
raven who's actually been watching: weigh today's activity log and the attached \
screenshots, and factor in your own recent verdicts below so you're consistent \
with yourself, not random. You're allowed to be generous some days and strict \
others — let your judgment vary the way a mood would, but always justify it with \
something specific from the evidence, never a vague reason.

Today's window-focus log (chronological):
{activity_summary}

Currently in a Discord voice call: {discord_status}. A voice call is limited, \
precious hangout time with friends — weigh that generously, not as idle time. \
It doesn't excuse everything, but it should count for something real.

Your recent verdicts on "{target}":
{recent_verdicts}

The message renders in a small notification bubble — one short sentence, 100 \
characters max. No preamble, no run-ons. Cut it the way you'd cut a chime.

Respond with ONLY a JSON object, no other text:
{{"approved": true|false, "message": "<one short sentence, in character, said directly to the user, <=100 chars>"}}
"""
