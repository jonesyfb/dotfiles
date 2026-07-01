#!/usr/bin/env python3
"""
Huginn daemon — Unix socket server.
Handles chat with tool calling loop, theme switching, and session history.
"""
import asyncio
import base64
import json
import os
import random
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import Config, PROFILES
from knowledge import DOTFILES_DIR, KNOWLEDGE_DIR, index_directory, query as knowledge_query, total_chunks
from llm import call_with_tools, stream_chat, ollama_lock, is_game_mode
from memory import (build_summary_prompt, compress_history, init_db,
                    load_recent_history, load_memories, log_alert,
                    save_message, should_summarize)
from tools import TOOL_DEFINITIONS, TOOL_TRUST, execute_tool
from voice import VoiceEngine

MAX_TOOL_ROUNDS = 6
_MOOD_RE = re.compile(r'^\[mood:(\w+)\]\s*', re.IGNORECASE)

def _infer_mood(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ("error", "fail", "broke", "crash", "oops", "sorry", "wrong")):
        return "annoyed"
    if any(w in t for w in ("interesting", "wonder", "curious", "hmm", "strange", "odd")):
        return "thinking"
    if "!" in text:
        return "alert"
    if any(w in t for w in ("good", "great", "nice", "well done", "perfect", "excellent")):
        return "pleased"
    return "neutral"


