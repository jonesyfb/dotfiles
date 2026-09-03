"""
Local-inference coordinator.

Replaces the old global `ollama_lock()` (a bare, unbounded, priority-blind
flock) with an explicit scheduler that decides which of Huginn's own
requests gets the machine's one real inference slot next — and can cancel
a running low-priority one when foreground work arrives.

Why a single slot, not a pool: measured on this box, `OLLAMA_MAX_LOADED_MODELS=1`
(server config) means Ollama can only ever have one model resident, and even
concurrent requests to the *same* already-loaded model degrade each other
substantially (measured: 11.7s solo -> 42-53s each when two ran at once).
There is no safe concurrency to exploit here. The fix is scheduling, not
parallelism.

Cross-process contract with Garage Watch (unchanged): both processes take
the same `/tmp/ollama.lock` flock around the moment they actually touch
Ollama, exactly as before. What's new is *how* Huginn acquires it — bounded
polling instead of an indefinite block — and everything above that layer
(which of Huginn's own requests goes next, preemption, expiry) is purely
internal bookkeeping Garage Watch has no visibility into and needs no
changes for.

Two axes, independent by design (see HUGINN_CODEX_CLAUDE_PROMPT.md slice 3
amendment): RequestClass says *what* this targets (which model / kind of
work, used for game-mode admission and diagnostics); Purpose says *why* it
exists (urgency, used for priority/aging/expiry). The same RequestClass can
carry different Purposes — e.g. ordinary local reasoning is DIRECT when
answering the user and AMBIENT when generating an unprompted chime.
"""
from __future__ import annotations

import asyncio
import fcntl
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, AsyncIterator, Awaitable, Callable

from config import (
    AMBIENT_MAX_QUEUE_SECONDS, COORDINATOR_MAX_QUEUE_DEPTH,
    GAME_MODE_POLL_SECONDS, GATE_DECISION_AGING_INTERVAL_SECONDS,
    LOCK_POLL_INITIAL_SECONDS, LOCK_POLL_MAX_SECONDS,
    MAINTENANCE_MAX_QUEUE_SECONDS, _OLLAMA_LOCK_PATH,
)


class RequestClass(Enum):
    RESIDENT_PERSONALITY = "resident_personality"
    ORDINARY_LOCAL_REASONING = "ordinary_local_reasoning"
    LOCAL_VISION_GATEKEEPER = "local_vision_gatekeeper"
    MODEL_LOAD_UNLOAD = "model_load_unload"
    GAME_MODE_PERSONALITY_ONLY = "game_mode_personality_only"
    MAINTENANCE_BACKGROUND = "maintenance_background"


# Classes allowed to run during game mode. MODEL_LOAD_UNLOAD is handled
# specially (unloads are always fine during game mode; loads of anything
# but the personality model are not) rather than being in this set.
_GAME_MODE_ALLOWED_CLASSES = frozenset({
    RequestClass.RESIDENT_PERSONALITY,
    RequestClass.GAME_MODE_PERSONALITY_ONLY,
})


class Purpose(Enum):
    CRITICAL = "critical"            # priority 0 — bypasses almost everything
    DIRECT = "direct"                # priority 1 — user is waiting right now
    GATE_DECISION = "gate_decision"  # priority 2, bounded, ages toward 1
    AMBIENT = "ambient"              # priority 3, disposable — expires, never ages
    MAINTENANCE = "maintenance"      # priority 4, disposable — expires, never ages


_BASE_PRIORITY = {
    Purpose.CRITICAL: 0,
    Purpose.DIRECT: 1,
    Purpose.GATE_DECISION: 2,
    Purpose.AMBIENT: 3,
    Purpose.MAINTENANCE: 4,
}

# Purposes whose *running* work may be cancelled to free the slot for
# foreground (DIRECT/CRITICAL) work. DIRECT and CRITICAL are never preempted
# once running — cutting off a live chat response mid-stream would be worse
# than the contention it avoids.
PREEMPTIBLE_PURPOSES = frozenset({Purpose.GATE_DECISION, Purpose.AMBIENT, Purpose.MAINTENANCE})

