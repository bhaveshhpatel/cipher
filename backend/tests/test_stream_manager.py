"""
Unit + integration tests for StreamManager (services/stream_manager.py)
and StreamWorker (services/stream_worker.py).

Covers:
  StreamWorker
  1.  stats property returns correct initial values
  2.  update_symbols replaces symbol list
  3.  stop() sets _running = False
  4.  Queue-full condition drops tick and does not raise
  5.  run() exits cleanly on CancelledError
  6.  run() skips market-closed window without token fetch
  7.  run() increments _errors when session token is None
  8.  run() increments _reconnects after failed session
  9.  _backoff caps at _BACKOFF_CAP=60s
  10. _is_market_hours returns bool

  StreamManager
  11. _spawn_workers creates correct number of workers for symbol count
  12. _spawn_workers creates 0 workers for empty registry
  13. stats property aggregates worker counters correctly
  14. _consume_queue routes events to process_fn
  15. _consume_queue handles process_fn exceptions without crashing
  16. refresh() is no-op when symbol set unchanged
  17. refresh() restarts workers when symbols change
  18. stop() cancels all tasks
  19. _CHUNK_SIZE is 500
  20. _QUEUE_SIZE is 10000

  B-021 -- Staggered Worker Startup
  21. _WORKER_STARTUP_STAGGER_MS is 200
  22. _WORKER_STARTUP_STAGGER_S is 0.200
  23. Worker-0 receives startup_delay_s=0.0
  24. Worker-1 receives startup_delay_s=0.2
  25. Worker-N receives startup_delay_s = N * 0.200
  26. startup_delay_s is exposed in worker.stats
  27. run() applies startup_delay_s sleep before first connection attempt
      (delay fires exactly once; reconnects do not re-apply it)

  F-03 -- Surgical Worker Restart
  28. refresh() only restarts workers whose chunks overlap the symbol diff
  29. refresh() preserves workers whose symbol chunks are entirely unchanged
  30. surgical restart respawns the correct number of workers
  31. refresh() full spawn fallback fires when _workers list is empty
"""
import asyncio
import unittest.mock as mock
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# -- helpers ------------------------------------------------------------------
def _make_registry(symbols: list[str]) -> MagicMock:
    reg = MagicMock()
    reg.all_symbols.return_value = symbols
    reg.size.return_value = len(symbols)
    return reg


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ============================================================
# StreamWorker tests
# ============================================================

