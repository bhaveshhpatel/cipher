"""
Coverage boost for services/demo_engine.py.
Targets: _run_demo_loop, start_demo (already_running), stop_demo (already_stopped),
         stop_demo (running with task), get_stats.
"""
import asyncio
from unittest.mock import AsyncMock, patch

from services.demo_engine import (
    is_running,
    get_stats,
    start_demo,
    stop_demo,
    _run_demo_loop,
)
import services.demo_engine as _de


def _reset():
    _de._running = False
    _de._task    = None
    _de._stats["ticks_emitted"]   = 0
    _de._stats["signals_emitted"] = 0
    _de._stats["errors"]          = 0


def setup_function():
    _reset()


def teardown_function():
    _reset()


# --- get_stats ---

def test_get_stats_initial():
    s = get_stats()
    assert s["running"] is False
    assert s["ticks_emitted"] == 0


# --- start_demo already_running ---

def test_start_demo_already_running():
    _de._running = True
    result = asyncio.run(start_demo())
    assert result["status"] == "already_running"
    _de._running = False


# --- stop_demo already_stopped ---

def test_stop_demo_already_stopped():
    result = asyncio.run(stop_demo())
    assert result["status"] == "already_stopped"


# --- start_demo then stop_demo ---

def test_start_and_stop_demo():
    async def _run():
        with patch("services.demo_engine.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            res_start = await start_demo(["AAPL"])
            assert res_start["status"] == "started"
            assert is_running() is True
            res_stop = await stop_demo()
            assert res_stop["status"] == "stopped"
            assert is_running() is False
    asyncio.run(_run())


# --- _run_demo_loop: CancelledError propagates, error branch ---

def test_run_demo_loop_cancelled():
    async def _run():
        with patch("services.demo_engine.asyncio.sleep",
                   new=AsyncMock(side_effect=asyncio.CancelledError)):
            try:
                await _run_demo_loop(["AAPL"])
            except asyncio.CancelledError:
                pass
    asyncio.run(_run())


def test_run_demo_loop_exception_increments_errors():
    call_count = [0]
    async def _boom(*a, **kw):
        call_count[0] += 1
        raise RuntimeError("boom")
    async def _run():
        with patch("services.demo_engine.asyncio.sleep", side_effect=_boom):
            try:
                await _run_demo_loop(["AAPL"])
            except RuntimeError:
                pass
    asyncio.run(_run())
    assert _de._stats["errors"] == 1
