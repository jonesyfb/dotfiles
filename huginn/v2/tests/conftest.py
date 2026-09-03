import sys
from pathlib import Path

# v2/*.py modules use bare sibling imports (e.g. `from config import ...`),
# relying on the interpreter adding the script's own directory to sys.path
# when run directly (`python3 v2/daemon.py`). pytest doesn't do that, so
# point it at v2/ explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_coordinator_singleton():
    """coordinator.coordinator is a module-level singleton shared across the
    whole process — without this, leftover queue/running state from one
    test's asyncio.run() (a fresh event loop each time) would bleed into the
    next test's. Reset before and after every test."""
    import coordinator as coordinator_module

    def _reset():
        c = coordinator_module.coordinator
        c._queue = []
        c._current = None
        c._scheduler_task = None  # asyncio.run() already cancelled it with its loop
        c._game_mode_check = lambda: False
        # asyncio.Lock/Event bind to whatever loop first uses them — each
        # test's asyncio.run() is a fresh loop, so these must be recreated
        # too or the next test fails with "bound to a different event loop".
        import asyncio
        c._lock = asyncio.Lock()
        c._wakeup = asyncio.Event()

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _reset_capture_singleflight():
    """capture._capture_task/_capture_task_lock are module-level singletons
    for the same reason as the coordinator above — an asyncio.Lock/Task
    bound to one test's event loop breaks the next test's fresh loop."""
    import asyncio

    import capture as capture_module

    def _reset():
        capture_module._capture_task = None
        capture_module._capture_task_lock = asyncio.Lock()

    _reset()
    yield
    _reset()