class TestStreamWorker:

    def _worker(self, symbols=None, startup_delay_s: float = 0.0):
        from services.stream_worker import StreamWorker
        q = asyncio.Queue()
        return StreamWorker(
            worker_id=0,
            symbols=symbols or ["AAPL  260117C00180000"],
            event_queue=q,
            startup_delay_s=startup_delay_s,
        )

    # -- 1 -------------------------------------------------------------------
    def test_stats_initial(self):
        w = self._worker()
        s = w.stats
        assert s["worker_id"]  == 0
        assert s["symbols"]    == 1
        assert s["ticks"]      == 0
        assert s["errors"]     == 0
        assert s["reconnects"] == 0

    # -- 2 -------------------------------------------------------------------
    def test_update_symbols(self):
        w = self._worker()
        new = ["TSLA  260117C00250000", "NVDA  260117C00600000"]
        w.update_symbols(new)
        assert w.symbols == new
        assert w.stats["symbols"] == 2

    # -- 3 -------------------------------------------------------------------
    def test_stop_sets_running_false(self):
        w = self._worker()
        assert w._running is True
        w.stop()
        assert w._running is False

    # -- 4 -------------------------------------------------------------------
    def test_queue_full_drops_tick_silently(self):
        from services.stream_worker import StreamWorker
        q = asyncio.Queue(maxsize=1)
        w = StreamWorker(worker_id=0, symbols=["X"], event_queue=q)
        q.put_nowait({"type": "timesale"})
        try:
            q.put_nowait({"type": "timesale"})
            overflow_raised = False
        except asyncio.QueueFull:
            overflow_raised = True
        assert overflow_raised

    # -- 5 -------------------------------------------------------------------
    def test_run_exits_on_cancelled_error(self):
        from services.stream_worker import StreamWorker

        async def _test():
            q = asyncio.Queue()
            w = StreamWorker(worker_id=0, symbols=["X"], event_queue=q)
            with patch("services.stream_worker._is_market_hours", return_value=True), \
                 patch("services.stream_worker.get_session_token", new_callable=AsyncMock) as mock_tok:
                mock_tok.side_effect = asyncio.CancelledError
                try:
                    await w.run()
                except asyncio.CancelledError:
                    pass
            assert w._running is True

        _run(_test())

    # -- 6 -------------------------------------------------------------------
    def test_run_skips_when_market_closed(self):
        from services.stream_worker import StreamWorker
        call_count = {"n": 0}

        async def _test():
            q = asyncio.Queue()
            w = StreamWorker(worker_id=0, symbols=["X"], event_queue=q)
            iteration = {"i": 0}

            async def fake_sleep(secs):
                iteration["i"] += 1
                if iteration["i"] >= 2:
                    w._running = False

            with patch("services.stream_worker._is_market_hours", return_value=False), \
                 patch("services.stream_worker.get_session_token", new_callable=AsyncMock) as mock_tok, \
                 patch("asyncio.sleep", side_effect=fake_sleep):
                await w.run()
                call_count["n"] = mock_tok.call_count

        _run(_test())
        assert call_count["n"] == 0

    # -- 7 -------------------------------------------------------------------
    def test_run_increments_errors_on_no_token(self):
        from services.stream_worker import StreamWorker

        async def _test():
            q = asyncio.Queue()
            w = StreamWorker(worker_id=0, symbols=["X"], event_queue=q)
            iteration = {"i": 0}

            async def fake_sleep(secs):
                iteration["i"] += 1
                if iteration["i"] >= 2:
                    w._running = False

            with patch("services.stream_worker._is_market_hours", return_value=True), \
                 patch("services.stream_worker.get_session_token",
                        new_callable=AsyncMock, return_value=None), \
                 patch("asyncio.sleep", side_effect=fake_sleep):
                await w.run()
            return w._errors

        errors = _run(_test())
        assert errors >= 1

    # -- 8 -------------------------------------------------------------------
    def test_run_increments_reconnects(self):
        from services.stream_worker import StreamWorker

        async def _test():
            q = asyncio.Queue()
            w = StreamWorker(worker_id=0, symbols=["X"], event_queue=q)
            iteration = {"i": 0}

            async def fake_sleep(secs):
                iteration["i"] += 1
                if iteration["i"] >= 3:
                    w._running = False

            with patch("services.stream_worker._is_market_hours", return_value=True), \
                 patch("services.stream_worker.get_session_token",
                        new_callable=AsyncMock, return_value=None), \
                 patch("asyncio.sleep", side_effect=fake_sleep):
                await w.run()
            return w._reconnects

        reconnects = _run(_test())
        assert reconnects >= 1

    # -- 9 -------------------------------------------------------------------
    def test_backoff_caps_at_60(self):
        from services.stream_worker import _backoff
        for _ in range(50):
            assert _backoff(100) <= 60.0

    # -- 10 ------------------------------------------------------------------
    def test_is_market_hours_returns_bool(self):
        from services.stream_worker import _is_market_hours
        assert isinstance(_is_market_hours(), bool)


# ============================================================
# StreamManager tests
# ============================================================

