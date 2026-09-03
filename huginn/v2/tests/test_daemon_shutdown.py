"""Regression test for the shutdown traceback:

    RuntimeError: Event loop stopped before Future completed.

Root cause: the old _shutdown() called asyncio.get_event_loop().stop()
while running under asyncio.run(main()) — asyncio.run() owns the loop's
lifecycle for the duration of main(), and stopping the loop out from under
its own run_until_complete() call means main()'s Future never gets to
finish. The fix cancels the serve_forever() task instead (the documented
way to stop it) and lets main() unwind and return normally.
"""
import asyncio

import daemon


def test_shutdown_cancels_serve_task_and_closes_server():
    async def scenario():
        class _FakeServer:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        server = _FakeServer()
        task = asyncio.ensure_future(asyncio.sleep(3600))
        await asyncio.sleep(0)  # let it start

        await daemon._shutdown(server, task)

        assert server.closed is True
        assert task.cancelled() or task.cancelling() > 0

    asyncio.run(scenario())


def test_shutdown_never_calls_loop_stop(monkeypatch):
    """The exact anti-pattern that produced the traceback — _shutdown must
    never touch get_event_loop()/stop() at all."""
    calls = {"stop": 0}

    class _FakeLoop:
        def stop(self):
            calls["stop"] += 1

    monkeypatch.setattr(asyncio, "get_event_loop", lambda: _FakeLoop())

    async def scenario():
        class _FakeServer:
            def close(self):
                pass

        task = asyncio.ensure_future(asyncio.sleep(3600))
        await asyncio.sleep(0)
        await daemon._shutdown(_FakeServer(), task)

    asyncio.run(scenario())
    assert calls["stop"] == 0


def test_main_returns_cleanly_from_asyncio_run_on_shutdown(monkeypatch, tmp_path):
    """The actual regression, end to end: main() running under
    asyncio.run(), shut down via cancelling its own serve_forever() task
    (as the real signal handler does), must return normally — no
    RuntimeError, no unhandled exception."""
    fake_socket = tmp_path / "huginn-test.sock"
    monkeypatch.setattr(daemon, "SOCKET_PATH", fake_socket)
    monkeypatch.setattr(daemon.os, "chmod", lambda *a, **kw: None)
    monkeypatch.setattr(daemon.coordinator, "set_game_mode_check", lambda fn: None)
    monkeypatch.setattr(daemon.coordinator, "start", lambda: None)
    monkeypatch.setattr(daemon, "task_worker", lambda: _never())
    monkeypatch.setattr(daemon, "random_chime_worker", lambda: _never())
    monkeypatch.setattr(daemon, "activity_tracker_worker", lambda: _never())
    monkeypatch.setattr(daemon, "screenshot_worker", lambda: _never())
    monkeypatch.setattr(daemon.context, "collect_interaction", lambda: _NEVER_GAME)

    class _FakeServer:
        def __init__(self):
            self.closed = False
            self.serve_task_ref: list = []

        async def serve_forever(self):
            self.serve_task_ref.append(asyncio.current_task())
            await asyncio.sleep(3600)

        def close(self):
            self.closed = True

    fake_server = _FakeServer()

    async def fake_start_unix_server(*a, **kw):
        return fake_server

    monkeypatch.setattr(asyncio, "start_unix_server", fake_start_unix_server)

    async def scenario():
        main_task = asyncio.ensure_future(daemon.main())
        # Let main() run until it's blocked inside `await serve_task`.
        for _ in range(20):
            await asyncio.sleep(0)
            if fake_server.serve_task_ref:
                break
        assert fake_server.serve_task_ref, "main() never reached serve_forever()"

        await daemon._shutdown(fake_server, fake_server.serve_task_ref[0])

        await asyncio.wait_for(main_task, timeout=2)
        assert fake_server.closed is True

    asyncio.run(scenario())


class _Interaction:
    mode = "ambient"


_NEVER_GAME = _Interaction()


async def _never():
    await asyncio.sleep(3600)
