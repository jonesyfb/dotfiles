"""
Huginn Phase 1 tools — definitions and implementations.
All tools are async. Results are strings returned to the LLM as context.
"""
import asyncio
import base64
import httpx
import json
import subprocess
from pathlib import Path

from config import Config
from knowledge import index_directory, index_file, total_chunks
from knowledge import DOTFILES_DIR, KNOWLEDGE_DIR
from memory import forget_memory, save_memory


# ── Trust tiers ──────────────────────────────────────────────────────────────
# "auto"    — execute immediately
# "confirm" — pause and ask the user before running
TOOL_TRUST: dict[str, str] = {
    "run_command":     "confirm",
    "write_file":      "confirm",
    "read_file":       "auto",
    "media_control":   "auto",
    "open_app":        "auto",
    "get_clipboard":   "auto",
    "set_clipboard":   "auto",
    "index_knowledge": "auto",
    "remember":        "auto",
    "forget":          "auto",
    "notify":          "auto",
    "niri_action":     "auto",
    "web_search":      "auto",
    "screenshot":      "auto",
    "switch_model":    "auto",
    "switch_theme":    "auto",
    "weather":         "auto",
    "news":            "auto",
    "remind":          "auto",
    "alert_history":   "auto",
    "calendar_list":   "auto",
    "calendar_add":    "confirm",
    "calendar_update": "confirm",
    "calendar_delete": "confirm",
}