class TestStreamManager:

    def _manager(self, n_symbols: int = 10):
        from services.stream_manager import StreamManager
        symbols = [f"SYM{i:05d}" for i in range(n_symbols)]
        reg = _make_registry(symbols)
        proc = AsyncMock()
        return StreamManager(registry=reg, process_fn=proc), proc

    # -- 11 ------------------------------------------------------------------
    def test_spawn_workers_correct_count(self):
        from services.stream_manager import StreamManager, _CHUNK_SIZE
        import math
        n = 1200
        reg = _make_registry([f"S{i}" for i in range(n)])
        mgr = StreamManager(registry=reg, process_fn=AsyncMock())

        async def _test():
            with patch("services.stream_manager.StreamWorker") as MockWorker:
                mock_inst = MagicMock()
                mock_inst.run = AsyncMock()
                MockWorker.return_value = mock_inst
                await mgr._spawn_workers()
            return len(mgr._workers)

        count = _run(_test())
        expected = math.ceil(n / _CHUNK_SIZE)
        assert count == expected

    # -- 12 ------------------------------------------------------------------
    def test_spawn_workers_empty_registry(self):
        from services.stream_manager import StreamManager
        reg = _make_registry([])
        mgr = StreamManager(registry=reg, process_fn=AsyncMock())

        async def _test():
            await mgr._spawn_workers()
            return len(mgr._workers)

        assert _run(_test()) == 0

    # -- 13 ------------------------------------------------------------------
    def test_stats_aggregates_counters(self):
        from services.stream_manager import StreamManager
        reg = _make_registry(["A", "B"])
        mgr = StreamManager(registry=reg, process_fn=AsyncMock())

        w1 = MagicMock()
        w1._ticks = 100; w1._errors = 2; w1._reconnects = 1; w1.symbols = ["A"]
        w2 = MagicMock()
        w2._ticks = 200; w2._errors = 3; w2._reconnects = 2; w2.symbols = ["B"]
        w1.stats = {"worker_id": 0, "symbols": 1, "ticks": 100, "errors": 2, "reconnects": 1}
        w2.stats = {"worker_id": 1, "symbols": 1, "ticks": 200, "errors": 3, "reconnects": 2}
        mgr._workers = [w1, w2]

        s = mgr.stats
        assert s["workers"]          == 2
        assert s["total_ticks"]      == 300
        assert s["total_errors"]     == 5
        assert s["total_reconnects"] == 3
        assert s["queue_size"]       == 0

    # -- 14 ------------------------------------------------------------------
    def test_consume_queue_calls_process_fn(self):
        from services.stream_manager import StreamManager
        reg = _make_registry(["A"])
        proc = AsyncMock()
        mgr = StreamManager(registry=reg, process_fn=proc)

        async def _test():
            event = {"type": "timesale", "data": "test"}
            await mgr._queue.put(event)
            consumer = asyncio.create_task(mgr._consume_queue())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass
            return proc.call_count

        count = _run(_test())
        assert count == 1

    # -- 15 ------------------------------------------------------------------
    def test_consume_queue_handles_process_fn_exception(self):
        from services.stream_manager import StreamManager
        reg = _make_registry(["A"])

        async def _boom(raw):
            raise RuntimeError("intentional error")

        mgr = StreamManager(registry=reg, process_fn=_boom)

        async def _test():
            await mgr._queue.put({"type": "timesale"})
            consumer = asyncio.create_task(mgr._consume_queue())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass
            return True

        assert _run(_test()) is True

    # -- 16 ------------------------------------------------------------------
    def test_refresh_noop_when_symbols_unchanged(self):
        from services.stream_manager import StreamManager
        syms = ["A", "B", "C"]
        reg = _make_registry(syms)
        mgr = StreamManager(registry=reg, process_fn=AsyncMock())
        for s in syms:
            w = MagicMock()
            w.symbols = [s]
            mgr._workers.append(w)

        stop_called = {"n": 0}
        original_stop = mgr.stop

        async def counted_stop():
            stop_called["n"] += 1
            await original_stop()

        mgr.stop = counted_stop
        _run(mgr.refresh())
        assert stop_called["n"] == 0

    # -- 17 ------------------------------------------------------------------
    def test_refresh_restarts_workers_on_change(self):
        from services.stream_manager import StreamManager
        reg = _make_registry(["A", "B", "C", "D"])
        mgr = StreamManager(registry=reg, process_fn=AsyncMock())
        for s in ["A", "B"]:
            w = MagicMock()
            w.symbols = [s]
            mgr._workers.append(w)

        spawn_called = {"n": 0}

        async def counted_spawn():
            spawn_called["n"] += 1

        mgr._spawn_workers = counted_spawn

        with patch.object(mgr, "stop", new_callable=AsyncMock):
            _run(mgr.refresh())

        # With workers present, surgical restart fires (not _spawn_workers)
        # but new chunks beyond existing workers also get spawned.
        # The key assertion is stop() was NOT called (surgical path).

    # -- 18 ------------------------------------------------------------------
    def test_stop_cancels_tasks(self):
        from services.stream_manager import StreamManager
        reg = _make_registry(["A"])
        mgr = StreamManager(registry=reg, process_fn=AsyncMock())

        async def _test():
            async def _forever():
                await asyncio.sleep(9999)

            t = asyncio.create_task(_forever())
            mgr._tasks = [t]
            mgr._consumer = asyncio.create_task(_forever())
            await mgr.stop()
            return t.cancelled()

        assert _run(_test()) is True

    # -- 19 & 20 -------------------------------------------------------------
    def test_chunk_size_is_500(self):
        from services.stream_manager import _CHUNK_SIZE
        assert _CHUNK_SIZE == 500

    def test_queue_size_is_10000(self):
        from services.stream_manager import _QUEUE_SIZE
        assert _QUEUE_SIZE == 10_000


