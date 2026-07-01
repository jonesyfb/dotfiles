"""
Huginn v2 tool definitions and implementations.
Trust tiers: "auto" runs immediately, "confirm" asks the user first.
"""
import asyncio
import json
import subprocess
from pathlib import Path

from memory import set_fact, all_facts, delete_fact

# ── Trust tiers ───────────────────────────────────────────────────────────────

TOOL_TRUST: dict[str, str] = {
    "shell":         "confirm",
    "read_file":     "auto",
    "write_file":    "confirm",
    "system_stats":  "auto",
    "web_search":    "auto",
    "get_weather":   "auto",
    "calendar_list": "auto",
    "notify":        "auto",
    "remember":      "auto",
    "recall":        "auto",
    "forget":        "auto",
}

# Prefixes that are always safe to run without confirmation
_SAFE_PREFIXES = (
    "cat ", "ls ", "df ", "du ", "free", "uname", "uptime", "ps ",
    "systemctl status", "systemctl --user status",
    "pacman -Q", "pacman -Si",
    "git status", "git log", "git diff",
    "pactl ", "wpctl get", "playerctl ",
    "nvidia-smi", "amdgpu_top", "radeontop",
    "ip addr", "ip link", "ss -",
    "journalctl", "dmesg",
    "find ", "grep ", "which ", "type ",
    "echo ", "date", "cal",
    "pgrep", "pidof",
)

def shell_is_safe(cmd: str) -> bool:
    stripped = cmd.strip()
    return any(stripped.startswith(p) for p in _SAFE_PREFIXES)


# ── Tool definitions (OpenAI function calling schema) ─────────────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": (
                "Run a shell command on the user's Arch Linux desktop. "
                "Safe read-only commands run immediately. "
                "Anything that writes, installs, or modifies state requires approval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the filesystem. Returns its contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "lines": {"type": "integer", "description": "Max lines to return (default 200)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file. Always requires confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_stats",
            "description": "Get current CPU, memory, disk, uptime, and GPU usage.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web with DuckDuckGo. Returns top results with snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather and forecast.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City, State (default: Joplin, MO)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calendar_list",
            "description": "List upcoming calendar events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Days to look ahead (default 7)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notify",
            "description": "Send a desktop notification to the user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["title", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a fact about the user's system or preferences to persistent memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Short snake_case key"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "Recall all stored facts from persistent memory.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget",
            "description": "Delete a fact from persistent memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                },
                "required": ["key"],
            },
        },
    },
]


# ── Implementations ───────────────────────────────────────────────────────────

async def run_tool(name: str, args: dict) -> str:
    try:
        match name:
            case "shell":
                return await _shell(args["command"])
            case "read_file":
                return await _read_file(args["path"], args.get("lines", 200))
            case "write_file":
                return await _write_file(args["path"], args["content"])
            case "system_stats":
                return await _system_stats()
            case "web_search":
                return await _web_search(args["query"], args.get("max_results", 5))
            case "get_weather":
                return await _get_weather(args.get("location", ""))
            case "calendar_list":
                return await _calendar_list(args.get("days", 7))
            case "notify":
                return await _notify(args["title"], args["body"])
            case "remember":
                set_fact(args["key"], args["value"])
                return f"remembered: {args['key']}"
            case "recall":
                facts = all_facts()
                return json.dumps(facts, indent=2) if facts else "no facts stored"
            case "forget":
                delete_fact(args["key"])
                return f"forgotten: {args['key']}"
            case _:
                return f"unknown tool: {name}"
    except Exception as e:
        return f"error: {e}"


async def _shell(cmd: str) -> str:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    return out.decode(errors="replace").strip()[:4000]


async def _read_file(path: str, max_lines: int) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return f"file not found: {path}"
    text = p.read_text(errors="replace")
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        return "\n".join(lines) + f"\n[truncated — {len(lines)}/{len(text.splitlines())} lines]"
    return "\n".join(lines)


async def _write_file(path: str, content: str) -> str:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"written: {path}"


async def _system_stats() -> str:
    tasks = [
        asyncio.create_subprocess_shell(
            "echo CPU:$(grep -m1 cpu /proc/stat | awk '{u=$2+$4; t=$2+$3+$4+$5+$6+$7; print int(100*u/t)\"%\"}'); "
            "echo MEM:$(free -h | awk '/Mem:/{print $3\"/\"$2}'); "
            "echo DISK:$(df -h / | awk 'NR==2{print $3\"/\"$2\" (\"$5\")\"}'); "
            "echo UPTIME:$(uptime -p); "
            "f=$(ls /sys/class/drm/card*/device/gpu_busy_percent 2>/dev/null | head -1); "
            "[ -f \"$f\" ] && echo GPU:$(cat \"$f\")% || true",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    ]
    proc = (await asyncio.gather(*tasks))[0]
    out, _ = await proc.communicate()
    return out.decode().strip()


async def _web_search(query: str, max_results: int) -> str:
    try:
        from duckduckgo_search import DDGS
        results = await asyncio.to_thread(
            lambda: list(DDGS().text(query, max_results=max_results))
        )
        lines = []
        for r in results:
            lines.append(f"• {r.get('title', '')}\n  {r.get('href', '')}\n  {r.get('body', '')[:200]}")
        return "\n\n".join(lines) or "no results"
    except Exception as e:
        return f"search error: {e}"


async def _get_weather(location: str) -> str:
    from config import WEATHER_LOCATION
    loc = location or WEATHER_LOCATION
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://wttr.in/{loc.replace(' ', '+')}?format=4")
            return r.text.strip()
    except Exception as e:
        return f"weather error: {e}"

try:
    import httpx
except ImportError:
    pass


async def _calendar_list(days: int) -> str:
    try:
        import caldav
        from datetime import datetime, timedelta
        from config import CALDAV_URL, CALDAV_USER, CALDAV_PASSWORD

        def _fetch():
            client = caldav.DAVClient(
                url=CALDAV_URL, username=CALDAV_USER, password=CALDAV_PASSWORD
            )
            calendar = client.calendar(url=CALDAV_URL)
            start = datetime.now()
            end = start + timedelta(days=days)
            events = calendar.search(start=start, end=end, event=True, expand=True)
            lines = []
            for e in sorted(events, key=lambda x: x.vobject_instance.vevent.dtstart.value):
                v = e.vobject_instance.vevent
                dt = v.dtstart.value
                dt_str = dt.strftime("%a %b %d %H:%M") if hasattr(dt, "strftime") else str(dt)
                lines.append(f"• {dt_str} — {v.summary.value}")
            return "\n".join(lines) or "no upcoming events"

        return await asyncio.to_thread(_fetch)
    except Exception as e:
        return f"calendar error: {e}"


async def _notify(title: str, body: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "notify-send", title, body, "-a", "Huginn",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return "sent"
