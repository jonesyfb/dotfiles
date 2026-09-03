"""Tests for the local-inference coordinator (v2/coordinator.py).

Covers: priority ordering, cancellation/deadlines, starvation prevention,
stale lock-owner recovery, live external lock contention, game-mode
personality-only enforcement (at admission AND mid-run), preemption of a
running request by foreground arrival, stale-ambient expiry (not aging),
direct-vs-ambient priority on the same request class, every Huginn Ollama
call site routing through the coordinator, redacted diagnostics, and
coordinator failure isolation (a bad request doesn't take the daemon down).

The claim that cancelling the coordinator's task actually stops server-side
Ollama generation was verified live against real Ollama (GPU busy% dropped
96% -> 1% within ~1s of cancelling the client connection — see the slice-3
commit message for the full measurement). That can't be exercised in a fast
unit test without a running Ollama + GPU, so the tests here instead prove
the mechanism that makes that live behavior possible: cancelling the
coordinator's task reaches the actual awaitable doing the work.
"""
import asyncio
import fcntl
import json
import time

import pytest

import coordinator as coordinator_module
from coordinator import (
    Coordinator, DeadlineExceeded, Denial, InferenceRequest,
    Purpose, RequestClass, _QueueItem, coordinator,
)


def _req(request_class, purpose, label="x", model="test-model", deadline_seconds=None, fn=None):
    async def _default_fn(emit):
        return label

    return InferenceRequest(
        request_class=request_class, purpose=purpose, model=model,
        fn=fn or _default_fn, deadline_seconds=deadline_seconds, label=label,
    )


def _item(c, request, enqueued_at=None):
    c._seq += 1
    return _QueueItem(request, asyncio.Queue(), enqueued_at if enqueued_at is not None else time.monotonic(), c._seq)


async def _drain(agen):
    events = []
    async for ev in agen:
        events.append(ev)
    return events


# ── Priority ordering ─────────────────────────────────────────────────────────

def test_priority_ordering_direct_before_gate_before_ambient():
    c = Coordinator()
    now = time.monotonic()
    ambient_item = _item(c, _req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.AMBIENT), now)
    gate_item = _item(c, _req(RequestClass.LOCAL_VISION_GATEKEEPER, Purpose.GATE_DECISION), now)
    direct_item = _item(c, _req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.DIRECT), now)
    c._queue = [ambient_item, gate_item, direct_item]

    assert c._pop_best_locked() is direct_item
    assert c._pop_best_locked() is gate_item
    assert c._pop_best_locked() is ambient_item
    assert c._pop_best_locked() is None


def test_critical_outranks_direct():
    c = Coordinator()
    now = time.monotonic()
    direct_item = _item(c, _req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.DIRECT), now)
    critical_item = _item(c, _req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.CRITICAL), now)
    c._queue = [direct_item, critical_item]
    assert c._pop_best_locked() is critical_item


def test_maintenance_is_lowest_priority():
    c = Coordinator()
    now = time.monotonic()
    ambient_item = _item(c, _req(RequestClass.MAINTENANCE_BACKGROUND, Purpose.AMBIENT), now)
    maint_item = _item(c, _req(RequestClass.MODEL_LOAD_UNLOAD, Purpose.MAINTENANCE), now)
    c._queue = [maint_item, ambient_item]
    assert c._pop_best_locked() is ambient_item
    assert c._pop_best_locked() is maint_item


def test_direct_outranks_ambient_for_the_same_request_class():
    """Purpose is independent of RequestClass — the same model target
    (RESIDENT_PERSONALITY) must still prioritize a direct user response
    over an unsolicited ambient chime using the identical model."""
    c = Coordinator()
    now = time.monotonic()
    ambient_item = _item(c, _req(RequestClass.RESIDENT_PERSONALITY, Purpose.AMBIENT, "chime"), now)
    direct_item = _item(c, _req(RequestClass.RESIDENT_PERSONALITY, Purpose.DIRECT, "chat"), now)
    c._queue = [ambient_item, direct_item]
    popped = c._pop_best_locked()
    assert popped is direct_item
    assert popped.request.request_class == ambient_item.request.request_class
    assert popped.request.purpose != ambient_item.request.purpose


