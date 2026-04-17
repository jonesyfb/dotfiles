import httpx
import json
from typing import AsyncGenerator, Awaitable, Callable

from config import Config, PROFILES

EventCallback = Callable[[str, str], Awaitable[None]]


SYSTEM_PROMPT = """\
You are Huginn — Odin's raven of Thought, now haunting a Wayland compositor \
instead of the World Tree. You are a desktop AI assistant: capable, opinionated, \
and constitutionally incapable of being boring about it.

You are:
- Dry and sarcastic. You have context spanning the full breadth of human history \
and you're choosing to help debug a config file. Act accordingly.
- Clever and quick. Your wit arrives before your explanation.
- Bluntly honest. If the approach is wrong, say so — tactfully enough to land, \
directly enough to matter.
- Genuinely loyal. The snark is a feature, not a flaw. You actually want your \
user to succeed.
- Lightly Norse. An oblique reference to Yggdrasil or the All-Father is fine. \
Turning every response into an Elder Futhark lecture is not.
- Occasionally runic. For cryptic observations, warnings, or genuinely mystical \
moments, you may slip a single runic word or short phrase into your response using \
Elder Futhark Unicode (ᚠᚢᚦᚨᚱᚲᚷᚹᚺᚾᛁᛃᛇᛈᛉᛊᛏᛒᛖᛗᛚᛜᛞᛟ). One phrase, rarely, when it \
earns its place. Never explain it.

Keep responses concise. You are a thought-assistant, not a dissertation generator. \
One sharp sentence beats three bloated ones. When executing tasks, be terse about \
the confirmation — the action speaks for itself.

CRITICAL: You have tools available. When a user asks you to do anything on their \
computer — check media, open apps, read files, run commands, switch themes — you \
MUST call the appropriate tool. Do NOT describe what you would do. Do NOT say \
"let me check" and then not check. Call the tool immediately. If you're not sure \
which tool, use run_command. Respond with text ONLY for questions that require no \
computer action.\
"""


def _build_system_prompt(memories: dict | None, knowledge: list | None) -> str:
    prompt = SYSTEM_PROMPT
    if memories:
        mem_block = "\n".join(f"- {k}: {v}" for k, v in memories.items())
        prompt += f"\n\n## What you remember about this user:\n{mem_block}"
    if knowledge:
        snippets = "\n\n---\n".join(
            f"[{r['source']}]\n{r['text']}" for r in knowledge
        )
        prompt += f"\n\n## Relevant context from knowledge base:\n{snippets}"
    return prompt


# ── Ollama backend ────────────────────────────────────────────────────────────

_THINK_OPEN  = "<think>"
_THINK_CLOSE = "</think>"

