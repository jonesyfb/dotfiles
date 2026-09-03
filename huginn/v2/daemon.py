#!/usr/bin/env python3
"""
Huginn v2 daemon — clean async rewrite.
Listens on a Unix socket, routes chat through the appropriate model,
manages a persistent task queue, and emits chimes on system events.
"""
import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import ambient
import context
import personality
from config import SOCKET_PATH, SYSTEM_PROMPT, GAME_MODE_FLAG
from coordinator import Purpose, coordinator
from gatekeeper import activity_summary, activity_tracker_worker, check_gate, screenshot_worker
from llm import route_model, stream_chat
from memory import (
    add_turn, get_history, clear_history, session_snapshot,
    enqueue_task, get_pending_tasks, update_task_status, get_all_tasks,
    log_ambient_event, recent_verdicts,
)
from tools import TOOL_DEFINITIONS, TOOL_TRUST, run_tool, shell_is_safe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("huginn")

# Pending tool calls waiting on user confirm
_pending_confirms: dict[str, dict] = {}


# ── Writer helpers ────────────────────────────────────────────────────────────

async def send(writer: asyncio.StreamWriter, obj: dict) -> None:
    writer.write((json.dumps(obj) + "\n").encode())
    await writer.drain()


# ── Chat handler ──────────────────────────────────────────────────────────────

async def handle_chat(writer: asyncio.StreamWriter, content: str) -> None:
    if Path(GAME_MODE_FLAG).exists():
        await send(writer, {"type": "token", "content": "Game mode. Standing down. ᚹ"})
        await send(writer, {"type": "done"})
        return

    add_turn("user", content)
    model_key = route_model(content)

    history = get_history(limit=40)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + history

    full_response = ""
    tool_calls_made: list[dict] = []

    async for ev in stream_chat(messages, model_key, tools=TOOL_DEFINITIONS):
        if ev["type"] == "thinking":
            await send(writer, {"type": "thinking", "content": ev["content"]})
        elif ev["type"] == "token":
            full_response += ev["content"]
            await send(writer, {"type": "token", "content": ev["content"]})
        elif ev["type"] == "tool_call":
            tool_calls_made.append(ev)
        elif ev["type"] == "done":
            break

    # Process tool calls
    if tool_calls_made:
        if full_response:
            add_turn("assistant", full_response)
            full_response = ""

        tool_results = []
        for tc in tool_calls_made:
            name = tc["tool"]
            args = tc.get("args", {})
            trust = TOOL_TRUST.get(name, "confirm")

            # Shell gets extra safety check
            if name == "shell" and shell_is_safe(args.get("command", "")):
                trust = "auto"

            if trust == "confirm":
                confirm_id = str(uuid.uuid4())
                _pending_confirms[confirm_id] = {
                    "tool": name, "args": args, "writer": writer,
                    "tool_calls": tool_calls_made, "history": get_history(40),
                    "model_key": model_key,
                }
                await send(writer, {
                    "type": "confirm_required",
                    "id": confirm_id,
                    "tool": name,
                    "args": args,
                })
                return  # caller will resume via handle_confirm
            else:
                await send(writer, {"type": "tool_call", "tool": name, "args": args})
                result = await run_tool(name, args)
                await send(writer, {"type": "tool_result", "tool": name, "output": result})
                tool_results.append({
                    "role": "tool",
                    "content": result,
                    "name": name,
                })

        if tool_results:
            # Ollama expects: assistant msg with tool_calls field, then tool result msgs
            asst_tool_msg = {
                "role": "assistant",
                "content": full_response or "",
                "tool_calls": [
                    {"function": {"name": tc["tool"], "arguments": tc.get("args", {})}}
                    for tc in tool_calls_made
                ],
            }
            prior_history = get_history(40)
            # Drop the last user turn we just added (we'll add it explicitly)
            if prior_history and prior_history[-1]["role"] == "user":
                prior_history = prior_history[:-1]
            follow_messages = (
                [{"role": "system", "content": SYSTEM_PROMPT}]
                + prior_history
                + [{"role": "user", "content": content}]
                + [asst_tool_msg]
                + [{"role": "tool", "content": r["content"]} for r in tool_results]
            )
            full_response = ""
            thinking_buf = ""

            async for ev in stream_chat(follow_messages, model_key):
                if ev["type"] == "thinking":
                    thinking_buf += ev["content"]
                elif ev["type"] == "token":
                    full_response += ev["content"]
                    await send(writer, {"type": "token", "content": ev["content"]})
                elif ev["type"] == "done":
                    break

            # If the model put its entire response in thinking, surface the last sentence
            if not full_response and thinking_buf:
                last = thinking_buf.rstrip().rsplit("\n", 1)[-1].strip()
                if last:
                    await send(writer, {"type": "token", "content": last})
                    full_response = last

    if full_response:
        add_turn("assistant", full_response)

    await send(writer, {"type": "done"})