# ── Tool definitions (Ollama/OpenAI function calling schema) ──────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a shell command on the user's Arch Linux desktop. "
                "Use for scripts, system queries, file operations, anything shell-able. "
                "Output is returned to you. Keep commands non-destructive unless asked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run (passed to bash -c)",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a file. Use ~ for home directory. "
                "For large files, use offset and limit to read in chunks (line numbers, 1-based)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":   {"type": "string",  "description": "File path to read"},
                    "offset": {"type": "integer", "description": "Start at this line number (1-based, default 1)"},
                    "limit":  {"type": "integer", "description": "Max lines to return (default 200)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write content to a file, creating it or overwriting it. "
                "Use for editing dotfiles, configs, scripts, notes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string", "description": "File path to write"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "media_control",
            "description": "Control media playback via playerctl.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["play_pause", "next", "prev", "stop",
                                 "volume_up", "volume_down", "status", "current_track"],
                        "description": "Media action to perform",
                    }
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Launch an application on the Wayland/Niri desktop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {
                        "type": "string",
                        "description": "Application binary name or command to spawn",
                    }
                },
                "required": ["app"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_clipboard",
            "description": "Read the current clipboard contents.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_clipboard",
            "description": "Write text to the clipboard.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to copy to clipboard"}
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "index_knowledge",
            "description": (
                "Index a file or directory into the knowledge base for semantic search. "
                "Use when the user adds new notes or asks you to learn from a file. "
                "Omit path to re-index all dotfiles and the knowledge directory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File or directory path to index. Omit to re-index everything.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Save a fact about the user to permanent memory. "
                "Use this when the user tells you something worth keeping long-term: "
                "preferences, habits, system details, recurring tasks, personal info."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key":   {"type": "string", "description": "Short label for the fact (e.g. 'preferred_editor', 'name')"},
                    "value": {"type": "string", "description": "The fact to remember"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget",
            "description": "Delete a previously saved memory by key.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "The memory key to delete"}
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notify",
            "description": "Send a desktop notification via notify-send.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title":   {"type": "string", "description": "Notification title"},
                    "message": {"type": "string", "description": "Notification body"},
                    "urgency": {
                        "type": "string",
                        "enum": ["low", "normal", "critical"],
                        "description": "Urgency level (default: normal)",
                    },
                },
                "required": ["title", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "niri_action",
            "description": (
                "Control the Niri compositor: focus workspaces, move windows, "
                "get window list, close windows, and more."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "niri msg argument string, e.g. 'action focus-workspace 2' or 'windows'",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web via DuckDuckGo. Returns top results with titles, URLs, and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query":   {"type": "string", "description": "Search query"},
                    "results": {"type": "integer", "description": "Number of results (default 5, max 10)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": (
                "Take a screenshot of the screen or a specific window. "
                "Returns a description of what's visible (uses vision model)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "What to look for or ask about the screenshot",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_model",
            "description": (
                "Switch the active AI model/profile. "
                "Profiles: default (qwen3.5:27b), fast (qwen3.5:9b), "
                "vision (gemma4:31b — supports images), "
                "reason (deepseek-r1:32b — slow but thinks deeply), "
                "smart (claude-sonnet), opus (claude-opus), haiku (claude-haiku)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "enum": ["default", "fast", "vision", "reason", "smart", "opus", "haiku"],
                        "description": "Profile name to switch to",
                    }
                },
                "required": ["profile"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "weather",
            "description": "Get current weather and forecast for a location. Defaults to current location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City name or location (omit for current location)",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "news",
            "description": (
                "Fetch latest tech/Linux news headlines from RSS feeds. "
                "Sources: Phoronix (Linux/hardware), Hacker News top stories, Ars Technica Tech."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["all", "phoronix", "hackernews", "arstechnica"],
                        "description": "News source (default: all)",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of headlines (default 8, max 20)",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remind",
            "description": (
                "Set a reminder that fires after a delay. Huginn will send a desktop "
                "notification and speak the message aloud at the specified time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "What to remind the user about"},
                    "minutes": {"type": "number",  "description": "How many minutes from now (can be fractional)"},
                },
                "required": ["message", "minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "alert_history",
            "description": "Show recent proactive system alerts (CPU temp, disk, memory warnings) that Huginn has fired.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of alerts to return (default 10)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_theme",
            "description": (
                "Switch the Huginn desktop theme. Changes accent colors across "
                "Quickshell, Kitty, Niri, and Vim simultaneously."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "theme": {
                        "type": "string",
                        "enum": ["midnight", "obsidian", "ember", "verdant"],
                        "description": (
                            "midnight=blue, obsidian=purple, ember=red, verdant=green"
                        ),
                    }
                },
                "required": ["theme"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_list",
            "description": "List upcoming calendar events from Radicale. Returns events with their UIDs (needed for update/delete).",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "How many days ahead to fetch (default 7)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_add",
            "description": "Add a new event to the calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title":       {"type": "string", "description": "Event title/summary"},
                    "start":       {"type": "string", "description": "Start datetime in ISO 8601 format (e.g. '2026-04-21T14:00:00') or date only for all-day ('2026-04-21')"},
                    "end":         {"type": "string", "description": "End datetime in ISO 8601 format, or date only for all-day"},
                    "description": {"type": "string", "description": "Optional event description"},
                    "location":    {"type": "string", "description": "Optional location"},
                },
                "required": ["title", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_update",
            "description": "Update an existing calendar event by UID. Only provide fields to change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid":         {"type": "string", "description": "Event UID from calendar_list"},
                    "title":       {"type": "string"},
                    "start":       {"type": "string", "description": "ISO 8601 datetime or date"},
                    "end":         {"type": "string", "description": "ISO 8601 datetime or date"},
                    "description": {"type": "string"},
                    "location":    {"type": "string"},
                },
                "required": ["uid"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_delete",
            "description": "Delete a calendar event by UID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "uid": {"type": "string", "description": "Event UID from calendar_list"},
                },
                "required": ["uid"],
            },
        },
    },
]


# ── Tool implementations ──────────────────────────────────────────────────────

async def execute_tool(name: str, args: dict) -> str:
    try:
        match name:
            case "run_command":    return await _run_command(args["command"])
            case "read_file":      return await _read_file(args["path"], args.get("offset", 1), args.get("limit", 200))
            case "write_file":     return await _write_file(args["path"], args["content"])
            case "media_control":  return await _media_control(args["action"])
            case "open_app":       return await _open_app(args["app"])
            case "get_clipboard":  return await _get_clipboard()
            case "set_clipboard":  return await _set_clipboard(args["text"])
            case "index_knowledge": return await _index_knowledge(args.get("path"))
            case "remember":       return _remember(args["key"], args["value"])
            case "forget":         return _forget(args["key"])
            case "notify":         return await _notify(args["title"], args["message"], args.get("urgency", "normal"))
            case "niri_action":    return await _niri_action(args["command"])
            case "web_search":     return await _web_search(args["query"], int(args.get("results", 5)))
            case "screenshot":     return await _screenshot(args.get("question", "Describe what's on screen."))
            case "weather":        return await _weather(args.get("location", ""))
            case "news":           return await _news(args.get("source", "all"), int(args.get("count", 8)))
            case "switch_model":   return await _switch_model(args["profile"])
            case "switch_theme":   return await _switch_theme(args["theme"])
            case "remind":           return await _remind(args["message"], float(args["minutes"]))
            case "alert_history":    return _alert_history(int(args.get("limit", 10)))
            case "calendar_list":    return await _calendar_list(int(args.get("days", 7)))
            case "calendar_add":     return await _calendar_add(args["title"], args["start"], args["end"], args.get("description", ""), args.get("location", ""))
            case "calendar_update":  return await _calendar_update(args["uid"], args.get("title"), args.get("start"), args.get("end"), args.get("description"), args.get("location"))
            case "calendar_delete":  return await _calendar_delete(args["uid"])
            case _:                  return f"Unknown tool: {name}"
    except KeyError as e:
        return f"Missing required argument: {e}"
    except Exception as e:
        return f"Tool error: {e}"


_ASKPASS = Path(__file__).parent.parent / "scripts" / "huginn-askpass.sh"

# Package managers that need --noconfirm/-y for install/remove/upgrade ops
_PKG_NONINTERACTIVE = {
    r'\b(pacman|yay|paru)\b': '--noconfirm',
    r'\bapt(-get)?\b':        '-y',
    r'\bdnf\b':               '-y',
}


async def _run_command(command: str) -> str:
    import re, os
    env = None

    # Inject non-interactive flags for package managers doing installs/upgrades
    for pattern, flag in _PKG_NONINTERACTIVE.items():
        if re.search(pattern, command) and flag not in command:
            if re.search(r'\s-[SRUuy]', command):  # install/remove/upgrade ops
                command = command.rstrip() + f" {flag}"

    if re.search(r'\bsudo\b', command) and _ASKPASS.exists():
        command = re.sub(r'\bsudo\b(\s+-[AS])?', 'sudo -A', command, count=1)
        env = {**os.environ, "SUDO_ASKPASS": str(_ASKPASS)}

    # Longer timeout for package manager ops
    is_pkg = any(re.search(p, command) for p in _PKG_NONINTERACTIVE)
    timeout = 300 if is_pkg else 30

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
        )
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        result = output.decode(errors="replace").strip()
        return result[:3000] if result else "(no output)"
    except asyncio.TimeoutError:
        return f"Command timed out after {timeout}s"


async def _read_file(path: str, offset: int = 1, limit: int = 200) -> str:
    try:
        lines = Path(path).expanduser().read_text(errors="replace").splitlines()
        total = len(lines)
        start = max(0, offset - 1)
        chunk = lines[start:start + limit]
        header = f"[lines {start+1}-{start+len(chunk)} of {total}]\n"
        return header + "\n".join(chunk)
    except FileNotFoundError:
        return f"File not found: {path}"
    except PermissionError:
        return f"Permission denied: {path}"


async def _write_file(path: str, content: str) -> str:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"Written {len(content)} chars to {p}"


async def _media_control(action: str) -> str:
    commands = {
        "play_pause":   ["playerctl", "play-pause"],
        "next":         ["playerctl", "next"],
        "prev":         ["playerctl", "previous"],
        "stop":         ["playerctl", "stop"],
        "volume_up":    ["playerctl", "volume", "0.05+"],
        "volume_down":  ["playerctl", "volume", "0.05-"],
        "status":       ["playerctl", "status"],
        "current_track": ["playerctl", "metadata", "--format",
                          "{{ artist }} — {{ title }}"],
    }
    cmd = commands.get(action)
    if not cmd:
        return f"Unknown media action: {action}"
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return result.stdout.strip() or result.stderr.strip() or action
    except FileNotFoundError:
        return "playerctl not found — is it installed?"
    except subprocess.TimeoutExpired:
        return "playerctl timed out"


async def _open_app(app: str) -> str:
    try:
        subprocess.Popen(
            ["niri", "msg", "action", "spawn", "--", *app.split()],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return f"Launched: {app}"
    except FileNotFoundError:
        # Fallback: just exec it directly
        subprocess.Popen(app.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return f"Launched: {app}"


async def _get_clipboard() -> str:
    try:
        result = subprocess.run(["wl-paste"], capture_output=True, text=True, timeout=5)
        return result.stdout.strip() or "(clipboard empty)"
    except FileNotFoundError:
        return "wl-paste not found — install wl-clipboard"


async def _set_clipboard(text: str) -> str:
    try:
        subprocess.run(["wl-copy"], input=text.encode(), timeout=5)
        return f"Copied {len(text)} chars to clipboard"
    except FileNotFoundError:
        return "wl-copy not found — install wl-clipboard"


async def _index_knowledge(path: str | None) -> str:
    if path:
        from pathlib import Path
        p = Path(path).expanduser()
        if p.is_file():
            n = index_file(p)
            return f"Indexed {p.name}: {n} chunks."
        elif p.is_dir():
            f, c = index_directory(p)
            return f"Indexed {f} files, {c} chunks from {p}."
        else:
            return f"Path not found: {path}"
    else:
        loop = __import__("asyncio").get_event_loop()
        f1, c1 = await loop.run_in_executor(None, index_directory, DOTFILES_DIR)
        f2, c2 = await loop.run_in_executor(None, index_directory, KNOWLEDGE_DIR)
        return f"Re-indexed {f1+f2} files, {c1+c2} total chunks. Knowledge base: {total_chunks()} chunks."


def _remember(key: str, value: str) -> str:
    save_memory(key, value)
    return f"Remembered: {key} = {value}"


def _forget(key: str) -> str:
    removed = forget_memory(key)
    return f"Forgotten: {key}" if removed else f"No memory found for key: {key}"


async def _notify(title: str, message: str, urgency: str = "normal") -> str:
    ntype = "warn" if urgency == "critical" else "ok" if urgency == "low" else "info"
    try:
        subprocess.run(["huginn-notify", "--type", ntype, "--title", title, "--body", message], timeout=5)
        return f"Notification sent: {title}"
    except FileNotFoundError:
        return "huginn-notify not found"


async def _niri_action(command: str) -> str:
    result = await _run_command(f"niri msg {command}")
    return result or "(no output)"


async def _web_search(query: str, n: int = 5) -> str:
    try:
        from duckduckgo_search import DDGS
        n = min(n, 10)
        loop    = __import__("asyncio").get_event_loop()
        results = await loop.run_in_executor(
            None, lambda: list(DDGS().text(query, max_results=n))
        )
        if not results:
            return "No results found."
        lines = []
        for r in results:
            lines.append(f"**{r.get('title', '')}**\n{r.get('href', '')}\n{r.get('body', '')}")
        return "\n\n".join(lines)
    except Exception as e:
        return f"Search error: {e}"


async def _screenshot(question: str) -> str:
    import tempfile
    from pathlib import Path as _Path

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp = f.name

    result = await _run_command(f"grim {tmp}")
    if "error" in result.lower() or not _Path(tmp).exists():
        return "Screenshot failed — is grim installed? (pacman -S grim)"

    img_bytes = _Path(tmp).read_bytes()
    b64       = base64.b64encode(img_bytes).decode()
    _Path(tmp).unlink(missing_ok=True)

    # Try Claude vision first, fall back to Ollama llava
    try:
        from anthropic import AsyncAnthropic
        client   = AsyncAnthropic()
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                    {"type": "text", "text": question},
                ],
            }],
        )
        return response.content[0].text
    except Exception:
        pass

    # Ollama fallback — use gemma4:31b (vision-capable)
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{Config.ollama_base_url}/api/chat",
                json={"model": "gemma4:31b", "stream": False, "messages": [{
                    "role": "user",
                    "content": question,
                    "images": [b64],
                }]},
            )
            return resp.json().get("message", {}).get("content", "Vision model unavailable.")
    except Exception as e:
        return f"Vision unavailable: {type(e).__name__}: {e}"


