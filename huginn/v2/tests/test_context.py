"""Tests for the Runtime Context Engine (v2/context.py).

Covers: state normalization, missing/stale upstream data, manual overrides,
and local-model unavailability. The cloud-isolation guarantee lives in
test_llm_local_only.py since that's an llm.py property, not context.py's.
"""
import asyncio

import context
import memory


def test_interaction_defaults_to_ambient(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "GAME_MODE_FLAG", tmp_path / "game-mode")
    state = context.collect_interaction()
    assert state.mode == "ambient"
    assert state.interruptions_allowed is True


def test_interaction_normalizes_flag_file_to_game_mode(tmp_path, monkeypatch):
    flag = tmp_path / "game-mode"
    flag.touch()
    monkeypatch.setattr(context, "GAME_MODE_FLAG", flag)
    state = context.collect_interaction()
    assert state.mode == "game"
    assert state.interruptions_allowed is False
    assert "game-mode flag file" in state.source


def test_manual_override_forces_do_not_disturb_regardless_of_anything_else(tmp_path, monkeypatch):
    """The manual flag file is the only signal that exists today — attention
    must follow it even though nothing else (no meeting/focus detector) is
    implemented yet."""
    flag = tmp_path / "game-mode"
    flag.touch()
    monkeypatch.setattr(context, "GAME_MODE_FLAG", flag)

    interaction = context.collect_interaction()
    attention = context.collect_attention(interaction)

    assert attention.level == "do_not_disturb"
    assert attention.evidence == interaction.evidence


def test_attention_available_when_no_override(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "GAME_MODE_FLAG", tmp_path / "game-mode")
    interaction = context.collect_interaction()
    attention = context.collect_attention(interaction)
    assert attention.level == "available"


def test_models_missing_data_when_ollama_unreachable():
    """probe_ollama_tags returning None (Ollama down/unreachable) must not be
    silently treated as "no models installed" — every ollama-backed entry
    should report available=None with a reason, not a confident False."""
    statuses = context.collect_models(ollama_tags=None)
    for key, spec in context.MODELS.items():
        if spec["backend"] == "ollama":
            assert statuses[key].available is None
            assert "unreachable" in statuses[key].reason


def test_models_reports_local_model_unavailability_when_not_pulled():
    """qwen3.5:4b (the new personality model) may not be pulled yet — that
    must show up as a concrete, honest "not pulled" status, not a crash and
    not a false "available"."""
    # Excludes "qwen3.5" entirely so the personality model's base name misses.
    tags = {"deepseek-r1", "gemma4"}
    statuses = context.collect_models(ollama_tags=tags)
    personality = statuses["personality"]
    assert personality.configured is True
    assert personality.available is False
    assert personality.reason == "model not pulled"


def test_models_reports_available_when_pulled():
    tags = {"qwen3.5", "deepseek-r1", "gemma4"}
    statuses = context.collect_models(ollama_tags=tags)
    assert statuses["personality"].available is True
    assert statuses["fast"].available is True


def test_models_claude_backend_never_exposes_key_value(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-super-secret-value")
    statuses = context.collect_models(ollama_tags=set())
    cloud = statuses["cloud"]
    assert cloud.available is True
    # Only a presence bool and a fixed reason string are ever recorded —
    # nothing here should be able to leak the key value itself.
    assert "sk-ant" not in repr(cloud)
    assert "sk-ant" not in cloud.reason


def test_models_claude_backend_reports_missing_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    statuses = context.collect_models(ollama_tags=set())
    assert statuses["cloud"].available is None
    assert "ANTHROPIC_API_KEY" in statuses["cloud"].reason


def test_tools_availability_tracks_ollama_reachability():
    assert context.collect_tools(ollama_tags=None).ollama_reachable is False
    assert context.collect_tools(ollama_tags=set()).ollama_reachable is True


def test_task_state_idle_with_empty_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")
    state = context.collect_task()
    assert state.state == "idle"
    assert state.queued_tasks == 0


def test_task_state_reports_queued_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")
    memory.enqueue_task("t1", "test task", "echo hi")
    state = context.collect_task()
    assert state.state == "tasks_queued"
    assert state.queued_tasks == 1


def test_task_state_reports_running_over_queued(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")
    memory.enqueue_task("t1", "running task", "echo hi")
    memory.update_task_status("t1", "running")
    memory.enqueue_task("t2", "queued task", "echo bye")
    state = context.collect_task()
    assert state.state == "tool_running"
    assert state.queued_tasks == 1


def test_desktop_state_fails_soft_when_niri_and_pactl_unavailable(monkeypatch):
    def _boom(*a, **kw):
        raise FileNotFoundError("no such command")

    monkeypatch.setattr(context.subprocess, "run", _boom)
    desktop = context.collect_desktop()
    assert desktop.focused_window is None
    assert desktop.in_discord_call is False


def test_probe_ollama_tags_fails_soft_and_returns_none(monkeypatch):
    """probe_ollama_tags must never raise — an unreachable Ollama is reported
    as unknown (None), not crash the whole context collection."""
    class _BoomClient:
        def __init__(self, *a, **kw):
            raise ConnectionError("ollama down")

    monkeypatch.setattr(context.httpx, "AsyncClient", _BoomClient)
    result = asyncio.run(context.probe_ollama_tags())
    assert result is None


def test_full_snapshot_collects_without_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "GAME_MODE_FLAG", tmp_path / "game-mode")
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")

    class _BoomClient:
        def __init__(self, *a, **kw):
            raise ConnectionError("ollama down")

    monkeypatch.setattr(context.httpx, "AsyncClient", _BoomClient)
    monkeypatch.setattr(context.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))

    snapshot = asyncio.run(context.collect())
    assert snapshot.interaction.mode == "ambient"
    assert snapshot.task.state == "idle"
    assert snapshot.tools.ollama_reachable is False
    assert snapshot.desktop.focused_window is None