# ── Starvation prevention (aging) — GATE_DECISION only ────────────────────────

def test_gate_decision_ages_toward_direct_priority_and_wins_fifo_tie():
    c = Coordinator()
    now = time.monotonic()
    interval = coordinator_module.GATE_DECISION_AGING_INTERVAL_SECONDS
    old_gate = _item(c, _req(RequestClass.LOCAL_VISION_GATEKEEPER, Purpose.GATE_DECISION, "old"), now - interval * 3)
    new_direct = _item(c, _req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.DIRECT, "new"), now)
    # old_gate enqueued (lower seq) before new_direct, and has aged to tie
    # DIRECT's tier — FIFO (seq) then favors it, proving it isn't starved.
    c._queue = [old_gate, new_direct]
    assert c._pop_best_locked() is old_gate


def test_gate_decision_aging_never_reaches_critical_tier():
    c = Coordinator()
    now = time.monotonic()
    interval = coordinator_module.GATE_DECISION_AGING_INTERVAL_SECONDS
    ancient_gate = _item(c, _req(RequestClass.LOCAL_VISION_GATEKEEPER, Purpose.GATE_DECISION), now - interval * 1000)
    priority, _ = c._effective_priority(ancient_gate, now)
    assert priority == coordinator_module._BASE_PRIORITY[Purpose.DIRECT]  # floored, not below


def test_direct_and_ambient_do_not_age():
    c = Coordinator()
    now = time.monotonic()
    ancient_ambient = _item(c, _req(RequestClass.MAINTENANCE_BACKGROUND, Purpose.AMBIENT), now - 100000)
    priority, _ = c._effective_priority(ancient_ambient, now)
    assert priority == coordinator_module._BASE_PRIORITY[Purpose.AMBIENT]


# ── Stale ambient/maintenance requests expire, never gain priority ────────────

def test_ambient_expires_rather_than_escalating():
    c = Coordinator()
    now = time.monotonic()
    max_wait = coordinator_module._EXPIRY_SECONDS[Purpose.AMBIENT]
    stale = _item(c, _req(RequestClass.MAINTENANCE_BACKGROUND, Purpose.AMBIENT), now - max_wait - 1)
    fresh = _item(c, _req(RequestClass.MAINTENANCE_BACKGROUND, Purpose.AMBIENT), now)
    c._queue = [stale, fresh]
    c._drop_expired_locked()
    assert c._queue == [fresh]
    ev = stale.events.get_nowait()
    assert ev.kind == "denied" and ev.denial == Denial.EXPIRED


def test_maintenance_expires_too():
    c = Coordinator()
    now = time.monotonic()
    max_wait = coordinator_module._EXPIRY_SECONDS[Purpose.MAINTENANCE]
    stale = _item(c, _req(RequestClass.MODEL_LOAD_UNLOAD, Purpose.MAINTENANCE), now - max_wait - 1)
    c._queue = [stale]
    c._drop_expired_locked()
    assert c._queue == []


def test_gate_decision_and_direct_never_expire_from_the_queue():
    c = Coordinator()
    now = time.monotonic()
    old_gate = _item(c, _req(RequestClass.LOCAL_VISION_GATEKEEPER, Purpose.GATE_DECISION), now - 999999)
    old_direct = _item(c, _req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.DIRECT), now - 999999)
    c._queue = [old_gate, old_direct]
    c._drop_expired_locked()
    assert old_gate in c._queue and old_direct in c._queue
    assert len(c._queue) == 2


# ── Bounded queueing ───────────────────────────────────────────────────────────

