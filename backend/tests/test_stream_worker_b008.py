"""
Tests for stream_worker.py B-008 global stats rollup.

Covers:
  SW-01  _inc_global_error() increments tradier_stream._stats["errors"]
  SW-02  _inc_global_reconnect() increments tradier_stream._stats["reconnects"]
  SW-03  _inc_global_reconnect() sets tradier_stream._stats["last_reconnect_at"] to a float
  SW-04  _inc_global_error() is tolerant of missing key (no exception raised)
  SW-05  Multiple workers each calling _inc_global_reconnect() accumulate correctly
"""
import asyncio
import time as _time
import types
import sys

import pytest


def _make_fake_tradier_stream_module(initial_stats: dict) -> types.ModuleType:
    """
    Build a minimal fake tradier_stream module with a _stats dict.
    Injected into sys.modules so stream_worker._global_stats() finds it.
    """
    mod = types.ModuleType("services.tradier_stream")
    mod._stats = initial_stats
    return mod


def _worker(stats_dict: dict):
    """Return a StreamWorker with fake tradier_stream injected."""
    fake_mod = _make_fake_tradier_stream_module(stats_dict)
    sys.modules["services.tradier_stream"] = fake_mod

    # Re-import to pick up fresh sys.modules
    import importlib
    import services.stream_worker as sw_mod
    importlib.reload(sw_mod)

    from services.stream_worker import StreamWorker
    return StreamWorker(
        worker_id=0,
        symbols=["AAPL  260117C00180000"],
        event_queue=asyncio.Queue(),
    )


class TestB008GlobalStatsRollup:

    # SW-01: _inc_global_error increments errors
    def test_inc_global_error_increments_errors(self):
        stats = {"errors": 0, "reconnects": 0, "last_reconnect_at": None}
        w = _worker(stats)
        w._inc_global_error()
        assert stats["errors"] == 1
        w._inc_global_error()
        assert stats["errors"] == 2

    # SW-02: _inc_global_reconnect increments reconnects
    def test_inc_global_reconnect_increments_reconnects(self):
        stats = {"errors": 0, "reconnects": 0, "last_reconnect_at": None}
        w = _worker(stats)
        w._inc_global_reconnect()
        assert stats["reconnects"] == 1

    # SW-03: _inc_global_reconnect sets last_reconnect_at to a recent float
    def test_inc_global_reconnect_sets_last_reconnect_at(self):
        stats = {"errors": 0, "reconnects": 0, "last_reconnect_at": None}
        w = _worker(stats)
        before = _time.time()
        w._inc_global_reconnect()
        after = _time.time()
        assert stats["last_reconnect_at"] is not None
        assert isinstance(stats["last_reconnect_at"], float)
        assert before <= stats["last_reconnect_at"] <= after

    # SW-04: _inc_global_error tolerates missing key — no exception
    def test_inc_global_error_tolerates_missing_key(self):
        stats = {}  # no "errors" key
        w = _worker(stats)
        # Should not raise
        w._inc_global_error()

    # SW-05: multiple workers accumulate into the same stats dict
    def test_multiple_workers_accumulate_reconnects(self):
        stats = {"errors": 0, "reconnects": 0, "last_reconnect_at": None}
        fake_mod = _make_fake_tradier_stream_module(stats)
        sys.modules["services.tradier_stream"] = fake_mod

        import importlib
        import services.stream_worker as sw_mod
        importlib.reload(sw_mod)
        from services.stream_worker import StreamWorker

        workers = [
            StreamWorker(worker_id=i, symbols=[], event_queue=asyncio.Queue())
            for i in range(5)
        ]
        for w in workers:
            w._inc_global_reconnect()

        assert stats["reconnects"] == 5
