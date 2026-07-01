"""
Model router + streaming for Huginn v2.
Auto-routes to fast/full/cloud based on query complexity.
Ollama exclusive lock prevents VRAM collisions with garage-watch.
"""
import asyncio
import contextlib
import fcntl
import json
import re
from pathlib import Path
from typing import AsyncIterator

import httpx

from config import MODELS, OLLAMA_BASE, _OLLAMA_LOCK_PATH, GAME_MODE_FLAG, SYSTEM_PROMPT

# ── Ollama exclusive lock (shared with garage-watch) ──────────────────────────

@contextlib.asynccontextmanager
async def ollama_lock():
    f = open(_OLLAMA_LOCK_PATH, "w")
    await asyncio.to_thread(fcntl.flock, f.fileno(), fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def is_game_mode() -> bool:
    return Path(GAME_MODE_FLAG).exists()


# ── Model routing ─────────────────────────────────────────────────────────────

_COMPLEX_RE = re.compile(
    r'\b(write|create|implement|refactor|analyze|compare|explain in detail|'
    r'debug|review|architecture|design|plan|why does|how does)\b',
    re.I,
)

def route_model(content: str, has_image: bool = False) -> str:
    if has_image:
        return "vision"
    if is_game_mode():
        return "cloud"
    words = content.split()
    if len(words) > 60 or _COMPLEX_RE.search(content):
        return "full"
    return "fast"


# ── Check which ollama models are available ───────────────────────────────────

async def available_ollama_models() -> set[str]:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{OLLAMA_BASE}/api/tags")
            data = r.json()
            return {m["name"].split(":")[0] for m in data.get("models", [])}
    except Exception:
        return set()


# ── Streaming ─────────────────────────────────────────────────────────────────

async def stream_ollama(
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
) -> AsyncIterator[dict]:
    """Yields dicts: {type: token|tool_call|thinking|done, ...}"""
    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": True,
        "options": {"num_ctx": 8192},
    }
    if tools:
        payload["tools"] = tools

    async with ollama_lock():
        async with httpx.AsyncClient(timeout=180) as client:
            async with client.stream(
                "POST", f"{OLLAMA_BASE}/api/chat", json=payload
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg = chunk.get("message", {})
                    role = msg.get("role", "")

                    # thinking tokens
                    thinking = msg.get("thinking", "")
                    if thinking:
                        yield {"type": "thinking", "content": thinking}

                    # regular content
                    content = msg.get("content", "")
                    if content:
                        yield {"type": "token", "content": content}

                    # tool calls
                    for tc in msg.get("tool_calls", []):
                        fn = tc.get("function", {})
                        yield {
                            "type": "tool_call",
                            "tool": fn.get("name", ""),
                            "args": fn.get("arguments", {}),
                        }

                    if chunk.get("done"):
                        yield {"type": "done"}
                        return


async def stream_claude(
    messages: list[dict],
    tools: list[dict] | None = None,
) -> AsyncIterator[dict]:
    """Yields same event dicts as stream_ollama."""
    import anthropic

    model_cfg = MODELS["cloud"]
    client = anthropic.Anthropic()

    system = SYSTEM_PROMPT
    claude_messages = _to_claude_messages(messages)

    kwargs: dict = {
        "model": model_cfg["model"],
        "max_tokens": 4096,
        "system": system,
        "messages": claude_messages,
    }
    if tools:
        kwargs["tools"] = _to_claude_tools(tools)

    with client.messages.stream(**kwargs) as stream:
        for event in stream:
            if hasattr(event, "type"):
                if event.type == "content_block_delta":
                    delta = event.delta
                    if hasattr(delta, "text"):
                        yield {"type": "token", "content": delta.text}
                    elif hasattr(delta, "partial_json"):
                        pass  # tool input streaming handled at stop
                elif event.type == "message_stop":
                    final = stream.get_final_message()
                    for block in final.content:
                        if block.type == "tool_use":
                            yield {
                                "type": "tool_call",
                                "tool": block.name,
                                "args": block.input,
                                "call_id": block.id,
                            }
                    yield {"type": "done"}
                    return


def _to_claude_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "system":
            continue
        content = m["content"]
        if isinstance(content, list):
            out.append({"role": m["role"], "content": content})
        else:
            out.append({"role": m["role"], "content": str(content)})
    return out


def _to_claude_tools(tools: list[dict]) -> list[dict]:
    out = []
    for t in tools:
        fn = t.get("function", t)
        out.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return out


async def stream_chat(
    messages: list[dict],
    model_key: str,
    tools: list[dict] | None = None,
) -> AsyncIterator[dict]:
    cfg = MODELS[model_key]
    if cfg["backend"] == "ollama":
        async for ev in stream_ollama(cfg["model"], messages, tools):
            yield ev
    else:
        async for ev in stream_claude(messages, tools):
            yield ev
