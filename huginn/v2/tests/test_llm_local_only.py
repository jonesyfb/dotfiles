"""The guarantee that local-only data (gatekeeper screenshots, activity
history) can never be routed to a cloud model.

judge_local_only() is the structural enforcement: it has no `prefer`
argument and calls _judge_ollama directly, with no branch anywhere in its
body that can reach _judge_claude. These tests exercise that at runtime
rather than trusting the source reads that way.
"""
import asyncio
import logging

import httpx

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
    from test_gatekeeper import _stub_common

    _stub_common(monkeypatch)

    calls = {"local_only": 0}

    async def fake_judge_local_only(prompt, images):
        calls["local_only"] += 1
        return '{"verdict": "approve", "confidence": 1.0, "message": "fine"}'

    monkeypatch.setattr(gatekeeper, "judge_local_only", fake_judge_local_only)

    result = asyncio.run(gatekeeper.check_gate("youtube"))

    assert calls["local_only"] == 1
    assert result["approved"] is True


# ── HTTP 400 classification (llm._judge_ollama / OllamaInvalidRequest) ─────


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        import json
        return json.loads(self.text)


def _fake_client_factory(response, captured=None):
    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            if captured is not None:
                captured["payload"] = json
            return response

    return _FakeClient


# The real body observed live against qwen3.8:27b: double-JSON-encoded —
# a top-level {"error": "<json string>"} whose decoded value is itself
# {"error": {"code", "type", "message", ...}}.
_REAL_400_BODY = (
    '{"error":"{\\"error\\":{\\"code\\":400,'
    '\\"message\\":\\"request (12219 tokens) exceeds the available context size (8192 tokens), try increasing it\\",'
    '\\"type\\":\\"exceed_context_size_error\\",\\"n_prompt_tokens\\":12219,\\"n_ctx\\":8192}}"}'
)


def test_parse_ollama_error_body_handles_double_encoded_body():
    category, detail = llm._parse_ollama_error_body(_REAL_400_BODY)
    assert category == "exceed_context_size_error"
    assert "exceeds the available context size" in detail


def test_parse_ollama_error_body_never_raises_on_garbage():
    category, detail = llm._parse_ollama_error_body("not json at all")
    assert category == "unknown"


def test_judge_ollama_raises_typed_error_on_http_400(monkeypatch):
    response = _FakeResponse(400, text=_REAL_400_BODY)
    monkeypatch.setattr(llm.httpx, "AsyncClient", _fake_client_factory(response))

    raised = False
    try:
        asyncio.run(llm._judge_ollama("prompt", []))
    except llm.OllamaInvalidRequest as e:
        raised = True
        assert e.status_code == 400
        assert e.category == "exceed_context_size_error"
    assert raised


def test_judge_ollama_http_400_logs_status_and_category_never_prompt_or_images(monkeypatch, caplog, tmp_path):
    response = _FakeResponse(400, text=_REAL_400_BODY)
    monkeypatch.setattr(llm.httpx, "AsyncClient", _fake_client_factory(response))

    image_path = tmp_path / "should-not-appear.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"SECRET_PIXELS" * 4)
    secret_prompt = "SECRET_ACTIVITY_LOG_CONTENTS_MUST_NOT_BE_LOGGED"
    with caplog.at_level(logging.WARNING, logger="huginn.llm"):
        try:
            asyncio.run(llm._judge_ollama(secret_prompt, [str(image_path)]))
        except llm.OllamaInvalidRequest:
            pass

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "exceed_context_size_error" in logged
    assert "400" in logged
    assert secret_prompt not in logged
    assert str(image_path) not in logged
    assert "SECRET_PIXELS" not in logged


def test_judge_ollama_does_not_raise_invalid_request_on_200(monkeypatch):
    response = _FakeResponse(200, text='{"message": {"content": "fine"}}')
    monkeypatch.setattr(llm.httpx, "AsyncClient", _fake_client_factory(response))

    result = asyncio.run(llm._judge_ollama("prompt", []))
    assert result == "fine"


def test_judge_ollama_sends_benchmark_aligned_options(monkeypatch):
    """Aligns production with the settings the vision model was actually
    validated against (scripts/vision_bench): think:false, temperature
    0.1, num_ctx 8192 — no seed (no demonstrated production need for
    deterministic output, unlike the benchmark's own reproducibility need)."""
    response = _FakeResponse(200, text='{"message": {"content": "ok"}}')
    captured: dict = {}
    monkeypatch.setattr(llm.httpx, "AsyncClient", _fake_client_factory(response, captured))

    asyncio.run(llm._judge_ollama("prompt", []))

    payload = captured["payload"]
    assert payload["think"] is False
    assert payload["options"]["temperature"] == 0.1
    assert payload["options"]["num_ctx"] == 8192
    assert "seed" not in payload["options"]


def test_gate_judge_http_400_is_classified_uncached_and_never_falls_back_to_cloud(monkeypatch):
    """End-to-end through check_gate: an invalid-request failure must
    surface as its own reason (not confused with a generic backend
    failure or a denial), must not be cached, and must never touch the
    cloud model."""
    import gatekeeper
    from test_gatekeeper import _stub_common

    _stub_common(monkeypatch)
    save_calls = {"n": 0}
    monkeypatch.setattr(gatekeeper, "save_verdict", lambda *a, **kw: save_calls.__setitem__("n", save_calls["n"] + 1))

    cloud_calls = {"n": 0}

    async def fake_claude(*a, **kw):
        cloud_calls["n"] += 1
        return "should never be reached"

    async def raising_judge(prompt, images):
        raise llm.OllamaInvalidRequest(400, "exceed_context_size_error", "too many tokens")

    monkeypatch.setattr(gatekeeper, "judge_local_only", raising_judge)
    monkeypatch.setattr(llm, "_judge_claude", fake_claude)

    result = asyncio.run(gatekeeper.check_gate("steam"))

    assert result["approved"] is False
    assert result["uncertain"] is True
    assert result["reason"] == "invalid_request"
    assert result["cached"] is False
    assert save_calls["n"] == 0
    assert cloud_calls["n"] == 0
