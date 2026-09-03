"""Tests for daemon.py's three personality-rendering call sites:
random_chime_worker (ambient periodic observation), _notify_task_complete
(task-queue completion), and _handle_bash_chime (shell hook notification).

All mocked at personality.render / ambient.decide — no real Ollama call
anywhere in this file.
"""
import asyncio

import ambient
import context
import daemon
import memory
import personality
from coordinator import Purpose
from personality import RenderResult


# ── _parse_stats ───────────────────────────────────────────────────────────────

def test_parse_stats_splits_key_value_lines():
    raw = "TIME:19:27 CDT\nCPU:2%\nMEM:6.7Gi/30Gi\nUPTIME:up 2 hours, 47 minutes"
    facts = daemon._parse_stats(raw)
    assert facts["CPU"] == "2%"
    assert facts["MEM"] == "6.7Gi/30Gi"
    assert facts["UPTIME"] == "up 2 hours, 47 minutes"


def test_parse_stats_ignores_lines_without_colon():
    facts = daemon._parse_stats("CPU:2%\nnot a kv line\nMEM:1Gi")
    assert facts == {"CPU": "2%", "MEM": "1Gi"}


def test_numeric_facts_extracts_leading_numbers():
    facts = {"CPU": "2%", "MEM": "6.7Gi/30Gi", "UPTIME": "up 2 hours"}
    numeric = daemon._numeric_facts(facts)
    assert numeric["CPU"] == 2.0
    assert numeric["MEM"] == 6.7
    assert numeric["UPTIME"] == 2.0


def test_numeric_facts_skips_non_numeric_values():
    facts = {"STATUS": "no data available"}
    assert daemon._numeric_facts(facts) == {}


# ── _notify_task_complete ──────────────────────────────────────────────────────

def test_notify_task_complete_uses_rendered_text_on_success(monkeypatch):
    emitted = {}
    monkeypatch.setattr(daemon, "_emit_chime", lambda title, body, *a, **kw: emitted.update(title=title, body=body))

    async def fake_render(request, *, purpose=Purpose.AMBIENT):
        assert request.purpose == "task_complete"
        return RenderResult(True, "The frontend build finished. No drama this time.", "No drama this time.", "task: build-frontend", "rendered")

    monkeypatch.setattr(personality, "render", fake_render)

    asyncio.run(daemon._notify_task_complete("build-frontend", "webpack output here"))

    assert emitted["title"] == "huginn"
    assert emitted["body"] == "The frontend build finished. No drama this time."


def test_notify_task_complete_uses_deterministic_on_render_failure(monkeypatch):
    """A failed flavor render still publishes the always-computed
    deterministic factual sentence — task_complete is actionable content,
    never silence."""
    emitted = {}
    monkeypatch.setattr(daemon, "_emit_chime", lambda title, body, *a, **kw: emitted.update(title=title, body=body))

    async def fake_render(request, *, purpose=Purpose.AMBIENT):
        return RenderResult(False, None, None, "task: build-frontend; result preview: webpack output here", "coordinator_denied:deadline_exceeded")

    monkeypatch.setattr(personality, "render", fake_render)

    asyncio.run(daemon._notify_task_complete("build-frontend", "webpack output here"))

    assert emitted["title"] == "huginn"
    assert "build-frontend" in emitted["body"]
    assert "webpack output here" in emitted["body"]


def test_notify_task_complete_falls_back_on_unexpected_exception(monkeypatch):
    emitted = {}
    monkeypatch.setattr(daemon, "_emit_chime", lambda title, body, *a, **kw: emitted.update(title=title, body=body))

    async def boom(request, *, purpose=Purpose.AMBIENT):
        raise RuntimeError("coordinator exploded")

    monkeypatch.setattr(personality, "render", boom)

    asyncio.run(daemon._notify_task_complete("build-frontend", "output"))

    assert emitted["title"] == "huginn"
    assert "build-frontend" in emitted["body"]


# ── _handle_bash_chime ─────────────────────────────────────────────────────────

class _FakeWriter:
    def __init__(self):
        self.sent = []

    def write(self, data):
        self.sent.append(data)

    async def drain(self):
        pass


def test_handle_bash_chime_uses_rendered_text_on_success(monkeypatch):
    emitted = {}
    monkeypatch.setattr(daemon, "_emit_chime", lambda title, body, *a, **kw: emitted.update(title=title, body=body))

    async def fake_render(request, *, purpose=Purpose.AMBIENT):
        assert request.purpose == "bash_event"
        assert request.severity == "notice"  # exit_code != 0
        return RenderResult(True, "That command did not go well.", "That command did not go well.", "command: make build", "rendered")

    monkeypatch.setattr(personality, "render", fake_render)

    writer = _FakeWriter()
    asyncio.run(daemon._handle_bash_chime(writer, 1, 12.0, "make build"))

    assert emitted["body"] == "That command did not go well."


def test_handle_bash_chime_uses_deterministic_and_preserves_exact_command(monkeypatch):
    emitted = {}
    monkeypatch.setattr(daemon, "_emit_chime", lambda title, body, *a, **kw: emitted.update(title=title, body=body))

    async def fake_render(request, *, purpose=Purpose.AMBIENT):
        return RenderResult(False, None, None, "rm -rf build/ exited with exit 2", "validation_failed")

    monkeypatch.setattr(personality, "render", fake_render)

    writer = _FakeWriter()
    asyncio.run(daemon._handle_bash_chime(writer, 2, 5.0, "rm -rf build/"))

    assert "exit 2" in emitted["body"]
    assert "rm -rf build/" in emitted["body"]