async def handle_confirm(writer: asyncio.StreamWriter, confirm_id: str, approved: bool) -> None:
    pending = _pending_confirms.pop(confirm_id, None)
    if not pending:
        await send(writer, {"type": "confirm_ack", "approved": approved})
        return

    await send(writer, {"type": "confirm_ack", "approved": approved})

    if not approved:
        add_turn("assistant", "[tool denied]")
        await send(writer, {"type": "token", "content": "Denied."})
        await send(writer, {"type": "done"})
        return

    name = pending["tool"]
    args = pending["args"]
    model_key = pending["model_key"]

    await send(writer, {"type": "tool_call", "tool": name, "args": args})
    result = await run_tool(name, args)
    await send(writer, {"type": "tool_result", "tool": name, "output": result})

    history = pending["history"]
    asst_tool_msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": name, "arguments": args}}],
    }
    follow_messages = (
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + history
        + [asst_tool_msg]
        + [{"role": "tool", "content": result}]
    )

    full_response = ""
    async for ev in stream_chat(follow_messages, model_key):
        if ev["type"] == "token":
            full_response += ev["content"]
            await send(writer, {"type": "token", "content": ev["content"]})
        elif ev["type"] == "done":
            break

    if full_response:
        add_turn("assistant", full_response)
    await send(writer, {"type": "done"})


# ── Task queue ────────────────────────────────────────────────────────────────