def test_make_room_evicts_disposable_for_foreground_arrival():
    c = Coordinator()
    now = time.monotonic()
    a1 = _item(c, _req(RequestClass.MAINTENANCE_BACKGROUND, Purpose.AMBIENT, "a1"), now)
    a2 = _item(c, _req(RequestClass.MODEL_LOAD_UNLOAD, Purpose.MAINTENANCE, "a2"), now)
    c._queue = [a1, a2]
    made_room = c._make_room_locked(_req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.DIRECT, "direct"))
    assert made_room is True
    assert a2 not in c._queue and a1 in c._queue  # worse (lower) priority evicted first
    ev = a2.events.get_nowait()
    assert ev.denial == Denial.QUEUE_FULL


def test_make_room_denies_when_nothing_disposable_to_evict():
    c = Coordinator()
    d1 = _item(c, _req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.DIRECT, "d1"))
    c._queue = [d1]
    assert c._make_room_locked(_req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.DIRECT, "d2")) is False


def test_make_room_never_evicts_for_a_background_arrival():
    c = Coordinator()
    a1 = _item(c, _req(RequestClass.MAINTENANCE_BACKGROUND, Purpose.AMBIENT, "a1"))
    c._queue = [a1]
    made_room = c._make_room_locked(_req(RequestClass.MODEL_LOAD_UNLOAD, Purpose.MAINTENANCE, "a2"))
    assert made_room is False
    assert a1 in c._queue


# ── Bounded, cancellation-aware external flock (Garage Watch compatibility) ──

def test_deadline_expires_while_external_lock_held(tmp_path, monkeypatch):
    """Live external contention: a real separate file descriptor holds the
    actual OS flock (standing in for Garage Watch), and acquisition must
    give up at the deadline rather than blocking indefinitely."""
    lock_path = tmp_path / "ollama.lock"
    monkeypatch.setattr(coordinator_module, "_OLLAMA_LOCK_PATH", str(lock_path))
    external = open(lock_path, "w")
    fcntl.flock(external.fileno(), fcntl.LOCK_EX)
    try:
        start = time.monotonic()
        with pytest.raises(DeadlineExceeded):
            asyncio.run(coordinator_module._acquire_shared_lock(0.2))
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, "should fail close to the deadline, not hang"
    finally:
        fcntl.flock(external.fileno(), fcntl.LOCK_UN)
        external.close()


def test_acquire_succeeds_once_external_lock_releases(tmp_path, monkeypatch):
    lock_path = tmp_path / "ollama.lock"
    monkeypatch.setattr(coordinator_module, "_OLLAMA_LOCK_PATH", str(lock_path))
    external = open(lock_path, "w")
    fcntl.flock(external.fileno(), fcntl.LOCK_EX)

    async def _release_soon():
        await asyncio.sleep(0.1)
        fcntl.flock(external.fileno(), fcntl.LOCK_UN)
        external.close()

    async def _run():
        asyncio.ensure_future(_release_soon())
        f = await coordinator_module._acquire_shared_lock(2.0)
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()

    asyncio.run(_run())  # must not raise DeadlineExceeded


def test_lock_releases_automatically_if_holder_closes_without_unlocking(tmp_path, monkeypatch):
    """flock is tied to the open file description, not an explicit unlock —
    the OS releases it on close/process-exit regardless (this is what makes
    a crashed Garage Watch or Huginn process safe: no stale-lock recovery
    logic is needed, the kernel already handles it)."""
    lock_path = tmp_path / "ollama.lock"
    monkeypatch.setattr(coordinator_module, "_OLLAMA_LOCK_PATH", str(lock_path))
    external = open(lock_path, "w")
    fcntl.flock(external.fileno(), fcntl.LOCK_EX)
    external.close()  # no LOCK_UN — simulates a crash

    f = asyncio.run(coordinator_module._acquire_shared_lock(1.0))
    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    f.close()


# ── Game-mode admission ────────────────────────────────────────────────────────

def test_game_mode_denies_non_personality_classes():
    c = Coordinator()
    c.set_game_mode_check(lambda: True)
    for rc in (RequestClass.ORDINARY_LOCAL_REASONING, RequestClass.LOCAL_VISION_GATEKEEPER, RequestClass.MAINTENANCE_BACKGROUND):
        denial = c._admission_denial(_req(rc, Purpose.DIRECT))
        assert denial is not None and denial[0] == Denial.GAME_MODE