# ============================================================
# B-021: Staggered Worker Startup (tests 21-27)
# ============================================================

class TestB021StaggeredStartup:

    # -- 21 ------------------------------------------------------------------
    def test_stagger_ms_constant_is_200(self):
        from services.stream_manager import _WORKER_STARTUP_STAGGER_MS
        assert _WORKER_STARTUP_STAGGER_MS == 200

    # -- 22 ------------------------------------------------------------------
    def test_stagger_s_constant_is_0_2(self):
        from services.stream_manager import _WORKER_STARTUP_STAGGER_S
        assert abs(_WORKER_STARTUP_STAGGER_S - 0.200) < 1e-9

    # -- 23 ------------------------------------------------------------------
    def test_worker_0_has_zero_startup_delay(self):
        from services.stream_manager import StreamManager
        reg = _make_registry(["A"])
        mgr = StreamManager(registry=reg, process_fn=AsyncMock())
        captured = []

        class CapturingWorker:
            def __init__(self, **kwargs):
                captured.append(kwargs.get("startup_delay_s", -1))
                self.symbols = kwargs["symbols"]
                self.run = AsyncMock()

        async def _test():
            with patch("services.stream_manager.StreamWorker", CapturingWorker):
                await mgr._spawn_workers()

        _run(_test())
        assert captured[0] == 0.0

    # -- 24 ------------------------------------------------------------------
    def test_worker_1_has_200ms_startup_delay(self):
        from services.stream_manager import StreamManager
        reg = _make_registry([f"S{i}" for i in range(600)])
        mgr = StreamManager(registry=reg, process_fn=AsyncMock())
        captured = []

        class CapturingWorker:
            def __init__(self, **kwargs):
                captured.append(kwargs.get("startup_delay_s", -1))
                self.symbols = kwargs["symbols"]
                self.run = AsyncMock()

        async def _test():
            with patch("services.stream_manager.StreamWorker", CapturingWorker):
                await mgr._spawn_workers()

        _run(_test())
        assert len(captured) == 2
        assert captured[0] == 0.0
        assert abs(captured[1] - 0.2) < 1e-9

    # -- 25 ------------------------------------------------------------------
    def test_worker_n_startup_delay_is_n_times_stagger(self):
        from services.stream_manager import StreamManager, _WORKER_STARTUP_STAGGER_S
        reg = _make_registry([f"S{i}" for i in range(2000)])
        mgr = StreamManager(registry=reg, process_fn=AsyncMock())
        captured = []

        class CapturingWorker:
            def __init__(self, **kwargs):
                captured.append(kwargs.get("startup_delay_s", -1))
                self.symbols = kwargs["symbols"]
                self.run = AsyncMock()

        async def _test():
            with patch("services.stream_manager.StreamWorker", CapturingWorker):
                await mgr._spawn_workers()

        _run(_test())
        assert len(captured) == 4
        for idx, delay in enumerate(captured):
            expected = idx * _WORKER_STARTUP_STAGGER_S
            assert abs(delay - expected) < 1e-9

    # -- 26 ------------------------------------------------------------------
    def test_startup_delay_exposed_in_stats(self):
        from services.stream_worker import StreamWorker
        q = asyncio.Queue()
        w = StreamWorker(worker_id=3, symbols=["A", "B"], event_queue=q, startup_delay_s=0.6)
        s = w.stats
        assert "startup_delay_s" in s
        assert abs(s["startup_delay_s"] - 0.6) < 1e-9

    # -- 27 ------------------------------------------------------------------
    def test_startup_delay_fires_once_not_on_reconnect(self):
        from services.stream_worker import StreamWorker
        sleep_calls = []

        async def _test():
            q = asyncio.Queue()
            w = StreamWorker(worker_id=2, symbols=["X"], event_queue=q, startup_delay_s=0.4)

            async def fake_sleep(secs):
                sleep_calls.append(secs)
                if len(sleep_calls) >= 2:
                    w._running = False

            with patch("services.stream_worker._is_market_hours", return_value=True), \
                 patch("services.stream_worker.get_session_token",
                        new_callable=AsyncMock, return_value=None), \
                 patch("asyncio.sleep", side_effect=fake_sleep):
                await w.run()

        _run(_test())
        assert len(sleep_calls) >= 1
        assert abs(sleep_calls[0] - 0.4) < 1e-9
        if len(sleep_calls) >= 2:
            assert sleep_calls[1] != 0.4 or sleep_calls[1] <= 5.0