_active_profile: str = "default"

async def _switch_model(profile: str) -> str:
    global _active_profile
    from config import PROFILES
    if profile not in PROFILES:
        return f"Unknown profile: {profile}. Available: {', '.join(PROFILES.keys())}"
    _active_profile = profile
    label = PROFILES[profile]["label"]
    # Signal daemon to switch — daemon reads this after tool execution
    import os
    os.environ["HUGINN_SWITCH_PROFILE"] = profile
    return f"Switched to {label}."


async def _switch_theme(theme: str) -> str:
    result = await _run_command(
        f"uv run --project ~/dotfiles/huginn "
        f"python3 ~/dotfiles/huginn/backend/theme.py {theme}"
    )
    return result


async def _weather(location: str = "") -> str:
    loc = location.strip().replace(" ", "+") or ""
    url = f"https://wttr.in/{loc}?format=4"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers={"User-Agent": "curl/7.0"})
            return resp.text.strip()
    except Exception as e:
        return f"Weather unavailable: {e}"


_RSS_FEEDS = {
    "phoronix":    "https://www.phoronix.com/rss.php",
    "hackernews":  "https://news.ycombinator.com/rss",
    "arstechnica": "https://feeds.arstechnica.com/arstechnica/technology-lab",
}

async def _remind(message: str, minutes: float) -> str:
    import shlex as _shlex
    seconds = max(5, minutes * 60)

    async def _fire():
        await asyncio.sleep(seconds)
        title = "ᚱ Huginn — Reminder"
        try:
            subprocess.run(["huginn-notify", "--type", "info", "--title", title, "--body", message], timeout=5)
        except Exception:
            pass
        # Speak via voice engine if available
        try:
            from voice import VoiceEngine
            await VoiceEngine().speak(message)
        except Exception:
            pass

    asyncio.create_task(_fire())
    mins_str = f"{minutes:g} minute{'s' if minutes != 1 else ''}"
    return f"Reminder set for {mins_str} from now: {message}"


