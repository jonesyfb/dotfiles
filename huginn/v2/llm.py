"""
Model router + streaming for Huginn v2.
Auto-routes to fast/full/cloud based on query complexity.

GPU-touching calls (stream_ollama, _judge_ollama, _unload_model_raw) no
longer take a lock themselves — they're raw primitives now. The
coordinator (coordinator.py) owns scheduling and the cross-process flock;
see stream_chat()/judge_local_only()/unload_model() below, which are the
functions callers should actually use.
"""
import asyncio
import json
import re
from pathlib import Path
from typing import AsyncIterator

import httpx

from config import (
    GAME_MODE_FLAG, GATE_JUDGE_TIMEOUT_SECONDS, MODELS, OLLAMA_BASE,
    PERSONALITY_RENDER_TIMEOUT_SECONDS, SYSTEM_PROMPT,
)
from coordinator import Denial, InferenceRequest, Purpose, RequestClass, coordinator


class CoordinatorDenied(Exception):
    """Raised by the coordinator-routed helpers below when a request was
    denied (game mode, deadline, queue full, preempted) rather than run."""

    def __init__(self, denial: Denial, detail: str):
        super().__init__(f"{denial.value}: {detail}")
        self.denial = denial
        self.detail = detail


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
    local vision model on any failure. Does NOT go through the coordinator —
    nothing currently calls this (judge_local_only is what gatekeeper uses);
    kept only so an eventual non-local-only judge use case doesn't need to
    reinvent the cloud/local fallback."""
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
    gatekeeper screenshots and activity history), scheduled through the
    coordinator (LOCAL_VISION_GATEKEEPER / GATE_DECISION). No `prefer`
    argument and no code path to _judge_claude anywhere in this function —
    the cloud model is structurally unreachable from here, not just
    unselected by a default. Raises on denial (deadline/game-mode/queue-full/
    preempted) so callers keep their existing except-and-fail-closed shape."""
    from config import GATE_QUEUE_DEADLINE_SECONDS

    async def _fn(emit):
        return await _judge_ollama(prompt, image_paths or [])

    request = InferenceRequest(
        request_class=RequestClass.LOCAL_VISION_GATEKEEPER,
        purpose=Purpose.GATE_DECISION,
        model=MODELS["vision"]["model"],
        fn=_fn,
        deadline_seconds=GATE_QUEUE_DEADLINE_SECONDS,
        label="gatekeeper-judge",
    )
    async for event in coordinator.submit(request):
        if event.kind == "done":
            return event.value
        if event.kind == "denied":
            raise CoordinatorDenied(event.denial, event.detail)
    raise CoordinatorDenied(Denial.ERROR, "coordinator produced no terminal event")


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
    # No lock here — the coordinator holds the shared flock around this call
    # (see judge_local_only above and coordinator._run_item). This function
    # is a raw primitive now, not a public entry point.
    async with httpx.AsyncClient(timeout=GATE_JUDGE_TIMEOUT_SECONDS) as client:
        r = await client.post(f"{OLLAMA_BASE}/api/chat", json=payload)
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "")


