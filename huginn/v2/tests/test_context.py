"""Tests for the Runtime Context Engine (v2/context.py).

Covers: state normalization, missing/stale upstream data, manual overrides,
and local-model unavailability. The cloud-isolation guarantee lives in
test_llm_local_only.py since that's an llm.py property, not context.py's.
"""
import asyncio
import json

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
    tags = {"deepseek-r1:32b", "gemma4:31b"}
    statuses = context.collect_models(ollama_tags=tags)
    personality = statuses["personality"]
    assert personality.configured is True
    assert personality.available is False
    assert personality.reason == "model not pulled"


def test_models_distinguishes_sibling_tags_of_the_same_model_family():
    """qwen3.5:9b, qwen3.5:27b, and qwen3.5:4b all share the base name
    "qwen3.5" — matching by base name alone would report the personality
    model as pulled just because a different-size sibling is. Tags must be
    matched exactly."""
    tags = {"qwen3.5:9b", "qwen3.5:27b", "deepseek-r1:32b", "gemma4:31b"}
    statuses = context.collect_models(ollama_tags=tags)
    assert statuses["fast"].available is True       # qwen3.5:9b — pulled
    assert statuses["full"].available is True        # qwen3.5:27b — pulled
    assert statuses["personality"].available is False  # qwen3.5:4b — NOT pulled
    assert statuses["personality"].reason == "model not pulled"


def test_models_reports_available_when_pulled():
    tags = {"qwen3.5:9b", "qwen3.5:4b", "deepseek-r1:32b", "gemma4:31b"}
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
    assert snapshot.model_resources.ollama_reachable is False
    assert snapshot.model_resources.loaded_models == ()


# ── Attention: call suppression (slice 2) ────────────────────────────────────

def test_attention_do_not_disturb_during_discord_call(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "GAME_MODE_FLAG", tmp_path / "game-mode")
    interaction = context.collect_interaction()
    desktop = context.DesktopState(focused_window=None, in_discord_call=True)
    attention = context.collect_attention(interaction, desktop)
    assert attention.level == "do_not_disturb"


def test_attention_available_ignores_desktop_when_no_call(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "GAME_MODE_FLAG", tmp_path / "game-mode")
    interaction = context.collect_interaction()
    desktop = context.DesktopState(focused_window=None, in_discord_call=False)
    attention = context.collect_attention(interaction, desktop)
    assert attention.level == "available"


def test_attention_backward_compatible_without_desktop_arg(tmp_path, monkeypatch):
    """Slice-1 call sites that never learned about desktop state must keep
    working unchanged."""
    monkeypatch.setattr(context, "GAME_MODE_FLAG", tmp_path / "game-mode")
    interaction = context.collect_interaction()
    assert context.collect_attention(interaction).level == "available"


# ── Model resources (slice 2) ────────────────────────────────────────────────

def test_model_resources_marks_loaded_model(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "GAME_MODE_FLAG", tmp_path / "game-mode")
    interaction = context.collect_interaction()
    tags = {"qwen3.5:9b", "gemma4:31b"}
    loaded = [{"model": "qwen3.5:9b", "size": 1, "size_vram": 1}]
    models = context.collect_models(tags, loaded)
    assert models["fast"].loaded is True
    assert models["vision"].loaded is False

    resources = context.collect_model_resources(models, loaded, interaction)
    assert resources.loaded_models == ("qwen3.5:9b",)
    assert resources.contention is False  # only one distinct model resident
    assert resources.ollama_reachable is True


def test_model_resources_flags_contention_with_multiple_loaded(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "GAME_MODE_FLAG", tmp_path / "game-mode")
    interaction = context.collect_interaction()
    loaded = [{"model": "qwen3.5:9b"}, {"model": "gemma4:31b"}]
    models = context.collect_models({"qwen3.5:9b", "gemma4:31b"}, loaded)
    resources = context.collect_model_resources(models, loaded, interaction)
    assert resources.contention is True


def test_model_resources_swap_required_when_different_model_resident(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "GAME_MODE_FLAG", tmp_path / "game-mode")
    interaction = context.collect_interaction()
    loaded = [{"model": "gemma4:31b"}]  # vision resident, fast is not
    models = context.collect_models({"qwen3.5:9b", "gemma4:31b"}, loaded)
    resources = context.collect_model_resources(models, loaded, interaction)
    assert resources.swap_required["fast"] is True    # not resident, something else is
    assert resources.swap_required["vision"] is False  # already resident