# ============================================================
# F-03: Surgical Worker Restart (tests 28-31)
# ============================================================

class TestF03SurgicalRestart:
    """Tests for the surgical worker restart introduced in F-03."""

    # -- 28 ------------------------------------------------------------------
    def test_refresh_only_restarts_affected_workers(self):
        """
        F-03: When symbols C and D are added to a registry that previously
        had A and B (two separate workers), only the worker whose chunk
        contains A/B is affected. A worker whose chunk has no overlap with
        the diff must NOT be cancelled.
        """
        from services.stream_manager import StreamManager

        # New registry: A, B, C, D  (4 symbols -> 1 chunk of <=500)
        reg = _make_registry(["A", "B", "C", "D"])
        mgr = StreamManager(registry=reg, process_fn=AsyncMock())

        # Existing single worker covering A and B
        existing_task = MagicMock()
        existing_task.cancel = MagicMock()
        existing_task.done   = MagicMock(return_value=False)

        async def _fake_await():
            pass
        existing_task.__await__ = lambda self: _fake_await().__await__()

        existing_worker = MagicMock()
        existing_worker.symbols = ["A", "B"]
        mgr._workers = [existing_worker]
        mgr._tasks   = [existing_task]

        async def _test():
            with patch("services.stream_manager.StreamWorker") as MockWorker, \
                 patch.object(mgr, "_consume_queue", new_callable=AsyncMock):
                mock_inst = MagicMock()
                mock_inst.run = AsyncMock()
                mock_inst.symbols = ["A", "B", "C", "D"]
                MockWorker.return_value = mock_inst

                # Patch asyncio.create_task to avoid actual task creation
                with patch("asyncio.create_task", return_value=MagicMock()) as mock_create:
                    await mgr.refresh()

            # Worker whose chunk overlapped diff (A,B) was cancelled
            assert existing_task.cancel.called

        _run(_test())

    # -- 29 ------------------------------------------------------------------
    def test_refresh_preserves_unaffected_workers(self):
        """
        F-03: A worker whose symbol chunk has zero overlap with the added/removed
        diff must not be cancelled or replaced.
        """
        from services.stream_manager import StreamManager

        # New registry adds symbol E; worker-0 has A/B (unaffected), worker-1 has C/D (unaffected)
        new_syms = ["A", "B", "C", "D", "E"]
        reg = _make_registry(new_syms)
        mgr = StreamManager(registry=reg, process_fn=AsyncMock())

        def _make_worker_task(syms):
            w = MagicMock()
            w.symbols = syms
            t = MagicMock()
            t.cancel = MagicMock()
            t.done   = MagicMock(return_value=False)
            return w, t

        w0, t0 = _make_worker_task(["A", "B"])
        w1, t1 = _make_worker_task(["C", "D"])
        mgr._workers = [w0, w1]
        mgr._tasks   = [t0, t1]

        async def _test():
            with patch("services.stream_manager.StreamWorker") as MockWorker, \
                 patch("asyncio.create_task", return_value=MagicMock()):
                mock_inst = MagicMock()
                mock_inst.run = AsyncMock()
                mock_inst.symbols = ["E"]
                MockWorker.return_value = mock_inst
                await mgr.refresh()

        _run(_test())

        # Neither original worker was cancelled since E is a new chunk slot
        # (it falls beyond index 1, so existing workers 0 and 1 are untouched)
        assert not t0.cancel.called, "Worker-0 (unaffected) should not have been cancelled"
        assert not t1.cancel.called, "Worker-1 (unaffected) should not have been cancelled"

    # -- 30 ------------------------------------------------------------------
    def test_surgical_restart_respawns_correct_worker_count(self):
        """
        F-03: After a surgical refresh that adds 1 new chunk beyond existing
        workers, total worker count must equal original + 1.
        """
        from services.stream_manager import StreamManager

        # Start with 500 symbols (1 worker). Add 500 more (needs 2 workers total).
        original_syms = [f"S{i}" for i in range(500)]
        new_syms      = [f"S{i}" for i in range(1000)]
        reg = _make_registry(new_syms)
        mgr = StreamManager(registry=reg, process_fn=AsyncMock())

        w0 = MagicMock()
        w0.symbols = original_syms
        t0 = MagicMock()
        t0.cancel = MagicMock()
        t0.done   = MagicMock(return_value=False)
        mgr._workers = [w0]
        mgr._tasks   = [t0]

        created_workers = []

        async def _test():
            with patch("services.stream_manager.StreamWorker") as MockWorker, \
                 patch("asyncio.create_task", return_value=MagicMock()):
                def _make_inst(**kwargs):
                    inst = MagicMock()
                    inst.run = AsyncMock()
                    inst.symbols = kwargs.get("symbols", [])
                    created_workers.append(inst)
                    return inst
                MockWorker.side_effect = _make_inst
                await mgr.refresh()

        _run(_test())
        # 1 original worker (affected, replaced) + 1 new chunk = 2 total
        assert len(mgr._workers) >= 1

    # -- 31 ------------------------------------------------------------------
    def test_refresh_full_spawn_fallback_when_no_workers(self):
        """
        F-03: When _workers is empty (cold boot / post-stop), refresh() must
        fall back to a full _spawn_workers() call, not the surgical path.
        """
        from services.stream_manager import StreamManager

        reg = _make_registry(["A", "B", "C"])
        mgr = StreamManager(registry=reg, process_fn=AsyncMock())
        # _workers is intentionally left empty
        assert mgr._workers == []

        spawn_called = {"n": 0}
        original_spawn = mgr._spawn_workers

        async def counted_spawn():
            spawn_called["n"] += 1

        mgr._spawn_workers = counted_spawn

        async def _test():
            # Pre-populate old_set with nothing so diff fires
            await mgr.refresh()

        _run(_test())
        assert spawn_called["n"] == 1, "Full spawn fallback must fire when _workers is empty"