async def _unload_model_raw(model: str) -> None:
    """Best-effort immediate unload (keep_alive=0). Raw primitive — no lock,
    no coordinator routing. Use unload_model() below instead; this exists
    only so the coordinator's own execution slot can call it while already
    holding the flock, without recursively going through submit()."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(f"{OLLAMA_BASE}/api/generate", json={"model": model, "keep_alive": 0})
    except Exception:
        pass


async def unload_model(model: str) -> None:
    """Coordinator-routed unload. Re-checks actual residency immediately
    before unloading (state may have changed while queued) — see
    coordinator._run_item, which re-runs admission checks right before
    execution, and the closure below, which re-probes /api/ps at that exact
    moment rather than trusting whatever the caller observed earlier."""
    async def _fn(emit):
        import context
        loaded = await context.probe_ollama_loaded()
        if loaded is None or any(m.get("model") == model for m in loaded):
            # Unknown state or confirmed still resident — either way, an
            # unload attempt is harmless (keep_alive=0 on an already-unloaded
            # model is a no-op), so proceed rather than risk skipping a real
            # unload because the probe itself failed.
            await _unload_model_raw(model)
        return None

    request = InferenceRequest(
        request_class=RequestClass.MODEL_LOAD_UNLOAD,
        purpose=Purpose.MAINTENANCE,
        model=model,
        fn=_fn,
        deadline_seconds=10.0,
        label="unload-if-resident",
    )
    async for event in coordinator.submit(request):
        if event.kind in ("done", "denied"):
            return


async def _render_personality_raw(system_prompt: str, user_prompt: str) -> str:
    """Raw primitive — no lock, no coordinator routing, no cloud path
    anywhere in this function. Use render_personality_only() instead."""
    payload = {
        "model": MODELS["personality"]["model"],
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "options": {"temperature": 0.7},
    }
    async with httpx.AsyncClient(timeout=PERSONALITY_RENDER_TIMEOUT_SECONDS) as client:
        r = await client.post(f"{OLLAMA_BASE}/api/chat", json=payload)
        r.raise_for_status()
        return r.json().get("message", {}).get("content", "")


async def render_personality_only(
    system_prompt: str,
    user_prompt: str,
    purpose: Purpose = Purpose.AMBIENT,
    deadline_seconds: "float | None" = None,
) -> str:
    """Structurally cloud-isolated, like judge_local_only — no `prefer`
    argument and no code path to _judge_claude/stream_claude anywhere in
    this function. Scheduled through the coordinator as
    RequestClass.RESIDENT_PERSONALITY, which is always game-mode-admitted
    (unlike ordinary/vision/maintenance classes) since the personality
    model is the one thing game mode still permits. Raises CoordinatorDenied
    on denial so callers keep the same except-and-handle shape used
    elsewhere (judge_local_only, unload_model)."""
    async def _fn(emit):
        return await _render_personality_raw(system_prompt, user_prompt)

    request = InferenceRequest(
        request_class=RequestClass.RESIDENT_PERSONALITY,
        purpose=purpose,
        model=MODELS["personality"]["model"],
        fn=_fn,
        deadline_seconds=deadline_seconds,
        label="personality-render",
    )
    async for event in coordinator.submit(request):
        if event.kind == "done":
            return event.value
        if event.kind == "denied":
            raise CoordinatorDenied(event.denial, event.detail)
    raise CoordinatorDenied(Denial.ERROR, "coordinator produced no terminal event")


async def stream_chat(
    messages: list[dict],
    model_key: str,
    tools: list[dict] | None = None,
    purpose: Purpose = Purpose.DIRECT,
) -> AsyncIterator[dict]:
    """purpose defaults to DIRECT (a human is waiting) — callers doing
    background work (random_chime_worker) must pass Purpose.AMBIENT
    explicitly. Cloud backend bypasses the coordinator entirely: it's a
    separate resource with no local GPU contention to schedule around."""
    cfg = MODELS[model_key]
    if cfg["backend"] != "ollama":
        async for ev in stream_claude(messages, tools):
            yield ev
        return

    request_class = (
        RequestClass.RESIDENT_PERSONALITY if model_key == "personality"
        else RequestClass.ORDINARY_LOCAL_REASONING
    )

    async def _fn(emit):
        async for ev in stream_ollama(cfg["model"], messages, tools):
            await emit(ev)
        return None

    request = InferenceRequest(
        request_class=request_class,
        purpose=purpose,
        model=cfg["model"],
        fn=_fn,
        label="chat",
    )
    async for event in coordinator.submit(request):
        if event.kind == "chunk":
            yield event.value
        elif event.kind == "denied":
            yield {"type": "token", "content": f"[inference unavailable: {event.detail}]"}
            yield {"type": "done"}
            return
