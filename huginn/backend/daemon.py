#!/usr/bin/env python3
"""
Huginn daemon — Unix socket server.
Handles chat with tool calling loop, theme switching, and session history.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import Config, PROFILES
from knowledge import DOTFILES_DIR, KNOWLEDGE_DIR, index_directory, query as knowledge_query, total_chunks
from llm import call_with_tools, stream_chat
from memory import (build_summary_prompt, compress_history, init_db,
                    load_recent_history, load_memories, save_message,
                    should_summarize)
from tools import TOOL_DEFINITIONS, TOOL_TRUST, execute_tool
from voice import VoiceEngine

MAX_TOOL_ROUNDS = 6


class HuginnDaemon:
    def __init__(self):
        init_db()
        self.history:          list[dict]                       = load_recent_history()
        self.voice             = VoiceEngine()
        self.profile           = PROFILES[Config.default_profile]
        self._pending_confirms: dict[str, asyncio.Event]        = {}
        self._confirm_results:  dict[str, bool]                 = {}
        print(f"Loaded {len(self.history)} messages from history.", flush=True)
        self._boot_index()

    def _boot_index(self) -> None:
        import threading
        def _index():
            try:
                existing = total_chunks()
                if existing == 0:
                    print("Indexing dotfiles and knowledge dir...", flush=True)
                    f1, c1 = index_directory(DOTFILES_DIR)
                    f2, c2 = index_directory(KNOWLEDGE_DIR)
                    print(f"Indexed {f1+f2} files, {c1+c2} chunks.", flush=True)
                else:
                    print(f"Knowledge base: {existing} chunks already indexed.", flush=True)
            except Exception as e:
                print(f"Index error: {e}", flush=True)
        threading.Thread(target=_index, daemon=True).start()

    async def _maybe_summarize(self) -> None:
        if not should_summarize(self.history):
            return
        try:
            to_summarize = self.history[:-10]
            prompt = build_summary_prompt(to_summarize)
            import httpx
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{Config.ollama_base_url}/api/chat",
                    json={"model": Config.model, "stream": False,
                          "messages": [{"role": "user", "content": prompt}]},
                )
                summary = resp.json().get("message", {}).get("content", "").strip()
            if summary:
                self.history = compress_history(self.history, summary)
                print(f"History compressed. Summary: {summary[:80]}...", flush=True)
        except Exception as e:
            print(f"Summarization failed: {e}", flush=True)

    # ── Message router ────────────────────────────────────────────────────────

    async def handle_message(self, msg: dict, writer: asyncio.StreamWriter) -> None:
        match msg.get("type"):
            case "chat":
                content = msg.get("content", "").strip()
                if content:
                    await self._handle_chat(content, writer, speak=msg.get("tts", False))
            case "voice_file":
                path = msg.get("path", "")
                tts  = msg.get("tts", False)
                if path:
                    await self._handle_voice(path, tts, writer)
            case "clear":
                self.history.clear()
                await self._send(writer, {"type": "cleared"})
            case "confirm":
                confirm_id = msg.get("id", "")
                approved   = bool(msg.get("approved", False))
                if confirm_id in self._pending_confirms:
                    self._confirm_results[confirm_id] = approved
                    self._pending_confirms[confirm_id].set()
                await self._send(writer, {"type": "confirm_ack"})
            case "switch_model":
                profile_name = msg.get("profile", "")
                await self._handle_switch_model(profile_name, writer)
            case "ping":
                has_claude = bool(os.environ.get("ANTHROPIC_API_KEY"))
                profiles_list = [
                    {
                        "id":        k,
                        "label":     v["label"],
                        "available": v.get("backend", "ollama") != "claude" or has_claude,
                        "backend":   v.get("backend", "ollama"),
                    }
                    for k, v in PROFILES.items()
                ]
                await self._send(writer, {
                    "type":     "pong",
                    "profile":  next((k for k, v in PROFILES.items() if v == self.profile), "fast"),
                    "label":    self.profile.get("label", ""),
                    "profiles": profiles_list,
                })
            case _:
                await self._send(writer, {"type": "error", "message": "Unknown message type"})

    async def _handle_switch_model(self, profile_name: str, writer: asyncio.StreamWriter) -> None:
        if profile_name not in PROFILES:
            available = ", ".join(PROFILES.keys())
            await self._send(writer, {"type": "error", "message": f"Unknown profile '{profile_name}'. Available: {available}"})
            return
        self.profile = PROFILES[profile_name]
        label = self.profile["label"]
        await self._send(writer, {"type": "model_switched", "profile": profile_name, "label": label})
        print(f"Switched to profile: {profile_name} ({label})", flush=True)

    # ── Chat with tool loop ───────────────────────────────────────────────────

    async def _handle_chat(self, content: str, writer: asyncio.StreamWriter, speak: bool = False) -> None:
        self.history.append({"role": "user", "content": content})
        save_message("user", content)

        await self._maybe_summarize()

        try:
            loop = asyncio.get_event_loop()
            memories, knowledge = await asyncio.gather(
                loop.run_in_executor(None, load_memories),
                loop.run_in_executor(None, knowledge_query, content),
            )
            for _ in range(MAX_TOOL_ROUNDS):
                streamed: list[str] = []

                async def on_event(event_type: str, text: str) -> None:
                    if event_type == "token":
                        streamed.append(text)
                    await self._send(writer, {"type": event_type, "content": text})

                msg = await call_with_tools(
                    self.history, TOOL_DEFINITIONS, memories, knowledge, self.profile,
                    on_event=on_event,
                )

                tool_calls = msg.get("tool_calls")
                if not tool_calls:
                    final_text = "".join(streamed).strip()
                    if final_text:
                        self.history.append({"role": "assistant", "content": final_text})
                        save_message("assistant", final_text)
                        if speak:
                            await self.voice.speak(final_text)
                    break

                self.history.append({
                    "role":       "assistant",
                    "content":    msg.get("content", ""),
                    "tool_calls": tool_calls,
                })
                save_message("assistant", msg.get("content", ""), tool_calls)

                for call in tool_calls:
                    fn        = call.get("function", call)
                    tool_name = fn["name"]
                    tool_args = fn.get("arguments", {})
                    if isinstance(tool_args, str):
                        tool_args = json.loads(tool_args)

                    # Trust tier gate
                    if TOOL_TRUST.get(tool_name, "auto") == "confirm":
                        confirm_id = f"{tool_name}_{id(tool_args)}"
                        event = asyncio.Event()
                        self._pending_confirms[confirm_id] = event
                        await self._send(writer, {
                            "type": "confirm_required",
                            "id":   confirm_id,
                            "tool": tool_name,
                            "args": tool_args,
                        })
                        await asyncio.wait_for(event.wait(), timeout=60)
                        approved = self._confirm_results.pop(confirm_id, False)
                        self._pending_confirms.pop(confirm_id, None)
                        if not approved:
                            result = "User denied this action."
                            await self._send(writer, {"type": "tool_result", "tool": tool_name, "output": result})
                            self.history.append({"role": "tool", "content": result})
                            save_message("tool", result)
                            continue

                    await self._send(writer, {"type": "tool_call", "tool": tool_name, "args": tool_args})
                    result = await execute_tool(tool_name, tool_args)
                    await self._send(writer, {"type": "tool_result", "tool": tool_name, "output": result})
                    self.history.append({"role": "tool", "content": result})
                    save_message("tool", result)

                    # Pick up profile switch requested by switch_model tool
                    switch_to = os.environ.pop("HUGINN_SWITCH_PROFILE", None)
                    if switch_to and switch_to in PROFILES:
                        self.profile = PROFILES[switch_to]
                        await self._send(writer, {"type": "model_switched",
                                                  "profile": switch_to,
                                                  "label": self.profile["label"]})

        except Exception as e:
            await self._send(writer, {"type": "error", "message": str(e)})

        await self._send(writer, {"type": "done"})

    async def _handle_voice(self, audio_path: str, tts: bool, writer: asyncio.StreamWriter) -> None:
        try:
            await self._send(writer, {"type": "status", "content": "transcribing..."})
            text = await self.voice.transcribe(audio_path)
            if not text:
                await self._send(writer, {"type": "error", "message": "Nothing heard."})
                await self._send(writer, {"type": "done"})
                return
            await self._send(writer, {"type": "transcript", "content": text})
            await self._handle_chat(text, writer, speak=tts)
        except Exception as e:
            await self._send(writer, {"type": "error", "message": f"Voice error: {e}"})
            await self._send(writer, {"type": "done"})

    # ── Socket handling ───────────────────────────────────────────────────────

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        print("Client connected", flush=True)
        try:
            async for raw in reader:
                line = raw.decode().strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    await self.handle_message(msg, writer)
                except json.JSONDecodeError:
                    await self._send(writer, {"type": "error", "message": "Invalid JSON"})
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            print("Client disconnected", flush=True)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, payload: dict) -> None:
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()

    async def run(self) -> None:
        Config.data_dir.mkdir(parents=True, exist_ok=True)
        if Config.socket_path.exists():
            Config.socket_path.unlink()

        server = await asyncio.start_unix_server(
            self.handle_client, str(Config.socket_path)
        )
        os.chmod(str(Config.socket_path), 0o600)
        print(f"Huginn is watching. Socket: {Config.socket_path}", flush=True)

        async with server:
            await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(HuginnDaemon().run())
    except KeyboardInterrupt:
        print("Huginn departs.", flush=True)