# Purposes with a freshness window: dropped, not escalated, once stale.
_EXPIRY_SECONDS = {
    Purpose.AMBIENT: AMBIENT_MAX_QUEUE_SECONDS,
    Purpose.MAINTENANCE: MAINTENANCE_MAX_QUEUE_SECONDS,
}


class Denial(Enum):
    GAME_MODE = "game_mode"
    QUEUE_FULL = "queue_full"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    EXPIRED = "expired"
    PREEMPTED = "preempted"
    LOCK_TIMEOUT = "lock_timeout"
    ERROR = "error"


EmitFn = Callable[[Any], Awaitable[None]]


@dataclass(frozen=True)
class InferenceRequest:
    request_class: RequestClass
    purpose: Purpose
    model: str                                       # target Ollama model string (diagnostics + game-mode checks)
    fn: Callable[[EmitFn], Awaitable[Any]]            # always takes emit; non-streaming callers just don't call it
    deadline_seconds: float | None = None             # None = no explicit cap beyond the purpose's own expiry/aging behavior
    label: str = ""                                   # short diagnostic tag — never prompt/output content


@dataclass(frozen=True)
class CoordinatorEvent:
    kind: str  # "chunk" | "done" | "denied"
    value: Any = None
    denial: Denial | None = None
    detail: str = ""


@dataclass
class _QueueItem:
    request: InferenceRequest
    events: asyncio.Queue
    enqueued_at: float
    seq: int
    task: "asyncio.Task | None" = None


class DeadlineExceeded(Exception):
    pass


async def _acquire_shared_lock(deadline_seconds: float):
    """Bounded, cancellation-aware acquisition of the cross-process flock at
    _OLLAMA_LOCK_PATH (shared with Garage Watch). Polls LOCK_EX|LOCK_NB
    instead of blocking indefinitely so a deadline can actually be honored
    even while Garage Watch holds the real OS lock. Raises DeadlineExceeded
    if not acquired in time. Caller owns closing the returned file object."""
    f = open(_OLLAMA_LOCK_PATH, "w")
    start = time.monotonic()
    delay = LOCK_POLL_INITIAL_SECONDS
    while True:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            pass
        if time.monotonic() - start >= deadline_seconds:
            f.close()
            raise DeadlineExceeded("timed out waiting for the shared Ollama lock (Garage Watch?)")
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, LOCK_POLL_MAX_SECONDS)


def _release_shared_lock(f) -> None:
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


