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

from config import (
    GAME_MODE_FLAG, GATE_JUDGE_TIMEOUT_SECONDS, MODELS, OLLAMA_BASE,
    SYSTEM_PROMPT, _OLLAMA_LOCK_PATH,
)

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


async def judge_once(
    prompt: str,
    image_paths: list[str] | None = None,
    prefer: str = "cloud",
) -> str:
    """One-shot, non-streaming completion with optional image attachments.
    Returns the raw text response. Tries `prefer` first, falls back to the
    local vision model on any failure."""
    image_paths = image_paths or []
    try:
        if prefer == "cloud":
            return await _judge_claude(prompt, image_paths)
        return await _judge_ollama(prompt, image_paths)
    except Exception:
        if prefer == "cloud":
            return await _judge_ollama(prompt, image_paths)
        raise


async def judge_local_only(prompt: str, image_paths: list[str] | None = None) -> str:
    """Local-only judgment for data that must never leave the machine (e.g.
    gatekeeper screenshots and activity history). Unlike judge_once, there is
    no `prefer` argument and no code path to _judge_claude anywhere in this
    function — the cloud model is structurally unreachable from here, not
    just unselected by a default."""
    return await _judge_ollama(prompt, image_paths or [])


async def _judge_claude(prompt: str, image_paths: list[str]) -> str:
    import base64
    import anthropic

    content: list[dict] = []
    for p in image_paths:
        data = await asyncio.to_thread(Path(p).read_bytes)
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(data).decode(),
            },
        })
    content.append({"type": "text", "text": prompt})

    client = anthropic.Anthropic()
    resp = await asyncio.to_thread(
        client.messages.create,
        model=MODELS["cloud"]["model"],
        max_tokens=512,
        messages=[{"role": "user", "content": content}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


async def _judge_ollama(prompt: str, image_paths: list[str]) -> str:
    import base64

    images_b64 = [
        base64.b64encode(await asyncio.to_thread(Path(p).read_bytes)).decode()
        for p in image_paths
    ]
    message: dict = {"role": "user", "content": prompt}
    if images_b64:
        message["images"] = images_b64

    payload = {
        "model": MODELS["vision"]["model"],
        "messages": [message],
        "stream": False,
    }
    async with ollama_lock():
        async with httpx.AsyncClient(timeout=GATE_JUDGE_TIMEOUT_SECONDS) as client:
            r = await client.post(f"{OLLAMA_BASE}/api/chat", json=payload)
            r.raise_for_status()
            return r.json().get("message", {}).get("content", "")


async def unload_model(model: str) -> None:
    """Best-effort immediate unload (keep_alive=0). Fire-and-forget — a
    failure here just means the model stays resident a bit longer, which is
    the same as if this were never called. Does not take ollama_lock(): it's
    used from gatekeeper's game-mode short-circuit specifically so it can't
    itself get stuck behind a slow judgment holding the lock."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(f"{OLLAMA_BASE}/api/generate", json={"model": model, "keep_alive": 0})
    except Exception:
        pass


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
