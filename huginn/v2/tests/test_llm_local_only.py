"""The guarantee that local-only data (gatekeeper screenshots, activity
history) can never be routed to a cloud model.

judge_local_only() is the structural enforcement: it has no `prefer`
argument and calls _judge_ollama directly, with no branch anywhere in its
body that can reach _judge_claude. These tests exercise that at runtime
rather than trusting the source reads that way.
"""
import asyncio

import llm


def test_judge_local_only_never_calls_judge_claude_on_success(monkeypatch):
    calls = {"claude": 0, "ollama": 0}

    async def fake_ollama(prompt, image_paths):
        calls["ollama"] += 1
        return "local response"

    async def fake_claude(prompt, image_paths):
        calls["claude"] += 1
        raise AssertionError("judge_local_only must never call _judge_claude")

    monkeypatch.setattr(llm, "_judge_ollama", fake_ollama)
    monkeypatch.setattr(llm, "_judge_claude", fake_claude)

    result = asyncio.run(llm.judge_local_only("prompt", ["/tmp/shot.png"]))

    assert result == "local response"
    assert calls == {"claude": 0, "ollama": 1}


def test_judge_local_only_does_not_fall_back_to_cloud_on_local_failure(monkeypatch):
    """judge_once's prefer="cloud" path falls back to local on failure — the
    local-only path must NOT have the mirror image of that behavior. A
    failed local judgment must raise, never quietly escalate to cloud."""
    calls = {"claude": 0}

    async def failing_ollama(prompt, image_paths):
        raise RuntimeError("ollama unreachable")

    async def fake_claude(prompt, image_paths):
        calls["claude"] += 1
        return "should never get here"

    monkeypatch.setattr(llm, "_judge_ollama", failing_ollama)
    monkeypatch.setattr(llm, "_judge_claude", fake_claude)

    try:
        asyncio.run(llm.judge_local_only("prompt", []))
        raised = False
    except llm.CoordinatorDenied as e:
        raised = True
        assert "ollama unreachable" in e.detail

    assert raised, "judge_local_only swallowed a local failure instead of raising"
    assert calls["claude"] == 0


def test_judge_local_only_has_no_prefer_parameter():
    """A `prefer` kwarg would reintroduce a way to accidentally route
    local-only data to cloud. Assert the signature can't take one."""
    import inspect

    params = inspect.signature(llm.judge_local_only).parameters
    assert "prefer" not in params


def test_gatekeeper_check_gate_uses_judge_local_only(monkeypatch):
    """Regression guard: gatekeeper must call the structurally-safe
    function, not judge_once (which defaults to prefer="cloud")."""
    import gatekeeper

    calls = {"local_only": 0}

    async def fake_judge_local_only(prompt, images):
        calls["local_only"] += 1
        return '{"approved": true, "message": "fine"}'

    from evidence import EvidenceCheck

    monkeypatch.setattr(gatekeeper, "judge_local_only", fake_judge_local_only)
    monkeypatch.setattr(gatekeeper, "last_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "recent_screenshots", lambda *a, **kw: ["/fake/screenshot.png"])
    monkeypatch.setattr(gatekeeper, "recent_verdicts", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "activity_since", lambda *a, **kw: [])
    monkeypatch.setattr(gatekeeper, "save_verdict", lambda *a, **kw: None)
    monkeypatch.setattr(gatekeeper, "in_discord_call", lambda: False)
    monkeypatch.setattr(
        gatekeeper, "validate_screenshots",
        lambda paths, **kw: EvidenceCheck(True, tuple(paths), None, "stubbed valid"),
    )

    result = asyncio.run(gatekeeper.check_gate("youtube"))

    assert calls["local_only"] == 1
    assert result["approved"] is True
