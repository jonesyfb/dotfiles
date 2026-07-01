"""
Huginn v2 tool definitions and implementations.
Trust tiers: "auto" runs immediately, "confirm" asks the user first.
"""
import asyncio
import json
import subprocess
from pathlib import Path

import httpx

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
    "search_memory": "auto",
    "queue_task":    "confirm",
    "claude_code":   "confirm",
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
            "description": "Get current conditions plus a 3-day forecast (high/low/description per day).",
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
            "name": "queue_task",
            "description": (
                "Queue a shell command to run in the background task queue. "
                "The task runs asynchronously; Huginn notifies when it completes. "
                "Use for long-running jobs: builds, downloads, backups, etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Short human-readable name for the task"},
                    "command": {"type": "string", "description": "Shell command to run"},
                },
                "required": ["label", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": (
                "Semantic search over stored memories and facts. "
                "Finds past remembered items similar in meaning to the query, "
                "even if the exact words don't match."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural language search query"},
                    "limit": {"type": "integer", "description": "Max results (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "claude_code",
            "description": (
                "Spawn a Claude Code session to complete a coding or engineering task autonomously. "
                "Claude Code can read and write files, run shell commands, and make multi-step changes. "
                "Use for tasks too complex for a single shell command: refactors, new features, "
                "debugging sessions, writing tests, or anything requiring multiple files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed task description for Claude Code",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (default: ~/dotfiles)",
                    },
                },
                "required": ["prompt"],
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
            case "claude_code":
                return await _claude_code(args["prompt"], args.get("cwd", ""))
            case "remember":
                set_fact(args["key"], args["value"])
                asyncio.ensure_future(
                    _embed_and_store(f"{args['key']}: {args['value']}", "fact")
                )
                return f"remembered: {args['key']}"
            case "search_memory":
                return await _search_memory(args["query"], args.get("limit", 5))
            case "queue_task":
                return _queue_task(args["label"], args["command"])
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
    proc = await asyncio.create_subprocess_shell(
        "TZ=America/Chicago date +'TIME:%H:%M %Z'; "
        "echo CPU:$(grep -m1 cpu /proc/stat | awk '{u=$2+$4; t=$2+$3+$4+$5+$6+$7; print int(100*u/t)\"%\"}'); "
        "echo MEM:$(free -h | awk '/Mem:/{print $3\"/\"$2}'); "
        "echo DISK:$(df -h / | awk 'NR==2{print $3\"/\"$2\" (\"$5\")\"}'); "
        "echo UPTIME:$(uptime -p); "
        "f=$(ls /sys/class/drm/card*/device/gpu_busy_percent 2>/dev/null | head -1); "
        "[ -f \"$f\" ] && echo GPU:$(cat \"$f\")% || true",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    return out.decode().strip()


async def _web_search(query: str, max_results: int) -> str:
    for attempt in range(2):
        try:
            from ddgs import DDGS
            results = await asyncio.to_thread(
                lambda: list(DDGS().text(query, max_results=max_results))
            )
            if results:
                lines = []
                for r in results:
                    lines.append(f"• {r.get('title', '')}\n  {r.get('href', '')}\n  {r.get('body', '')[:200]}")
                return "\n\n".join(lines)
        except Exception as e:
            if attempt == 1:
                return f"search error: {e}"
            await asyncio.sleep(1)
    return "no results"


async def _get_weather(location: str) -> str:
    from config import WEATHER_LOCATION
    import datetime
    loc = location or WEATHER_LOCATION
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"https://wttr.in/{loc.replace(' ', '+')}?format=j1",
                headers={"Accept": "application/json"},
            )
            data = r.json()

        cur = data["current_condition"][0]
        current = (
            f"Now: {cur['temp_F']}°F, feels {cur['FeelsLikeF']}°F, "
            f"{cur['weatherDesc'][0]['value']}, "
            f"humidity {cur['humidity']}%, wind {cur['windspeedMiles']}mph"
        )

        days = []
        for w in data.get("weather", []):
            date = datetime.datetime.strptime(w["date"], "%Y-%m-%d")
            label = date.strftime("%a %b %-d")
            hi, lo = w["maxtempF"], w["mintempF"]
            desc = w["hourly"][4]["weatherDesc"][0]["value"]  # midday
            days.append(f"  {label}: {lo}–{hi}°F, {desc}")

        return current + "\n\nForecast:\n" + "\n".join(days)
    except Exception as e:
        return f"weather error: {e}"


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


def _queue_task(label: str, command: str) -> str:
    import uuid
    from memory import enqueue_task
    task_id = str(uuid.uuid4())[:8]
    enqueue_task(task_id, label, command)
    return f"queued: {label} (id {task_id})"


async def _get_embedding(text: str) -> list[float]:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            "http://localhost:11434/api/embed",
            json={"model": "nomic-embed-text", "input": text},
        )
        return r.json()["embeddings"][0]


async def _embed_and_store(text: str, source: str) -> None:
    try:
        from memory import store_vec
        vec = await _get_embedding(text)
        store_vec(text, source, vec)
    except Exception:
        pass


async def _search_memory(query: str, limit: int = 5) -> str:
    try:
        from memory import semantic_search
        vec = await _get_embedding(query)
        results = semantic_search(vec, limit)
        if not results:
            return "no similar memories found"
        lines = [f"[{r['source']}] {r['text']}" for r in results]
        return "\n".join(lines)
    except Exception as e:
        return f"search error: {e}"


async def _claude_code(prompt: str, cwd: str = "") -> str:
    import os
    work_dir = Path(cwd).expanduser() if cwd else Path.home() / "dotfiles"
    if not work_dir.exists():
        work_dir = Path.home()

    proc = await asyncio.create_subprocess_exec(
        "claude", "--print", "--dangerously-skip-permissions",
        "--output-format", "text",
        prompt,
        cwd=str(work_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ},
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=300)
        result = out.decode(errors="replace").strip()
        if not result:
            result = err.decode(errors="replace").strip()[:500] or "no output"
        # Surface the tail — final answer is at the end of verbose output
        return result[-3000:] if len(result) > 3000 else result
    except asyncio.TimeoutError:
        proc.kill()
        return "timeout after 5 minutes"


async def _notify(title: str, body: str, notif_type: str = "info") -> str:
    proc = await asyncio.create_subprocess_exec(
        "huginn-notify", "--type", notif_type, "--title", title, "--body", body,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return "sent"