def test_model_resources_unreachable_probe_reports_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "GAME_MODE_FLAG", tmp_path / "game-mode")
    interaction = context.collect_interaction()
    models = context.collect_models(None, None)
    resources = context.collect_model_resources(models, None, interaction)
    assert resources.ollama_reachable is False
    assert resources.loaded_models == ()


def test_model_resources_restricts_to_personality_in_game_mode(tmp_path, monkeypatch):
    flag = tmp_path / "game-mode"
    flag.touch()
    monkeypatch.setattr(context, "GAME_MODE_FLAG", flag)
    interaction = context.collect_interaction()
    resources = context.collect_model_resources({}, [], interaction)
    assert resources.game_mode_restricts_to_personality is True


def test_model_resources_no_restriction_outside_game_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "GAME_MODE_FLAG", tmp_path / "game-mode")
    interaction = context.collect_interaction()
    resources = context.collect_model_resources({}, [], interaction)
    assert resources.game_mode_restricts_to_personality is False


def test_model_resources_always_prohibits_cloud_for_local_only(tmp_path, monkeypatch):
    """This field just restates llm.judge_local_only's structural guarantee
    for the debug view — it should never read False."""
    monkeypatch.setattr(context, "GAME_MODE_FLAG", tmp_path / "game-mode")
    interaction = context.collect_interaction()
    resources = context.collect_model_resources({}, [], interaction)
    assert resources.cloud_prohibited_for_local_only is True


def test_probe_ollama_loaded_fails_soft(monkeypatch):
    class _BoomClient:
        def __init__(self, *a, **kw):
            raise ConnectionError("ollama down")

    monkeypatch.setattr(context.httpx, "AsyncClient", _BoomClient)
    assert asyncio.run(context.probe_ollama_loaded()) is None


# ── Debug snapshot redaction (slice 2) ───────────────────────────────────────

def test_debug_dict_redacts_window_title(tmp_path, monkeypatch):
    monkeypatch.setattr(context, "GAME_MODE_FLAG", tmp_path / "game-mode")
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")

    interaction = context.collect_interaction()
    desktop = context.DesktopState(
        focused_window={"app_id": "brave-browser", "title": "very private banking session"},
        in_discord_call=False,
        evidence=("focused: brave-browser — very private banking session",),
    )
    attention = context.collect_attention(interaction, desktop)
    task = context.TaskState("idle", 0)
    models = context.collect_models(set())
    tools = context.collect_tools(set())
    resources = context.collect_model_resources(models, [], interaction)
    snapshot = context.RuntimeContext(0.0, interaction, attention, task, models, tools, desktop, resources, {})

    payload = context.to_debug_dict(snapshot)
    dumped = json.dumps(payload)

    assert "very private banking session" not in dumped
    assert payload["desktop"]["focused_app_id"] == "brave-browser"


def test_debug_dict_never_contains_screenshot_or_activity_keys(tmp_path, monkeypatch):
    """context.py never collects screenshots or raw activity history in the
    first place (that's gatekeeper.py's job) — this pins that invariant so a
    future change to context.py can't accidentally start threading it
    through into the debug snapshot."""
    monkeypatch.setattr(context, "GAME_MODE_FLAG", tmp_path / "game-mode")
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")

    class _BoomClient:
        def __init__(self, *a, **kw):
            raise ConnectionError("ollama down")

    monkeypatch.setattr(context.httpx, "AsyncClient", _BoomClient)
    monkeypatch.setattr(context.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))

    snapshot = asyncio.run(context.collect())
    dumped = json.dumps(context.to_debug_dict(snapshot))
    for forbidden in ("screenshot", "activity_log", "screens/"):
        assert forbidden not in dumped


def test_debug_dict_marks_manual_override(tmp_path, monkeypatch):
    flag = tmp_path / "game-mode"
    flag.touch()
    monkeypatch.setattr(context, "GAME_MODE_FLAG", flag)
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "test.db")

    class _BoomClient:
        def __init__(self, *a, **kw):
            raise ConnectionError("ollama down")

    monkeypatch.setattr(context.httpx, "AsyncClient", _BoomClient)
    monkeypatch.setattr(context.subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(FileNotFoundError()))

    snapshot = asyncio.run(context.collect())
    payload = context.to_debug_dict(snapshot)
    assert payload["overrides_active"] == ["game"]
    assert payload["interaction"]["basis"].startswith("manual")


def test_debug_dict_never_exposes_credential_value(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-super-secret-value")
    models = context.collect_models(set())
    payload_models = {
        key: {"backend": s.backend, "model": s.model, "configured": s.configured,
              "available": s.available, "loaded": s.loaded, "reason": s.reason}
        for key, s in models.items()
    }
    assert "sk-ant" not in json.dumps(payload_models)