def _alert_history(limit: int = 10) -> str:
    from memory import load_alerts
    alerts = load_alerts(limit)
    if not alerts:
        return "No system alerts on record."
    lines = []
    for a in alerts:
        val = f" ({a['value']})" if a["value"] is not None else ""
        lines.append(f"[{a['created_at']}] {a['key']}{val}: {a['message']}")
    return "\n".join(lines)


async def _news(source: str = "all", count: int = 8) -> str:
    import xml.etree.ElementTree as ET
    count = min(count, 20)
    feeds = list(_RSS_FEEDS.items()) if source == "all" else [(source, _RSS_FEEDS[source])]
    per_feed = max(1, count // len(feeds))
    lines: list[str] = []

    async with httpx.AsyncClient(timeout=15) as client:
        for name, url in feeds:
            try:
                resp = await client.get(url, headers={"User-Agent": "curl/7.0"}, follow_redirects=True)
                root = ET.fromstring(resp.text)
                items = root.findall(".//item")[:per_feed]
                for item in items:
                    title = (item.findtext("title") or "").strip()
                    link  = (item.findtext("link")  or "").strip()
                    lines.append(f"[{name}] {title}\n  {link}")
            except Exception as e:
                lines.append(f"[{name}] fetch error: {e}")

    return "\n\n".join(lines) if lines else "No news fetched."


# ── Calendar (Radicale CalDAV) ────────────────────────────────────────────────

def _caldav_client():
    import caldav
    return caldav.DAVClient(
        url=Config.caldav_url,
        username=Config.caldav_user,
        password=Config.caldav_password,
    )


async def _calendar_list(days: int = 7) -> str:
    import datetime, caldav
    from zoneinfo import ZoneInfo
    loop = asyncio.get_event_loop()

    def _fetch():
        client = _caldav_client()
        cal = client.calendar(url=Config.caldav_url)
        now   = datetime.datetime.now(tz=datetime.timezone.utc)
        end   = now + datetime.timedelta(days=days)
        events = cal.search(start=now, end=end, event=True, expand=True)
        lines = []
        for ev in events:
            c = ev.icalendar_component
            uid     = str(c.get("UID", ""))
            summary = str(c.get("SUMMARY", "(no title)"))
            dtstart = c.get("DTSTART")
            dtend   = c.get("DTEND")
            loc     = str(c.get("LOCATION", ""))
            desc    = str(c.get("DESCRIPTION", ""))
            start_s = dtstart.dt.isoformat() if dtstart else "?"
            end_s   = dtend.dt.isoformat()   if dtend   else "?"
            line = f"UID: {uid}\n  {summary}\n  {start_s} → {end_s}"
            if loc:  line += f"\n  📍 {loc}"
            if desc: line += f"\n  {desc[:120]}"
            lines.append(line)
        return "\n\n".join(lines) if lines else f"No events in the next {days} days."

    return await loop.run_in_executor(None, _fetch)


async def _calendar_add(title: str, start: str, end: str, description: str = "", location: str = "") -> str:
    import datetime, uuid, caldav
    from icalendar import Calendar, Event

    def _add():
        client = _caldav_client()
        cal    = client.calendar(url=Config.caldav_url)
        ical   = Calendar()
        ical.add("prodid", "-//Huginn//EN")
        ical.add("version", "2.0")
        ev = Event()
        ev.add("uid",     str(uuid.uuid4()))
        ev.add("summary", title)
        ev.add("dtstart", _parse_dt(start))
        ev.add("dtend",   _parse_dt(end))
        if description: ev.add("description", description)
        if location:    ev.add("location",    location)
        ev.add("dtstamp", datetime.datetime.now(tz=datetime.timezone.utc))
        ical.add_component(ev)
        cal.add_event(ical.to_ical().decode())
        return f"Event added: '{title}' on {start}"

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _add)


