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
            "description": "Read the contents of a file. Use ~ for home directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to read"}
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
]


# ── Tool implementations ──────────────────────────────────────────────────────

async def execute_tool(name: str, args: dict) -> str:
    try:
        match name:
            case "run_command":    return await _run_command(args["command"])
            case "read_file":      return await _read_file(args["path"])
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
            case "switch_model":   return await _switch_model(args["profile"])
            case "switch_theme":   return await _switch_theme(args["theme"])
            case _:                return f"Unknown tool: {name}"
    except KeyError as e:
        return f"Missing required argument: {e}"
    except Exception as e:
        return f"Tool error: {e}"


async def _run_command(command: str) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        result = output.decode(errors="replace").strip()
        return result[:3000] if result else "(no output)"
    except asyncio.TimeoutError:
        return "Command timed out after 30s"


async def _read_file(path: str) -> str:
    try:
        content = Path(path).expanduser().read_text(errors="replace")
        return content[:5000] if len(content) > 5000 else content
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
    try:
        subprocess.run(["notify-send", "-u", urgency, title, message], timeout=5)
        return f"Notification sent: {title}"
    except FileNotFoundError:
        return "notify-send not found — install libnotify"


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