def test_handle_bash_chime_falls_back_on_unexpected_exception(monkeypatch):
    """The last-resort literal fallback only fires if personality.render()
    itself raises, not on an ordinary validation_failed/denied result
    (which already has a usable deterministic sentence)."""
    emitted = {}
    monkeypatch.setattr(daemon, "_emit_chime", lambda title, body, *a, **kw: emitted.update(title=title, body=body))

    async def boom(request, *, purpose=Purpose.AMBIENT):
        raise RuntimeError("boom")

    monkeypatch.setattr(personality, "render", boom)

    writer = _FakeWriter()
    asyncio.run(daemon._handle_bash_chime(writer, 2, 5.0, "rm -rf build/"))

    assert "exit 2" in emitted["body"]
    assert "rm -rf build/" in emitted["body"]


def test_handle_bash_chime_success_path_notice_vs_info_severity(monkeypatch):
    captured = {}

    async def fake_render(request, *, purpose=Purpose.AMBIENT):
        captured["severity"] = request.severity
        return RenderResult(True, "Slow, but it finished.", "Slow, but it finished.", "outcome: finished (slow)", "rendered")

    monkeypatch.setattr(personality, "render", fake_render)
    monkeypatch.setattr(daemon, "_emit_chime", lambda *a, **kw: None)

    writer = _FakeWriter()
    asyncio.run(daemon._handle_bash_chime(writer, 0, 45.0, "cargo build --release"))

    assert captured["severity"] == "info"  # exit_code == 0, just slow


# ── random_chime_worker: policy gating ────────────────────────────────────────

_real_sleep = asyncio.sleep  # captured before any monkeypatching


async def _instant_sleep(*_):
    """Replaces daemon.asyncio.sleep for these tests — daemon.asyncio IS the
    real asyncio module (not a copy), so patching its .sleep with anything
    that calls asyncio.sleep by name would recurse into the patched version.
    Still yields control once (await _real_sleep(0)) rather than returning
    synchronously — without that yield, asyncio.wait_for's own cancellation
    never gets a chance to run and the test hangs instead of timing out."""
    await _real_sleep(0)


async def _spin_worker_briefly():
    try:
        await asyncio.wait_for(daemon.random_chime_worker(), timeout=0.3)
    except asyncio.TimeoutError:
        pass


def test_random_chime_worker_never_renders_when_policy_denies(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(daemon.asyncio, "sleep", _instant_sleep)

    async def fake_collect():
        return object()  # never inspected further since decide() is mocked

    monkeypatch.setattr(context, "collect", fake_collect)
    monkeypatch.setattr(daemon, "_run_stats", lambda: _async_return("CPU:2%\nMEM:1Gi"))

    def deny(*a, **kw):
        return ambient.AmbientDecision(False, "cooldown active", "info", 0, None, "observed")

    monkeypatch.setattr(ambient, "decide", deny)

    render_calls = {"n": 0}

    async def spy_render(request, *, purpose=Purpose.AMBIENT):
        render_calls["n"] += 1
        return RenderResult(True, "should not happen", "should not happen", "", "rendered")

    monkeypatch.setattr(personality, "render", spy_render)

    asyncio.run(_spin_worker_briefly())

    assert render_calls["n"] == 0


def test_random_chime_worker_renders_with_ambient_purpose_when_policy_allows(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(daemon.asyncio, "sleep", _instant_sleep)

    fake_snapshot = context.RuntimeContext(
        timestamp=0.0,
        interaction=context.InteractionState(mode="ambient", interruptions_allowed=True, source="default"),
        attention=context.AttentionState("available"),
        task=context.TaskState("idle", 0),
        models={}, tools=context.ToolAvailability(True, True),
        desktop=context.DesktopState(focused_window=None, in_discord_call=False),
        model_resources=context.ModelResourceState((), True, False, {}, False, True),
        coordinator={},
    )

    async def fake_collect():
        return fake_snapshot

    monkeypatch.setattr(context, "collect", fake_collect)

    call_count = {"n": 0}

    def allow_once(opportunity, snapshot):
        call_count["n"] += 1
        # Allow the pre-generation check; deny the post-generation dedup
        # re-check so the loop doesn't spin emitting real chimes forever.
        if opportunity.candidate_text is None:
            return ambient.AmbientDecision(True, "allowed", "info", 1800, "personality", "observed")
        return ambient.AmbientDecision(False, "dedup", "info", 1800, None, "observed")

    monkeypatch.setattr(ambient, "decide", allow_once)
    monkeypatch.setattr(daemon, "_run_stats", lambda: _async_return("CPU:2%\nMEM:1Gi"))

    render_calls = []

    async def spy_render(request, *, purpose=Purpose.AMBIENT):
        render_calls.append((request.purpose, purpose))
        return RenderResult(True, "Suspiciously calm today.", "Suspiciously calm today.", "CPU: 2%; MEM: 1Gi", "rendered")

    monkeypatch.setattr(personality, "render", spy_render)
    monkeypatch.setattr(daemon, "_emit_chime", lambda *a, **kw: None)
    monkeypatch.setattr(daemon, "log_ambient_event", lambda *a, **kw: None)

    asyncio.run(_spin_worker_briefly())

    assert len(render_calls) >= 1
    assert render_calls[0][0] == "periodic_observation"
    assert render_calls[0][1] == Purpose.AMBIENT


async def _async_return(value):
    return value