async def task_worker() -> None:
    """Background loop that runs queued tasks one at a time."""
    while True:
        pending = get_pending_tasks()
        for task in pending:
            update_task_status(task["id"], "running")
            try:
                proc = await asyncio.create_subprocess_shell(
                    task["command"],
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
                result = out.decode(errors="replace").strip()[-2000:]
                update_task_status(task["id"], "done", result)
                await _notify_task_complete(task["label"], result)
            except asyncio.TimeoutError:
                update_task_status(task["id"], "failed", "timeout")
            except Exception as e:
                update_task_status(task["id"], "failed", str(e))
        await asyncio.sleep(5)


_AMBIENT_KIND = "periodic_observation"

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _numeric_facts(facts: dict) -> dict[str, float]:
    """Best-effort extraction of a leading numeric token from each fact
    value (e.g. "3%" -> 3.0, "6.7Gi" -> 6.7) for baseline-deviation
    worthiness checks. Facts with no numeric token are silently skipped —
    this is a heuristic feed into evaluate_periodic_worthiness, not a
    parser anything else depends on."""
    out = {}
    for key, value in facts.items():
        m = _NUM_RE.search(value)
        if m:
            try:
                out[key] = float(m.group())
            except ValueError:
                pass
    return out


async def random_chime_worker() -> None:
    """Hourly loop: ask the ambient policy whether Huginn may make an
    unsolicited dry observation right now. The policy — not a dice roll —
    decides via eligibility (interaction/attention/snooze), worthiness (a
    real deviation from the last-seen stats, not just availability of
    sensor data), and cooldown/budget/dedup; a denial here cannot be
    overridden by anything generated below. Wording comes from the narrow
    personality renderer (qwen3.5:4b), not a raw freeform chat completion —
    it only ever sees the parsed stats facts, never the full Runtime
    Context Engine snapshot or SYSTEM_PROMPT."""
    await asyncio.sleep(60)  # settle after startup
    while True:
        await asyncio.sleep(3600)
        try:
            snapshot = await context.collect()
            stats = await _run_stats()
            facts = _parse_stats(stats)
            worthiness = ambient.evaluate_periodic_worthiness(_AMBIENT_KIND, _numeric_facts(facts))
            opportunity = ambient.AmbientOpportunity(kind=_AMBIENT_KIND, worthiness=worthiness)
            decision = ambient.decide(opportunity, snapshot)
            if not decision.allowed:
                continue

            request = personality.PersonalityRequest(
                purpose=_AMBIENT_KIND,
                facts=facts,
                severity=decision.severity,
                interruption_reason=decision.reason,
                max_length=120,
                prohibited_additions=("diagnosis", "recommendation", "urgency", "an action to take"),
                interaction_mode=snapshot.interaction.mode,
            )
            result = await personality.render(request, purpose=Purpose.AMBIENT)
            if not result.ok:
                # Noncritical ambient content: silence on failure, never a
                # deterministic fallback chime — there's nothing anyone is
                # waiting to hear here.
                continue
            response = result.text

            # Re-check with the actual generated text so dedup can run
            # against it — everything else in the snapshot is unchanged
            # since the first check moments ago.
            final = ambient.decide(
                ambient.AmbientOpportunity(kind=_AMBIENT_KIND, severity=decision.severity, worthiness=worthiness, candidate_text=response),
                snapshot,
            )
            if not final.allowed:
                continue

            log_ambient_event(_AMBIENT_KIND, response)
            _emit_chime("huginn", response)
        except Exception:
            pass


async def _run_stats() -> str:
    from tools import run_tool
    return await run_tool("system_stats", {})


def _parse_stats(raw: str) -> dict:
    """system_stats' output is already "KEY:value" lines — turn it into a
    flat facts dict instead of handing the raw blob to the renderer as
    freeform prompt text."""
    facts = {}
    for line in raw.splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            facts[key.strip()] = value.strip()
    return facts


async def _notify_task_complete(label: str, result: str) -> None:
    """Renders a task-completion notification in Huginn's voice. Unlike the
    periodic ambient chime, this is meaningful content someone may actually
    be waiting on — on a flavor-render failure this publishes the
    deterministic factual sentence (always computed by personality.render(),
    independent of the model call) instead of going silent."""
    try:
        request = personality.PersonalityRequest(
            purpose="task_complete",
            facts={"task": label, "result_preview": result[:100]},
            protected_keys=("task",),
            severity="info",
            interaction_mode="ambient",
        )
        render_result = await personality.render(request, purpose=Purpose.AMBIENT)
        text = render_result.text if render_result.ok else render_result.deterministic
        _emit_chime("huginn", text)
    except Exception:
        _emit_chime("huginn", f"Task complete: {label}")


def _emit_chime(title: str, body: str, notif_type: str = "info") -> None:
    from config import CHIME_LOG
    import time
    log_line = f"[{time.strftime('%H:%M')}] {title}: {body}\n"
    Path(CHIME_LOG).parent.mkdir(parents=True, exist_ok=True)
    with open(CHIME_LOG, "a") as f:
        f.write(log_line)
    subprocess.run(
        ["huginn-notify", "--type", notif_type, "--title", title, "--body", body[:200]],
        capture_output=True, timeout=5,
    )


# ── Connection handler ────────────────────────────────────────────────────────

async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        data = await asyncio.wait_for(reader.readline(), timeout=30)
        if not data:
            return
        msg = json.loads(data.decode().strip())
        t = msg.get("type", "")

        if t == "ping":
            await send(writer, {
                "type": "pong",
                "version": "2",
                "label": "auto",
                "profile": "auto",
                "profiles": [],
            })

        elif t == "chat":
            content = msg.get("content", "").strip()
            if content:
                await handle_chat(writer, content)
            else:
                await send(writer, {"type": "done"})

        elif t == "bash_event":
            # Chime on interesting bash events
            exit_code = msg.get("exit_code", 0)
            elapsed = msg.get("elapsed", 0)
            cmd = msg.get("cmd", "")
            if Path(GAME_MODE_FLAG).exists():
                return
            if elapsed > 30 or exit_code != 0:
                await _handle_bash_chime(writer, exit_code, elapsed, cmd)

        elif t == "confirm":
            await handle_confirm(writer, msg.get("id", ""), msg.get("approved", False))

        elif t == "clear":
            clear_history()
            await send(writer, {"type": "cleared"})

        elif t == "recover":
            history = session_snapshot()
            for turn in history:
                if turn["role"] == "user":
                    content = turn["content"] if isinstance(turn["content"], str) else json.dumps(turn["content"])
                    await send(writer, {"type": "transcript", "content": content})
                elif turn["role"] == "assistant":
                    content = turn["content"] if isinstance(turn["content"], str) else json.dumps(turn["content"])
                    await send(writer, {"type": "token", "content": content})
            await send(writer, {"type": "recovered"})

        elif t == "switch_model":
            # v2 uses auto-routing, but acknowledge for QML compatibility
            await send(writer, {"type": "model_switched", "label": "auto", "profile": "auto"})

        elif t == "task_queue":
            tasks = get_all_tasks()
            await send(writer, {"type": "task_list", "tasks": tasks})

        elif t == "gate_check":
            target = msg.get("target", "")
            verdict = await check_gate(target)
            await send(writer, {"type": "gate_verdict", "target": target, **verdict})

        elif t == "gate_history":
            await send(writer, {
                "type": "gate_history",
                "steam": recent_verdicts("steam", 8),
                "youtube": recent_verdicts("youtube", 8),
                "activity": activity_summary(),
            })

        elif t == "context_snapshot":
            snapshot = await context.collect()
            await send(writer, {"type": "context_snapshot", "data": context.to_debug_dict(snapshot)})

        else:
            await send(writer, {"type": "error", "message": f"unknown type: {t}"})

    except json.JSONDecodeError:
        await send(writer, {"type": "error", "message": "invalid json"})
    except Exception as e:
        log.exception("handler error")
        try:
            await send(writer, {"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _handle_bash_chime(
    writer: asyncio.StreamWriter, exit_code: int, elapsed: float, cmd: str
) -> None:
    """The bash hook (scripts/huginn-bash.sh) fires this fully detached —
    `&>/dev/null &` — so nothing ever reads the streamed tokens this used
    to send; only the resulting notification (_emit_chime) is ever seen.
    Renders via the narrow personality path instead of a raw SYSTEM_PROMPT
    chat completion. A failed/slow command is actionable information, not
    disposable ambient content — falls back to a deterministic message on
    render failure rather than going silent."""
    failed = exit_code != 0
    short_cmd = cmd[:60] + ("…" if len(cmd) > 60 else "")

    try:
        request = personality.PersonalityRequest(
            purpose="bash_event",
            facts={
                "command": short_cmd,
                "exit_code": str(exit_code),
                "elapsed_seconds": f"{elapsed:.0f}",
                "outcome": "failed" if failed else "finished (slow)",
            },
            protected_keys=("command", "exit_code", "elapsed_seconds"),
            severity="notice" if failed else "info",
            prohibited_additions=("a fix", "a diagnosis of the cause") if failed else (),
            interaction_mode="ambient",
        )
        render_result = await personality.render(request, purpose=Purpose.AMBIENT)
        response = render_result.text if render_result.ok else render_result.deterministic
    except Exception:
        response = f"Command {'failed' if failed else 'finished'} (exit {exit_code}) after {elapsed:.0f}s: {short_cmd}"

    _emit_chime("Huginn", response[:200])
    await send(writer, {"type": "done"})


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()

    server = await asyncio.start_unix_server(handle_connection, path=str(SOCKET_PATH))
    os.chmod(str(SOCKET_PATH), 0o600)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.ensure_future(_shutdown(server)))

    coordinator.set_game_mode_check(lambda: context.collect_interaction().mode == "game")
    coordinator.start()

    asyncio.ensure_future(task_worker())
    asyncio.ensure_future(random_chime_worker())
    asyncio.ensure_future(activity_tracker_worker())
    asyncio.ensure_future(screenshot_worker())

    log.info("Huginn v2 listening on %s", SOCKET_PATH)
    async with server:
        await server.serve_forever()


async def _shutdown(server: asyncio.Server) -> None:
    log.info("shutting down")
    server.close()
    await server.wait_closed()
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    asyncio.get_event_loop().stop()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    asyncio.run(main())
