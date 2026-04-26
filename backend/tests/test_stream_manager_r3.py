"""
Round 3 tests for services/stream_manager.py

Covers S-06: stale_workers count and worker_detail extension in
StreamManager.stats.

These tests are ADDITIVE to the existing test_stream_manager.py (Round 1/2).
They live in a separate file to avoid changing the committed SHA.

S-06 tests:
  SM-R3-01. stats dict includes the 'stale_workers' key
  SM-R3-02. stale_workers = 0 when all workers ticked within threshold
  SM-R3-03. stale_workers counts workers where last_tick_at is None
  SM-R3-04. stale_workers counts workers whose last_tick_at is too old
  SM-R3-05. worker_detail rows include 'last_tick_at' and 'session_ticks'
  SM-R3-06. stale_workers = total workers when no workers exist (empty list)
  SM-R3-07. stale_workers threshold constant is 60.0 seconds
"""
import asyncio
import time as _time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.stream_manager import StreamManager, _STALE_WORKER_THRESHOLD_S
from services.stream_worker import StreamWorker


def _make_manager() -> StreamManager:
    """Return a StreamManager with a mocked registry (size=0, no symbols)."""
    registry = MagicMock()
    registry.all_symbols.return_value = []
    registry.size.return_value = 0

    async def _noop(raw):
        pass

    mgr = StreamManager(registry=registry, process_fn=_noop)
    return mgr


def _make_worker(worker_id: int = 0, last_tick_at=None, session_ticks: int = 0) -> MagicMock:
    """Return a MagicMock that mimics a StreamWorker with S-04 stats."""
    w = MagicMock(spec=StreamWorker)
    w.worker_id      = worker_id
    w.symbols        = ["SYM1"]
    w._ticks         = 10
    w._errors        = 0
    w._reconnects    = 0
    w._last_tick_at  = last_tick_at
    w._session_ticks = session_ticks
    w.stats = {
        "worker_id":       worker_id,
        "symbols":         1,
        "ticks":           10,
        "errors":          0,
        "reconnects":      0,
        "startup_delay_s": 0.0,
        "last_tick_at":    last_tick_at,
        "session_ticks":   session_ticks,
    }
    return w


# SM-R3-01
def test_stats_includes_stale_workers_key():
    mgr = _make_manager()
    assert "stale_workers" in mgr.stats


# SM-R3-02
def test_stale_workers_zero_when_all_recent():
    mgr = _make_manager()
    now = _time.time()
    mgr._workers = [
        _make_worker(0, last_tick_at=now - 5.0),   # ticked 5s ago -- fresh
        _make_worker(1, last_tick_at=now - 10.0),  # ticked 10s ago -- fresh
    ]
    assert mgr.stats["stale_workers"] == 0


# SM-R3-03
def test_stale_workers_counts_none_last_tick():
    mgr = _make_manager()
    now = _time.time()
    mgr._workers = [
        _make_worker(0, last_tick_at=None),          # never ticked -- stale
        _make_worker(1, last_tick_at=now - 5.0),     # fresh
        _make_worker(2, last_tick_at=None),          # never ticked -- stale
    ]
    assert mgr.stats["stale_workers"] == 2


# SM-R3-04
def test_stale_workers_counts_old_last_tick():
    mgr = _make_manager()
    now = _time.time()
    mgr._workers = [
        _make_worker(0, last_tick_at=now - 120.0),  # 2 min ago -- stale
        _make_worker(1, last_tick_at=now - 5.0),    # fresh
        _make_worker(2, last_tick_at=now - 90.0),   # 90s ago -- stale
    ]
    assert mgr.stats["stale_workers"] == 2


# SM-R3-05
def test_worker_detail_includes_last_tick_at_and_session_ticks():
    mgr = _make_manager()
    now = _time.time()
    mgr._workers = [
        _make_worker(0, last_tick_at=now - 3.0, session_ticks=42),
    ]
    detail = mgr.stats["worker_detail"]
    assert len(detail) == 1
    assert "last_tick_at"  in detail[0]
    assert "session_ticks" in detail[0]
    assert detail[0]["session_ticks"] == 42


# SM-R3-06
def test_stale_workers_zero_when_no_workers():
    """An empty worker list should yield stale_workers=0, not crash."""
    mgr = _make_manager()
    mgr._workers = []
    assert mgr.stats["stale_workers"] == 0


# SM-R3-07
def test_stale_worker_threshold_is_60s():
    assert _STALE_WORKER_THRESHOLD_S == 60.0