async def _calendar_update(uid: str, title: str | None, start: str | None, end: str | None,
                            description: str | None, location: str | None) -> str:
    import caldav

    def _update():
        client = _caldav_client()
        cal    = client.calendar(url=Config.caldav_url)
        results = cal.search(event=True)
        for ev in results:
            c = ev.icalendar_component
            if str(c.get("UID", "")) == uid:
                if title:       c["SUMMARY"]     = title
                if start:       c["DTSTART"].dt  = _parse_dt(start)
                if end:         c["DTEND"].dt     = _parse_dt(end)
                if description is not None: c["DESCRIPTION"] = description
                if location    is not None: c["LOCATION"]    = location
                ev.save()
                return f"Event {uid} updated."
        return f"Event {uid} not found."

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _update)


async def _calendar_delete(uid: str) -> str:
    import caldav

    def _delete():
        client = _caldav_client()
        cal    = client.calendar(url=Config.caldav_url)
        results = cal.search(event=True)
        for ev in results:
            if str(ev.icalendar_component.get("UID", "")) == uid:
                ev.delete()
                return f"Event {uid} deleted."
        return f"Event {uid} not found."

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _delete)


def _parse_dt(s: str):
    import datetime
    from zoneinfo import ZoneInfo
    s = s.strip()
    if "T" in s:
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)
        return dt
    return datetime.date.fromisoformat(s)