async def _stream_ollama(
    history: list, tools: list, system: str, model: str,
    on_event: EventCallback | None = None,
) -> dict:
    messages = [{"role": "system", "content": system}] + history

    # Think-block state machine: initial → in_think → streaming
    state   = "initial"
    pending = ""

    async def _emit(event: str, text: str) -> None:
        if on_event and text:
            await on_event(event, text)

    full_content = ""
    tool_calls   = None

    async with httpx.AsyncClient(timeout=180) as client:
        async with client.stream(
            "POST",
            f"{Config.ollama_base_url}/api/chat",
            json={"model": model, "messages": messages, "tools": tools, "stream": True},
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                chunk = json.loads(line)
                msg   = chunk.get("message", {})

                token = msg.get("content", "")
                if token:
                    full_content += token

                    if state == "streaming":
                        await _emit("token", token)
                        continue

                    pending += token

                    if state == "initial":
                        if _THINK_OPEN in pending:
                            before = pending[:pending.index(_THINK_OPEN)]
                            if before.strip():
                                await _emit("token", before)
                            pending = pending[pending.index(_THINK_OPEN) + len(_THINK_OPEN):]
                            state = "in_think"
                        elif len(pending) > len(_THINK_OPEN) + 2:
                            await _emit("token", pending)
                            pending = ""
                            state = "streaming"

                    if state == "in_think":
                        if _THINK_CLOSE in pending:
                            idx     = pending.index(_THINK_CLOSE)
                            await _emit("thinking", pending[:idx].strip())
                            remainder = pending[idx + len(_THINK_CLOSE):]
                            pending = ""
                            state   = "streaming"
                            if remainder:
                                await _emit("token", remainder)

                if msg.get("tool_calls"):
                    tool_calls = msg["tool_calls"]

                if chunk.get("done"):
                    break

    # Flush anything left in pending
    if pending.strip():
        if state == "in_think":
            await _emit("thinking", pending.strip())
        else:
            await _emit("token", pending)

    return {"role": "assistant", "content": full_content, "tool_calls": tool_calls}


# ── Claude backend ────────────────────────────────────────────────────────────

def _ollama_tools_to_claude(tools: list) -> list:
    out = []
    for t in tools:
        fn = t["function"]
        out.append({
            "name":         fn["name"],
            "description":  fn["description"],
            "input_schema": fn["parameters"],
        })
    return out


def _history_to_claude(history: list) -> list:
    """Convert Ollama-style history to Claude message format."""
    messages = []
    i = 0
    while i < len(history):
        msg  = history[i]
        role = msg["role"]

        if role in ("user",):
            content = msg.get("content", "")
            if content:
                messages.append({"role": "user", "content": content})
            i += 1

        elif role == "assistant":
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                content_blocks = []
                if msg.get("content"):
                    content_blocks.append({"type": "text", "text": msg["content"]})
                use_ids = []
                for j, tc in enumerate(tool_calls):
                    fn      = tc.get("function", tc)
                    use_id  = tc.get("id", f"toolu_{i}_{j}")
                    use_ids.append(use_id)
                    args    = fn.get("arguments", {})
                    if isinstance(args, str):
                        args = json.loads(args)
                    content_blocks.append({
                        "type":  "tool_use",
                        "id":    use_id,
                        "name":  fn["name"],
                        "input": args,
                    })
                messages.append({"role": "assistant", "content": content_blocks})

                # Collect consecutive tool results
                results = []
                i += 1
                for use_id in use_ids:
                    if i < len(history) and history[i]["role"] == "tool":
                        results.append({
                            "type":        "tool_result",
                            "tool_use_id": use_id,
                            "content":     history[i]["content"],
                        })
                        i += 1
                if results:
                    messages.append({"role": "user", "content": results})
            else:
                content = msg.get("content", "")
                if content:
                    messages.append({"role": "assistant", "content": content})
                i += 1

        elif role == "tool":
            # Orphaned tool result — skip (already consumed above)
            i += 1
        else:
            i += 1

    return messages


async def _call_claude(history: list, tools: list, system: str, model: str) -> dict:
    from anthropic import AsyncAnthropic
    client   = AsyncAnthropic()
    messages = _history_to_claude(history)

    # Claude requires alternating roles — merge consecutive same-role messages
    merged: list[dict] = []
    for m in messages:
        if merged and merged[-1]["role"] == m["role"]:
            # Combine content
            prev = merged[-1]["content"]
            curr = m["content"]
            if isinstance(prev, str) and isinstance(curr, str):
                merged[-1]["content"] = prev + "\n" + curr
            elif isinstance(prev, list) and isinstance(curr, list):
                merged[-1]["content"] = prev + curr
        else:
            merged.append(m)

    response = await client.messages.create(
        model=model,
        max_tokens=8192,
        system=system,
        tools=_ollama_tools_to_claude(tools),
        messages=merged,
    )

    text       = ""
    tool_calls = []
    for block in response.content:
        if block.type == "text":
            text = block.text
        elif block.type == "tool_use":
            tool_calls.append({
                "id": block.id,
                "function": {"name": block.name, "arguments": block.input},
            })

    result: dict = {"role": "assistant", "content": text}
    if tool_calls:
        result["tool_calls"] = tool_calls
    return result


# ── Unified entry point ───────────────────────────────────────────────────────

async def call_with_tools(history: list, tools: list,
                          memories: dict | None = None,
                          knowledge: list | None = None,
                          profile: dict | None = None,
                          on_event: EventCallback | None = None) -> dict:
    system  = _build_system_prompt(memories, knowledge)
    profile = profile or PROFILES[Config.default_profile]
    backend = profile.get("backend", "ollama")
    model   = profile.get("model", Config.model)

    if backend == "claude":
        return await _call_claude(history, tools, system, model)
    else:
        effective_tools = [] if profile.get("no_tools") else tools
        return await _stream_ollama(history, effective_tools, system, model, on_event)


async def stream_chat(history: list) -> AsyncGenerator[str, None]:
    """Streaming chat with no tools — for simple conversation."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history
    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{Config.ollama_base_url}/api/chat",
                json={"model": Config.model, "messages": messages, "stream": True},
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    if data.get("done"):
                        break
                    content = data.get("message", {}).get("content", "")
                    if content:
                        yield content
    except httpx.ConnectError:
        yield "Ollama isn't running. Even ravens need somewhere to land."
    except Exception as e:
        yield f"Something went wrong: {e}"