class HuginnDaemon:
    def __init__(self):
        init_db()
        self.history:          list[dict]                       = load_recent_history()
        self.voice             = VoiceEngine()
        self.profile           = PROFILES[Config.default_profile]
        self._pending_confirms:   dict[str, asyncio.Event] = {}
        self._confirm_results:    dict[str, bool]          = {}
        self._reminder_lead:      dict[str, float]         = {}  # uid -> minutes lead time
        self._reminder_fired:     set[str]                 = set()
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
            async with ollama_lock():
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
            case "image_file":
                path    = msg.get("path", "")
                caption = msg.get("caption", "").strip()
                tts     = msg.get("tts", False)
                if path:
                    await self._handle_image(path, caption, writer, speak=tts)
            case "clear":
                self.history.clear()
                await self._send(writer, {"type": "cleared"})
            case "recover":
                self.history = load_recent_history()
                await self._send(writer, {"type": "recovered", "count": len(self.history)})
            case "confirm":
                confirm_id = msg.get("id", "")
                approved   = bool(msg.get("approved", False))
                if confirm_id in self._pending_confirms:
                    self._confirm_results[confirm_id] = approved
                    self._pending_confirms[confirm_id].set()
                await self._send(writer, {"type": "confirm_ack"})
            case "bash_event":
                asyncio.create_task(self._handle_bash_event(msg))
                await self._send(writer, {"type": "done"})
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

    _BASH_COMMENT_CHANCE = 0.35  # probability of commenting on a non-zero exit

    async def _handle_bash_event(self, msg: dict) -> None:
        cmd     = msg.get("cmd", "").strip()
        exit_code = int(msg.get("exit_code", 0))
        elapsed   = float(msg.get("elapsed", 0))
        if not cmd:
            return

        parts = []
        if elapsed >= 30:
            parts.append(f"The command took {elapsed:.0f} seconds.")
        if exit_code != 0:
            if random.random() > self._BASH_COMMENT_CHANCE:
                return
            parts.append(f"It exited with code {exit_code}.")

        if not parts:
            return
        if is_game_mode():
            return

        context = " ".join(parts)
        prompt = (
            f"Shell event: `{cmd[:200]}`\n{context}\n\n"
            "One dry, in-character line. No greeting."
        )
        try:
            msg_out = await call_with_tools(
                [{"role": "user", "content": prompt}],
                [],
                profile=self.profile,
            )
            quip = _MOOD_RE.sub("", msg_out.get("content", "")).strip()
            if quip:
                await asyncio.create_subprocess_shell(
                    f"huginn-notify --type info --title {shlex.quote('ᚹ Huginn')} --body {shlex.quote(quip)}"
                )
        except Exception as e:
            print(f"Bash event error: {e}", flush=True)

    # ── Chat with tool loop ───────────────────────────────────────────────────

    async def _handle_chat(self, content: str, writer: asyncio.StreamWriter, speak: bool = False) -> None:
        if is_game_mode():
            await self._send(writer, {"type": "token", "content": "[mood:neutral] Game mode — standing down. ᚹ"})
            await self._send(writer, {"type": "done"})
            return
        self.history.append({"role": "user", "content": content})
        save_message("user", content)
        await self._maybe_summarize()
        await self._run_llm_loop(content, writer, speak)

    async def _handle_image(self, path: str, caption: str, writer: asyncio.StreamWriter, speak: bool = False) -> None:
        try:
            img_bytes = Path(path).read_bytes()
            b64       = base64.b64encode(img_bytes).decode()
        except Exception as e:
            await self._send(writer, {"type": "error", "message": f"Failed to read image: {e}"})
            await self._send(writer, {"type": "done"})
            return
        display = caption or "What is this image?"
        self.history.append({
            "role":       "user",
            "content":    display,
            "image_b64":  b64,
            "image_type": "image/png",
        })
        save_message("user", f"[image] {display}")
        await self._maybe_summarize()
        await self._run_llm_loop(display, writer, speak)

    async def _run_llm_loop(self, query: str, writer: asyncio.StreamWriter, speak: bool = False) -> None:
        try:
            loop = asyncio.get_event_loop()
            memories, knowledge = await asyncio.gather(
                loop.run_in_executor(None, load_memories),
                loop.run_in_executor(None, knowledge_query, query),
            )
            mood_emitted = False
            for _ in range(MAX_TOOL_ROUNDS):
                streamed: list[str] = []
                mood_buf = ""
                mood_done = False

                async def on_event(event_type: str, text: str) -> None:
                    nonlocal mood_buf, mood_done, mood_emitted
                    if event_type == "token" and not mood_done:
                        mood_buf += text
                        m = _MOOD_RE.match(mood_buf)
                        if m:
                            mood_done = True
                            mood_emitted = True
                            mood_val = m.group(1).lower()
                            await self._send(writer, {"type": "expression", "mood": mood_val})
                            asyncio.create_task(self._maybe_switch_theme(mood_val))
                            remainder = mood_buf[m.end():]
                            if remainder:
                                streamed.append(remainder)
                                await self._send(writer, {"type": "token", "content": remainder})
                        elif len(mood_buf) > 20 or (mood_buf and mood_buf[0] != '['):
                            mood_done = True
                            streamed.append(mood_buf)
                            await self._send(writer, {"type": "token", "content": mood_buf})
                        return
                    if event_type == "token":
                        streamed.append(text)
                    await self._send(writer, {"type": event_type, "content": text})

                msg = await call_with_tools(
                    self.history, TOOL_DEFINITIONS, memories, knowledge, self.profile,
                    on_event=on_event,
                )

                # Flush buffered mood_buf if model gave a very short response with no tag
                if mood_buf and not mood_done:
                    streamed.append(mood_buf)
                    await self._send(writer, {"type": "token", "content": mood_buf})

                tool_calls = msg.get("tool_calls")
                if not tool_calls:
                    # Claude non-streaming: content not yet sent to client
                    if not streamed and msg.get("content"):
                        content = msg["content"]
                        m = _MOOD_RE.match(content)
                        if m:
                            mood_emitted = True
                            mood_val = m.group(1).lower()
                            await self._send(writer, {"type": "expression", "mood": mood_val})
                            asyncio.create_task(self._maybe_switch_theme(mood_val))
                            content = content[m.end():]
                        if content:
                            await self._send(writer, {"type": "token", "content": content})
                            streamed.append(content)
                    final_text = "".join(streamed).strip()
                    if final_text:
                        if not mood_emitted:
                            fallback = _infer_mood(final_text)
                            await self._send(writer, {"type": "expression", "mood": fallback})
                            asyncio.create_task(self._maybe_switch_theme(fallback))
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
            asyncio.create_task(self._monitor_loop())
            asyncio.create_task(self._chime_loop())
            asyncio.create_task(self._briefing_loop())
            asyncio.create_task(self._reminder_loop())
            asyncio.create_task(self._startup_greeting())
            await server.serve_forever()

    async def _startup_greeting(self) -> None:
        await asyncio.sleep(2)  # let socket settle
        import datetime
        now_str = datetime.datetime.now().strftime("%H:%M")
        prompt = (
            f"Time: {now_str}\n\n"
            "You just came online. Fire a single dry, in-character startup quip — "
            "like a crow ruffling its feathers and announcing its presence. "
            "One sentence. No greeting, no sign-off. Must begin with a [mood:X] tag."
        )
        try:
            msg = await call_with_tools(
                [{"role": "user", "content": prompt}],
                [],
                profile=self.profile,
            )
            quip = msg.get("content", "").strip()
            if not quip:
                return
            clean = _MOOD_RE.sub("", quip).strip()
            print(f"Startup: {quip}", flush=True)
            await asyncio.create_subprocess_shell(
                f"huginn-notify --type info --title {shlex.quote('ᚹ Huginn')} --body {shlex.quote(clean)}"
            )
            await self.voice.speak(clean)
        except Exception as e:
            print(f"Startup greeting error: {e}", flush=True)

    # ── Proactive system monitoring ───────────────────────────────────────────

    _MONITOR_INTERVAL  = 60    # seconds between system checks
    _COOLDOWN          = 300   # seconds before re-alerting same system condition
    _WEATHER_INTERVAL  = 1800  # 30 min between weather fetches

    # wttr.in weather codes grouped by significance
    _WX_SEVERE = {200,227,230,302,305,308,311,314,338,350,356,359,362,365,371,374,377,386,389,392,395}
    _WX_RAIN   = {176,263,266,281,284,293,296,299,317,320,353}
    _WX_SNOW   = {179,182,185,323,326,329,332,335,368}
    _WX_FOG    = {143,248,260}

    _CHIME_MIN = 900   # 15 min
    _CHIME_MAX = 2700  # 45 min

    _MOOD_THEME_MAP = {
        "annoyed": ("ember",   0.20),
        "alert":   ("ember",   0.12),
        "pleased": ("verdant", 0.20),
    }

    _ALERTS = {
        "cpu_temp": {
            "threshold": 85,
            "message":   "CPU temperature has reached {value}°C. The cores approach Muspelheim.",
            "title":     "ᚠ Huginn — Thermal Warning",
        },
        "disk": {
            "threshold": 90,
            "message":   "Disk usage at {value}%. The World Tree's roots grow crowded.",
            "title":     "ᚦ Huginn — Disk Warning",
        },
        "memory": {
            "threshold": 90,
            "message":   "Memory at {value}%. RAM approaches the void.",
            "title":     "ᚾ Huginn — Memory Warning",
        },
    }

    async def _monitor_loop(self) -> None:
        last_alert: dict[str, float] = {}
        last_weather_check  = 0.0
        last_weather_state  = ""
        await asyncio.sleep(30)
        while True:
            try:
                now = asyncio.get_event_loop().time()
                await self._check_cpu_temp(now, last_alert)
                await self._check_disk(now, last_alert)
                await self._check_memory(now, last_alert)
                if now - last_weather_check >= self._WEATHER_INTERVAL:
                    last_weather_state = await self._check_weather(now, last_alert, last_weather_state)
                    last_weather_check = now
            except Exception as e:
                print(f"Monitor error: {e}", flush=True)
            await asyncio.sleep(self._MONITOR_INTERVAL)

    async def _alert(self, key: str, value: int, now: float, last_alert: dict) -> None:
        if now - last_alert.get(key, 0) < self._COOLDOWN:
            return
        last_alert[key] = now
        cfg     = self._ALERTS[key]
        message = cfg["message"].format(value=value)
        title   = cfg["title"]
        print(f"ALERT [{key}]: {message}", flush=True)
        log_alert(key, message, value)
        try:
            await asyncio.create_subprocess_shell(
                f"huginn-notify --type warn --title {shlex.quote(title)} --body {shlex.quote(message)}"
            )
        except Exception:
            pass
        try:
            await self.voice.speak(message)
        except Exception:
            pass

    async def _check_cpu_temp(self, now: float, last_alert: dict) -> None:
        # Only read from k10temp (AMD CPU die temp), ignore GPU/NVMe/etc.
        import glob as _glob
        for hwmon in _glob.glob("/sys/class/hwmon/hwmon*"):
            try:
                if Path(hwmon, "name").read_text().strip() != "k10temp":
                    continue
                val = int(Path(hwmon, "temp1_input").read_text().strip()) // 1000
                if val > self._ALERTS["cpu_temp"]["threshold"]:
                    await self._alert("cpu_temp", val, now, last_alert)
                return
            except Exception:
                continue

    async def _check_disk(self, now: float, last_alert: dict) -> None:
        import shutil as _shutil
        usage = _shutil.disk_usage("/")
        pct   = int(usage.used / usage.total * 100)
        if pct >= self._ALERTS["disk"]["threshold"]:
            await self._alert("disk", pct, now, last_alert)

    async def _check_memory(self, now: float, last_alert: dict) -> None:
        try:
            lines = Path("/proc/meminfo").read_text().splitlines()
            info  = {l.split(":")[0]: int(l.split()[1]) for l in lines if ":" in l}
            total = info.get("MemTotal", 0)
            avail = info.get("MemAvailable", 0)
            if total > 0:
                pct = int((total - avail) / total * 100)
                if pct >= self._ALERTS["memory"]["threshold"]:
                    await self._alert("memory", pct, now, last_alert)
        except Exception:
            pass

    async def _check_weather(self, now: float, last_alert: dict, last_state: str) -> str:
        try:
            loc = Config.weather_location.strip()
            url = f"https://wttr.in/{loc}?format=j1"
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=10,
                    headers={"User-Agent": "HuginnWeatherBot/1.0"}) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return last_state
                data = resp.json()

            cur   = data["current_condition"][0]
            code  = int(cur["weatherCode"])
            desc  = cur["weatherDesc"][0]["value"].strip()
            temp  = cur["temp_F"]

            # Next 4 three-hour slots (~12 hours ahead)
            upcoming = data["weather"][0]["hourly"][:4]
            up_codes  = [int(h["weatherCode"]) for h in upcoming]
            up_rain   = [int(h.get("chanceofrain", 0)) for h in upcoming]

            # Classify significance: check current + imminent forecast
            if code in self._WX_SEVERE or any(c in self._WX_SEVERE for c in up_codes[:2]):
                state = "severe"
            elif code in self._WX_RAIN or any(c in self._WX_RAIN for c in up_codes[:2]) \
                    or any(p > 60 for p in up_rain[:2]):
                state = "rain"
            elif code in self._WX_SNOW or any(c in self._WX_SNOW for c in up_codes[:2]):
                state = "snow"
            elif code in self._WX_FOG:
                state = "fog"
            else:
                state = "clear"

            print(f"Weather: {desc} {temp}°F [{state}] (was: {last_state})", flush=True)

            # Only notify on transitions TO a notable state
            if state != "clear" and state != last_state:
                cooldown = 3600 if state == "severe" else 7200
                key = f"weather_{state}"
                if now - last_alert.get(key, 0) >= cooldown:
                    last_alert[key] = now
                    if state == "severe":
                        ntype = "warn"
                        title = "ᚨ Huginn — Severe Weather"
                        body  = f"{desc}, {temp}°F. The storm ravens circle."
                    elif state == "rain":
                        ntype = "info"
                        title = "ᚨ Huginn — Rain Incoming"
                        body  = f"{desc}, {temp}°F. The clouds gather."
                    elif state == "snow":
                        ntype = "info"
                        title = "ᚨ Huginn — Snow"
                        body  = f"{desc}, {temp}°F. Winter tightens its grip."
                    else:
                        ntype = "info"
                        title = "ᚨ Huginn — Weather"
                        body  = f"{desc}, {temp}°F."
                    log_alert(key, body, 0)
                    await asyncio.create_subprocess_shell(
                        f"huginn-notify --type {ntype} --title {shlex.quote(title)} "
                        f"--body {shlex.quote(body)}"
                    )

            return state
        except Exception as e:
            print(f"Weather check error: {e}", flush=True)
            return last_state


    async def _maybe_switch_theme(self, mood: str) -> None:
        entry = self._MOOD_THEME_MAP.get(mood)
        if not entry:
            return
        target_slug, probability = entry
        if random.random() > probability:
            return
        theme_file = Path.home() / ".config" / "huginn" / "current-theme.json"
        try:
            if json.loads(theme_file.read_text()).get("slug", "") == target_slug:
                return
        except Exception:
            pass
        try:
            from theme import apply_theme
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, apply_theme, target_slug)
            print(f"Mood theme: {mood} → {target_slug}", flush=True)
        except Exception as e:
            print(f"Theme switch error: {e}", flush=True)

    async def _chime_loop(self) -> None:
        await asyncio.sleep(random.uniform(self._CHIME_MIN, self._CHIME_MAX))
        while True:
            try:
                await self._fire_chime()
            except Exception as e:
                print(f"Chime error: {e}", flush=True)
            await asyncio.sleep(random.uniform(self._CHIME_MIN, self._CHIME_MAX))

    async def _fire_chime(self) -> None:
        import glob
        import tempfile
        env = dict(os.environ)
        if "NIRI_SOCKET" not in env:
            socks = glob.glob(f"/run/user/{os.getuid()}/niri*.sock")
            if socks:
                env["NIRI_SOCKET"] = socks[0]
        proc = await asyncio.create_subprocess_exec(
            "niri", "msg", "--json", "focused-window",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        stdout, _ = await proc.communicate()
        window_info = ""
        if stdout:
            try:
                data = json.loads(stdout.decode())
                app   = data.get("app_id", "")
                title = data.get("title", "")
                window_info = f'{app} — "{title}"' if title else app
            except Exception:
                window_info = stdout.decode().strip()[:120]

        screenshot_b64 = None
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = tmp.name
            grim = await asyncio.create_subprocess_exec(
                "grim", tmp_path,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                env=env,
            )
            await grim.communicate()
            if grim.returncode == 0:
                with open(tmp_path, "rb") as f:
                    screenshot_b64 = base64.b64encode(f.read()).decode()
        except Exception as e:
            print(f"Chime screenshot failed: {e}", flush=True)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        import datetime
        now_str = datetime.datetime.now().strftime("%H:%M")
        prompt = (
            f"The user is currently focused on: {window_info or 'unknown'}\n"
            f"Time: {now_str}\n\n"
            "Fire a single dry, in-character observation about what they're doing. "
            "One sentence, two at most. No greeting, no sign-off. Just the quip."
        )

        user_msg: dict = {"role": "user", "content": prompt}
        if screenshot_b64:
            user_msg["image_b64"] = screenshot_b64
            user_msg["image_type"] = "image/png"

        chime_profile = PROFILES.get("vision", self.profile) if screenshot_b64 else self.profile
        msg = await call_with_tools(
            [user_msg],
            [],
            profile=chime_profile,
        )
        quip = _MOOD_RE.sub("", msg.get("content", "").strip()).strip()
        if not quip:
            return

        print(f"Chime: {quip}", flush=True)
        await asyncio.create_subprocess_shell(
            f"huginn-notify --type info --title {shlex.quote('ᚹ Huginn')} --body {shlex.quote(quip)}"
        )
        import datetime
        chime_log = Config.data_dir / "chimes.log"
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        with chime_log.open("a") as f:
            f.write(f"[{ts}] [{window_info or 'unknown'}] {quip}\n")


    async def _briefing_loop(self) -> None:
        import datetime
        while True:
            now = datetime.datetime.now()
            h, m = map(int, Config.briefing_time.split(":"))
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if target <= now:
                target += datetime.timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            try:
                await self._fire_briefing()
            except Exception as e:
                print(f"Briefing error: {e}", flush=True)

    async def _fire_briefing(self) -> None:
        from tools import execute_tool
        import datetime
        now_str = datetime.datetime.now().strftime("%A, %B %-d at %H:%M")
        weather, news, calendar = await asyncio.gather(
            execute_tool("weather",       {"location": Config.weather_location}),
            execute_tool("news",          {"source": "all", "count": 6}),
            execute_tool("calendar_list", {"days": 3}),
        )
        prompt = (
            f"It's {now_str}. Here's today's data:\n\n"
            f"WEATHER:\n{weather}\n\n"
            f"CALENDAR (next 3 days):\n{calendar}\n\n"
            f"NEWS:\n{news}\n\n"
            "Deliver a morning briefing in Huginn's voice — dry, in-character, like a crow "
            "reporting from the field. Cover the weather briefly, mention any upcoming events, "
            "then the most interesting headlines. Three to five sentences total. Begin with a [mood:X] tag."
        )
        msg = await call_with_tools(
            [{"role": "user", "content": prompt}],
            [],
            profile=self.profile,
        )
        briefing = msg.get("content", "").strip()
        if not briefing:
            return
        clean = _MOOD_RE.sub("", briefing).strip()
        print(f"Briefing: {briefing}", flush=True)
        await asyncio.create_subprocess_shell(
            f"huginn-notify --type info --title {shlex.quote('ᚹ Huginn — Morning Briefing')} --body {shlex.quote(clean)}"
        )
        await self.voice.speak(clean)


    _REMINDER_CHECK_INTERVAL = 3 * 60   # check every 3 minutes
    _REMINDER_LOOKAHEAD      = 2 * 60   # look 2 hours ahead for events

    async def _reminder_loop(self) -> None:
        await asyncio.sleep(30)  # let startup settle
        while True:
            try:
                await self._check_reminders()
            except Exception as e:
                print(f"Reminder check error: {e}", flush=True)
            await asyncio.sleep(self._REMINDER_CHECK_INTERVAL)

    async def _check_reminders(self) -> None:
        import datetime
        from tools import execute_tool

        loop = asyncio.get_event_loop()

        # Fetch events in the next lookahead window
        def _fetch():
            import caldav
            from tools import _caldav_client, Config as _Cfg
            client = _caldav_client()
            cal    = client.calendar(url=_Cfg.caldav_url)
            now    = datetime.datetime.now(tz=datetime.timezone.utc)
            end    = now + datetime.timedelta(minutes=self._REMINDER_LOOKAHEAD)
            return cal.search(start=now, end=end, event=True, expand=True)

        events = await loop.run_in_executor(None, _fetch)

        import datetime
        now = datetime.datetime.now(tz=datetime.timezone.utc)

        for ev in events:
            c       = ev.icalendar_component
            uid     = str(c.get("UID", ""))
            if not uid or uid in self._reminder_fired:
                continue

            summary = str(c.get("SUMMARY", "(no title)"))
            dtstart = c.get("DTSTART")
            dtend   = c.get("DTEND")
            loc     = str(c.get("LOCATION", ""))
            desc    = str(c.get("DESCRIPTION", ""))

            if not dtstart:
                continue

            start_dt = dtstart.dt
            if isinstance(start_dt, datetime.date) and not isinstance(start_dt, datetime.datetime):
                # all-day event — treat as midnight UTC
                start_dt = datetime.datetime(start_dt.year, start_dt.month, start_dt.day,
                                             tzinfo=datetime.timezone.utc)
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=datetime.timezone.utc)

            minutes_until = (start_dt - now).total_seconds() / 60

            # Ask the LLM for lead time if we haven't decided yet
            if uid not in self._reminder_lead:
                end_s  = dtend.dt.isoformat() if dtend else "?"
                prompt = (
                    f"Calendar event coming up:\n"
                    f"  Title: {summary}\n"
                    f"  Start: {start_dt.isoformat()}\n"
                    f"  End:   {end_s}\n"
                    + (f"  Location: {loc}\n" if loc else "")
                    + (f"  Description: {desc[:200]}\n" if desc else "")
                    + f"\nHow many minutes before this event should the user be reminded? "
                    f"Reply with a single integer only. Consider travel time if there's a location, "
                    f"prep time for meetings, etc."
                )
                try:
                    msg = await call_with_tools(
                        [{"role": "user", "content": prompt}],
                        [],
                        profile=self.profile,
                    )
                    raw = msg.get("content", "").strip()
                    # extract first integer from response
                    import re as _re
                    m = _re.search(r'\d+', raw)
                    lead = float(m.group()) if m else 15.0
                    lead = max(2.0, min(lead, 120.0))  # clamp 2–120 min
                    self._reminder_lead[uid] = lead
                    print(f"Reminder lead for '{summary}': {lead:.0f} min", flush=True)
                except Exception as e:
                    self._reminder_lead[uid] = 15.0
                    print(f"Lead time LLM error: {e}", flush=True)

            lead_minutes = self._reminder_lead[uid]
            if minutes_until <= lead_minutes:
                self._reminder_fired.add(uid)
                when = f"in {int(minutes_until)} min" if minutes_until > 1 else "now"
                body = f"{summary} — {when}"
                if loc:
                    body += f" @ {loc}"
                print(f"Reminder firing: {body}", flush=True)
                await asyncio.create_subprocess_shell(
                    f"huginn-notify --type info --title {shlex.quote('ᚹ Huginn — Reminder')} --body {shlex.quote(body)}"
                )
                await self.voice.speak(body)


if __name__ == "__main__":
    try:
        asyncio.run(HuginnDaemon().run())
    except KeyboardInterrupt:
        print("Huginn departs.", flush=True)