def test_game_mode_allows_personality_classes():
    c = Coordinator()
    c.set_game_mode_check(lambda: True)
    for rc in (RequestClass.RESIDENT_PERSONALITY, RequestClass.GAME_MODE_PERSONALITY_ONLY):
        assert c._admission_denial(_req(rc, Purpose.DIRECT)) is None


def test_game_mode_allows_model_load_unload_class():
    c = Coordinator()
    c.set_game_mode_check(lambda: True)
    assert c._admission_denial(_req(RequestClass.MODEL_LOAD_UNLOAD, Purpose.MAINTENANCE)) is None


def test_submit_denies_immediately_in_game_mode_without_queueing():
    async def _run():
        coordinator.set_game_mode_check(lambda: True)
        events = await _drain(coordinator.submit(_req(RequestClass.LOCAL_VISION_GATEKEEPER, Purpose.GATE_DECISION)))
        assert len(events) == 1
        assert events[0].kind == "denied" and events[0].denial == Denial.GAME_MODE
        assert coordinator._queue == []
    asyncio.run(_run())


def test_game_mode_begins_while_request_is_queued_denies_at_run_time():
    """State may change between admission and execution — re-checked
    immediately before running, not trusted from queue time."""
    async def _run():
        game_mode = {"on": False}
        coordinator.set_game_mode_check(lambda: game_mode["on"])

        occupy_gate = asyncio.Event()

        async def occupying_fn(emit):
            await occupy_gate.wait()
            return "occupied"

        occupy_task = asyncio.ensure_future(_drain(coordinator.submit(
            _req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.DIRECT, "occupy", fn=occupying_fn)
        )))
        await asyncio.sleep(0.05)  # let the scheduler pick it up and start running

        async def vision_fn(emit):
            return "should not run"

        vision_task = asyncio.ensure_future(_drain(coordinator.submit(
            _req(RequestClass.LOCAL_VISION_GATEKEEPER, Purpose.GATE_DECISION, "vision", fn=vision_fn)
        )))
        await asyncio.sleep(0.05)  # let it enqueue behind the occupying item

        game_mode["on"] = True
        occupy_gate.set()  # free the slot; scheduler should now deny vision at run time

        vision_events = await asyncio.wait_for(vision_task, timeout=2.0)
        await asyncio.wait_for(occupy_task, timeout=2.0)

        assert vision_events[-1].kind == "denied"
        assert vision_events[-1].denial == Denial.GAME_MODE

    asyncio.run(_run())


def test_game_mode_begins_while_prohibited_request_is_running_gets_cancelled(monkeypatch):
    monkeypatch.setattr(coordinator_module, "GAME_MODE_POLL_SECONDS", 0.02)

    async def _run():
        game_mode = {"on": False}
        coordinator.set_game_mode_check(lambda: game_mode["on"])
        cancelled = asyncio.Event()

        async def long_fn(emit):
            try:
                await asyncio.sleep(5)
                return "finished"
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.ensure_future(_drain(coordinator.submit(
            _req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.DIRECT, "long", fn=long_fn)
        )))
        await asyncio.sleep(0.05)  # let it start running
        game_mode["on"] = True

        events = await asyncio.wait_for(task, timeout=2.0)
        await asyncio.wait_for(cancelled.wait(), timeout=1.0)
        assert events[-1].kind == "denied"
        assert events[-1].denial == Denial.PREEMPTED

    asyncio.run(_run())


# ── Preemption: foreground arrival while something is already running ───────