class Coordinator:
    """One instance per daemon process. Owns the single inference slot."""

    def __init__(self) -> None:
        self._queue: list[_QueueItem] = []
        self._lock = asyncio.Lock()
        self._wakeup = asyncio.Event()
        self._seq = 0
        self._current: "_QueueItem | None" = None
        self._scheduler_task: "asyncio.Task | None" = None
        # Injected by daemon.py at startup; kept here (not imported directly)
        # to avoid a context.py <-> coordinator.py import cycle.
        self._game_mode_check: Callable[[], bool] = lambda: False

    def set_game_mode_check(self, fn: Callable[[], bool]) -> None:
        self._game_mode_check = fn

    def start(self) -> None:
        # `.done()` also catches a scheduler task left over from a previous,
        # now-closed event loop (asyncio.run() cancels outstanding tasks on
        # exit) — important because this is a module-level singleton that
        # can outlive any one loop, e.g. across separate test runs.
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.ensure_future(self._scheduler_loop())

    async def stop(self) -> None:
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
            self._scheduler_task = None

    # ── Admission ────────────────────────────────────────────────────────────

    def _class_allowed_in_game_mode(self, request_class: RequestClass) -> bool:
        if request_class in _GAME_MODE_ALLOWED_CLASSES:
            return True
        if request_class == RequestClass.MODEL_LOAD_UNLOAD:
            return True  # unloads are always fine; the fn itself decides load vs unload
        return False

    def _admission_denial(self, request: InferenceRequest) -> "tuple[Denial, str] | None":
        if self._game_mode_check() and not self._class_allowed_in_game_mode(request.request_class):
            return (Denial.GAME_MODE, f"{request.request_class.value} not permitted during game mode")
        return None

    # ── Public API ───────────────────────────────────────────────────────────

    async def submit(self, request: InferenceRequest) -> AsyncIterator[CoordinatorEvent]:
        self.start()  # idempotent — lazy-start so a caller (or a test) can't
                      # deadlock on an un-started scheduler by forgetting to
                      # call start() explicitly at process boot.

        denial = self._admission_denial(request)
        if denial is not None:
            yield CoordinatorEvent("denied", denial=denial[0], detail=denial[1])
            return

        item = _QueueItem(request, asyncio.Queue(), time.monotonic(), 0)
        async with self._lock:
            if len(self._queue) >= COORDINATOR_MAX_QUEUE_DEPTH and not self._make_room_locked(request):
                yield CoordinatorEvent("denied", denial=Denial.QUEUE_FULL, detail="coordinator queue is full")
                return
            self._seq += 1
            item.seq = self._seq
            self._queue.append(item)
            self._maybe_preempt_locked(request)
            self._wakeup.set()

        while True:
            ev = await item.events.get()
            yield ev
            if ev.kind in ("done", "denied"):
                return

    def snapshot(self) -> dict:
        """Diagnostic-only: class/model/state/wait duration/denial reason.
        Never includes prompts, images, or generated content."""
        now = time.monotonic()
        running = None
        if self._current is not None:
            running = {
                "request_class": self._current.request.request_class.value,
                "purpose": self._current.request.purpose.value,
                "model": self._current.request.model,
                "label": self._current.request.label,
                "running_for_seconds": round(now - self._current.enqueued_at, 2),
            }
        queued = [
            {
                "request_class": it.request.request_class.value,
                "purpose": it.request.purpose.value,
                "model": it.request.model,
                "label": it.request.label,
                "waited_seconds": round(now - it.enqueued_at, 2),
            }
            for it in self._queue
        ]
        return {"running": running, "queued": queued, "queue_depth": len(self._queue)}

    # ── Internal scheduling ──────────────────────────────────────────────────

    def _effective_priority(self, item: _QueueItem, now: float) -> tuple[int, int]:
        base = _BASE_PRIORITY[item.request.purpose]
        if item.request.purpose == Purpose.GATE_DECISION:
            waited = now - item.enqueued_at
            aged = base - int(waited // GATE_DECISION_AGING_INTERVAL_SECONDS)
            base = max(aged, _BASE_PRIORITY[Purpose.DIRECT])
        return (base, item.seq)  # seq breaks ties FIFO

    def _make_room_locked(self, incoming: InferenceRequest) -> bool:
        """Queue is at capacity and a new item wants in. If the incoming
        request is foreground and something disposable is queued, evict the
        worst disposable item to make room. Never evicts to make room for
        background work."""
        if _BASE_PRIORITY[incoming.purpose] > _BASE_PRIORITY[Purpose.DIRECT]:
            return False
        candidates = [it for it in self._queue if it.request.purpose in (Purpose.AMBIENT, Purpose.MAINTENANCE)]
        if not candidates:
            return False
        worst = max(candidates, key=lambda it: (_BASE_PRIORITY[it.request.purpose], -it.seq))
        self._queue.remove(worst)
        worst.events.put_nowait(CoordinatorEvent("denied", denial=Denial.QUEUE_FULL, detail="evicted to admit foreground work"))
        return True

    def _maybe_preempt_locked(self, incoming: InferenceRequest) -> None:
        if _BASE_PRIORITY[incoming.purpose] > _BASE_PRIORITY[Purpose.DIRECT]:
            return  # incoming isn't foreground — never preempts anything
        current = self._current
        if current is None or current.task is None or current.task.done():
            return
        if current.request.purpose not in PREEMPTIBLE_PURPOSES:
            return
        current.task.cancel()

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            async with self._lock:
                self._wakeup.clear()
                self._drop_expired_locked()
                item = self._pop_best_locked()
            if item is None:
                continue
            await self._run_item(item)

    def _drop_expired_locked(self) -> None:
        now = time.monotonic()
        survivors = []
        for it in self._queue:
            max_wait = _EXPIRY_SECONDS.get(it.request.purpose)
            if max_wait is not None and (now - it.enqueued_at) > max_wait:
                it.events.put_nowait(CoordinatorEvent(
                    "denied", denial=Denial.EXPIRED,
                    detail=f"{it.request.purpose.value} request stale after {max_wait}s unserved",
                ))
            else:
                survivors.append(it)
        self._queue = survivors

    def _pop_best_locked(self) -> "_QueueItem | None":
        if not self._queue:
            return None
        now = time.monotonic()
        best = min(self._queue, key=lambda it: self._effective_priority(it, now))
        self._queue.remove(best)
        return best

    async def _run_item(self, item: _QueueItem) -> None:
        request = item.request

        # Re-check admission immediately before doing anything — state may
        # have changed while queued (game mode toggled, etc).
        denial = self._admission_denial(request)
        if denial is not None:
            await item.events.put(CoordinatorEvent("denied", denial=denial[0], detail=denial[1]))
            return

        deadline = request.deadline_seconds if request.deadline_seconds is not None else 60.0
        start = time.monotonic()
        try:
            lock_f = await _acquire_shared_lock(deadline)
        except DeadlineExceeded as e:
            await item.events.put(CoordinatorEvent("denied", denial=Denial.LOCK_TIMEOUT, detail=str(e)))
            return

        watchdog: "asyncio.Task | None" = None
        try:
            remaining = None
            if request.deadline_seconds is not None:
                remaining = max(request.deadline_seconds - (time.monotonic() - start), 0.01)

            async def emit(value: Any) -> None:
                await item.events.put(CoordinatorEvent("chunk", value=value))

            run_coro = request.fn(emit)
            task = asyncio.ensure_future(
                asyncio.wait_for(run_coro, timeout=remaining) if remaining is not None else run_coro
            )
            item.task = task
            self._current = item

            if request.request_class not in _GAME_MODE_ALLOWED_CLASSES:
                watchdog = asyncio.ensure_future(self._game_mode_watchdog(task))

            try:
                result = await task
                await item.events.put(CoordinatorEvent("done", value=result))
            except asyncio.CancelledError:
                await item.events.put(CoordinatorEvent("denied", denial=Denial.PREEMPTED, detail="cancelled for higher-priority work or a game-mode change"))
            except asyncio.TimeoutError:
                await item.events.put(CoordinatorEvent("denied", denial=Denial.DEADLINE_EXCEEDED, detail="deadline exceeded while running"))
            except Exception as e:
                await item.events.put(CoordinatorEvent("denied", denial=Denial.ERROR, detail=str(e)))
        finally:
            if watchdog is not None:
                watchdog.cancel()
            self._current = None
            _release_shared_lock(lock_f)

    async def _game_mode_watchdog(self, task: asyncio.Task) -> None:
        """While a non-personality item runs, periodically re-check game
        mode; cancel the running task if it turns on mid-run. Never leaves
        model state ambiguous: cancellation goes through the same path as
        preemption, and the caller (e.g. gatekeeper) re-probes actual
        residency itself before deciding whether to unload anything."""
        try:
            while not task.done():
                await asyncio.sleep(GAME_MODE_POLL_SECONDS)
                if self._game_mode_check():
                    task.cancel()
                    return
        except asyncio.CancelledError:
            pass


# One instance per process. Deliberately not tied to context.py's game-mode
# check at import time (that would create a context.py <-> coordinator.py
# cycle, since context.py imports this module for snapshot()) — daemon.py
# wires it up at startup via set_game_mode_check().
coordinator = Coordinator()