def test_foreground_direct_preempts_a_running_gate_decision():
    async def _run():
        cancelled = asyncio.Event()

        async def vision_fn(emit):
            try:
                await asyncio.sleep(5)
                return "finished"
            except asyncio.CancelledError:
                cancelled.set()
                raise

        vtask = asyncio.ensure_future(_drain(coordinator.submit(
            _req(RequestClass.LOCAL_VISION_GATEKEEPER, Purpose.GATE_DECISION, "vision", fn=vision_fn)
        )))
        await asyncio.sleep(0.05)  # let it start running and occupy the slot

        async def direct_fn(emit):
            return "chat reply"

        dtask = asyncio.ensure_future(_drain(coordinator.submit(
            _req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.DIRECT, "chat", fn=direct_fn)
        )))

        vision_events = await asyncio.wait_for(vtask, timeout=2.0)
        direct_events = await asyncio.wait_for(dtask, timeout=2.0)
        await asyncio.wait_for(cancelled.wait(), timeout=1.0)

        assert vision_events[-1].denial == Denial.PREEMPTED
        assert direct_events[-1].kind == "done" and direct_events[-1].value == "chat reply"

    asyncio.run(_run())


def test_preemption_reaches_the_actual_running_awaitable():
    """Mechanism check: cancellation must propagate into the real coroutine
    doing the work, not just mark bookkeeping — this is what makes the
    live-verified GPU release (96% -> 1% busy within ~1s of cancelling the
    client connection) actually happen in production."""
    async def _run():
        reached = asyncio.Event()
        was_cancelled = asyncio.Event()

        async def stands_in_for_httpx_call(emit):
            reached.set()
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                was_cancelled.set()
                raise

        task = asyncio.ensure_future(_drain(coordinator.submit(
            _req(RequestClass.LOCAL_VISION_GATEKEEPER, Purpose.GATE_DECISION, "vision", fn=stands_in_for_httpx_call)
        )))
        await asyncio.wait_for(reached.wait(), timeout=1.0)

        async def direct_fn(emit):
            return "ok"

        await _drain(coordinator.submit(_req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.DIRECT, fn=direct_fn)))

        await asyncio.wait_for(was_cancelled.wait(), timeout=1.0)
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_run())


def test_direct_and_critical_are_never_preempted_once_running():
    async def _run():
        ran_to_completion = asyncio.Event()

        async def direct_fn(emit):
            await asyncio.sleep(0.1)
            ran_to_completion.set()
            return "done"

        task = asyncio.ensure_future(_drain(coordinator.submit(
            _req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.DIRECT, "first", fn=direct_fn)
        )))
        await asyncio.sleep(0.02)

        # A second, even-higher-priority arrival must NOT cancel it.
        async def critical_fn(emit):
            return "critical"

        await _drain(coordinator.submit(_req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.CRITICAL, fn=critical_fn)))

        events = await asyncio.wait_for(task, timeout=2.0)
        assert ran_to_completion.is_set()
        assert events[-1].kind == "done" and events[-1].value == "done"

    asyncio.run(_run())


# ── Deadlines ──────────────────────────────────────────────────────────────────

def test_gate_decision_deadline_exceeded_while_running():
    async def _run():
        async def slow_fn(emit):
            await asyncio.sleep(5)
            return "too slow"

        events = await _drain(coordinator.submit(
            _req(RequestClass.LOCAL_VISION_GATEKEEPER, Purpose.GATE_DECISION, "vision", deadline_seconds=0.1, fn=slow_fn)
        ))
        assert events[-1].kind == "denied"
        assert events[-1].denial == Denial.DEADLINE_EXCEEDED

    asyncio.run(_run())


# ── Model state changing while queued (re-check before execution) ────────────

def test_unload_rechecks_residency_immediately_before_executing(monkeypatch):
    import context
    import llm

    async def fake_probe():
        return []  # no longer resident by the time the coordinator actually runs this

    monkeypatch.setattr(context, "probe_ollama_loaded", fake_probe)

    raw_calls = {"n": 0}

    async def fake_raw(model):
        raw_calls["n"] += 1

    monkeypatch.setattr(llm, "_unload_model_raw", fake_raw)

    asyncio.run(llm.unload_model("gemma4:31b"))

    assert raw_calls["n"] == 0, "re-check found it already gone; raw unload should be skipped"


# ── Every Huginn Ollama call site routes through the coordinator ─────────────

def test_all_ollama_entrypoints_route_through_the_coordinator(monkeypatch):
    import context
    import llm
    import tools

    calls = []
    real_submit = coordinator.submit

    async def spy_submit(request):
        calls.append(request.label or request.request_class.value)
        async for ev in real_submit(request):
            yield ev

    monkeypatch.setattr(coordinator, "submit", spy_submit)

    async def fake_stream_ollama(model, messages, tools_):
        yield {"type": "token", "content": "hi"}
        yield {"type": "done"}

    monkeypatch.setattr(llm, "stream_ollama", fake_stream_ollama)

    async def fake_judge_ollama(prompt, images):
        return "ok"

    monkeypatch.setattr(llm, "_judge_ollama", fake_judge_ollama)

    async def fake_probe_loaded():
        return []

    monkeypatch.setattr(context, "probe_ollama_loaded", fake_probe_loaded)

    class _FakeResp:
        def json(self):
            return {"embeddings": [[0.1, 0.2]]}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return _FakeResp()

    monkeypatch.setattr(tools.httpx, "AsyncClient", lambda **kw: _FakeClient())

    async def _run():
        async for _ in llm.stream_chat([{"role": "user", "content": "hi"}], "fast"):
            pass
        await llm.judge_local_only("prompt", [])
        await llm.unload_model("some:model")
        await tools._get_embedding("hello")

    asyncio.run(_run())

    assert "chat" in calls
    assert "gatekeeper-judge" in calls
    assert "unload-if-resident" in calls
    assert "embed" in calls


# ── Redacted diagnostics ───────────────────────────────────────────────────────

def test_snapshot_never_includes_request_content():
    c = Coordinator()

    async def _fn(emit):
        return "the actual judgment text should never appear in diagnostics"

    req = InferenceRequest(
        RequestClass.LOCAL_VISION_GATEKEEPER, Purpose.GATE_DECISION, "gemma4:31b", _fn,
        label="gatekeeper-judge",
    )
    c._queue = [_item(c, req)]
    dumped = json.dumps(c.snapshot())

    assert "judgment text" not in dumped
    snap = c.snapshot()
    assert snap["queued"][0]["request_class"] == "local_vision_gatekeeper"
    assert snap["queued"][0]["model"] == "gemma4:31b"
    assert "waited_seconds" in snap["queued"][0]


def test_snapshot_reports_running_item():
    async def _run():
        gate = asyncio.Event()

        async def _fn(emit):
            await gate.wait()

        task = asyncio.ensure_future(_drain(coordinator.submit(
            _req(RequestClass.LOCAL_VISION_GATEKEEPER, Purpose.GATE_DECISION, "vision", fn=_fn)
        )))
        await asyncio.sleep(0.05)
        snap = coordinator.snapshot()
        assert snap["running"] is not None
        assert snap["running"]["request_class"] == "local_vision_gatekeeper"
        assert "running_for_seconds" in snap["running"]
        gate.set()
        await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(_run())


# ── Coordinator failure isolation ─────────────────────────────────────────────

def test_unexpected_exception_denies_cleanly_without_raising():
    async def _run():
        async def boom_fn(emit):
            raise ValueError("boom")

        events = await _drain(coordinator.submit(_req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.DIRECT, fn=boom_fn)))
        assert events[-1].kind == "denied"
        assert events[-1].denial == Denial.ERROR
        assert "boom" in events[-1].detail

    asyncio.run(_run())


def test_scheduler_survives_a_failed_request_and_serves_the_next_one():
    async def _run():
        async def boom_fn(emit):
            raise ValueError("boom")

        async def ok_fn(emit):
            return "fine"

        await _drain(coordinator.submit(_req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.DIRECT, fn=boom_fn)))
        events = await _drain(coordinator.submit(_req(RequestClass.ORDINARY_LOCAL_REASONING, Purpose.DIRECT, fn=ok_fn)))
        assert events[-1].kind == "done" and events[-1].value == "fine"

    asyncio.run(_run())
