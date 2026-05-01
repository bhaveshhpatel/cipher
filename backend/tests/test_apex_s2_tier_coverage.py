"""
apex/s2 — _refresh_tier_map + _process_tick Branch Coverage
============================================================
Closes #26 and #27 (pre-S3 hard gates).

Issue #26 — _refresh_tier_map (5 tests):
  - Happy path: tier_map rebuilt and timestamps updated
  - registry is None: early return, cache unchanged
  - registry not ready (is_ready() == False): early return, cache unchanged
  - assign_tiers raises: exception caught, warning logged, cache unchanged
  - int tier → str conversion applied: T1/T2/T3 written to cache

Issue #27 — _process_tick registry lookup path (3 tests):
  - get_registry() returns None: avg_volume falls back to 1.0, tick processed
  - registry present but symbol missing from _avg_volume_by_ticker: fallback to 1.0
  - registry lookup raises: exception swallowed, tick still processed

Inline fixes applied post-deliberation (2026-05-01):
  Fix 2 — patch target comment: lazy imports inside _refresh_tier_map are
    resolved at call time, so patching the source module is correct. If those
    imports are ever hoisted to module level, the patch targets must change.
  Fix 3 — removed unused `prices` and `oi` kwargs from _make_registry.
  Fix 6 — test_symbol_missing_from_avg_volume_uses_fallback now explicitly
    sets is_ready on the mock and documents why it is not the guard under test.

Issue #30 (2026-05-01) — Test isolation / module global teardown:
  `reset_tier_map_globals` autouse fixture added to TestRefreshTierMap.
  Saves and restores _tier_map_cache, _tier_map_ts, _tier_map_refresh_task
  around every test in the class. Prevents session-level pollution from
  sentinel values left behind by individual tests.

Post-deliberation follow-ups resolved in this file:
  #31 — happy path now asserts _tier_map_refresh_task is None or done post-call
  #32 — exception test now uses caplog to assert warning was emitted
  #34 — new test: inner registry exception path (_avg_volume_by_ticker.get raises)
  #35 — new test: assign_tiers returns empty dict {}
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from services.stream_worker import (
    _TICK_TYPE_TIMESALE,
    _TICK_SYMBOL,
    _TICK_LAST,
    _TICK_SIZE,
    _TICK_VOLUME,
    _TICK_TIMESTAMP,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timesale(
    symbol: str = "AAPL",
    last: float = 150.0,
    size: int = 10,
    volume: int = 500_000,
    ts: float | None = None,
) -> dict:
    return {
        "type":          _TICK_TYPE_TIMESALE,
        _TICK_SYMBOL:    symbol,
        _TICK_LAST:      last,
        _TICK_SIZE:      size,
        _TICK_VOLUME:    volume,
        _TICK_TIMESTAMP: ts if ts is not None else time.time(),
    }


def _make_worker() -> Any:
    from services.stream_worker import StreamWorker
    q = asyncio.Queue()
    return StreamWorker(worker_id=1, symbols=["AAPL", "TSLA"], event_queue=q)


def _make_registry(
    watchlist: list[str] | None = None,
    avg_volume: dict | None = None,
    ready: bool = True,
) -> MagicMock:
    """Build a minimal symbol registry mock.

    Fix 3: removed unused `prices` and `oi` parameters — neither is accessed
    by any production code path covered in this file.
    """
    reg = MagicMock()
    reg.is_ready.return_value = ready
    reg._watchlist = watchlist or ["AAPL", "TSLA"]
    reg._avg_volume_by_ticker = avg_volume or {"AAPL": 10_000, "TSLA": 5_000}
    return reg


# ---------------------------------------------------------------------------
# Issue #26 — _refresh_tier_map branch coverage
# ---------------------------------------------------------------------------

class TestRefreshTierMap:
    """
    Tests for _refresh_tier_map().

    Isolation contract (#30):
    The `reset_tier_map_globals` autouse fixture below saves the three
    module-level globals (_tier_map_cache, _tier_map_ts, _tier_map_refresh_task)
    before each test and restores them in a `finally` block after, regardless
    of pass/fail. This prevents any sentinel value written by one test from
    leaking into subsequent tests in this class or in other test files that
    import services.stream_worker in the same pytest session.
    """

    @pytest.fixture(autouse=True)
    def reset_tier_map_globals(self):
        """
        Save and restore the three module-level tier_map globals around
        every test in TestRefreshTierMap.

        Globals protected:
          - sw._tier_map_cache        (dict[str, str])
          - sw._tier_map_ts           (float)
          - sw._tier_map_refresh_task (asyncio.Task | None)
          - sw._tier_map_refresh_in_progress (bool)  (#24)

        Pattern: snapshot before yield, unconditional restore in finally.
        This makes each test hermetic — it can freely mutate module state
        without affecting any other test in the session.
        """
        import services.stream_worker as sw
        saved_cache    = dict(sw._tier_map_cache)
        saved_ts       = sw._tier_map_ts
        saved_task     = sw._tier_map_refresh_task
        saved_progress = sw._tier_map_refresh_in_progress
        try:
            yield
        finally:
            sw._tier_map_cache               = saved_cache
            sw._tier_map_ts                  = saved_ts
            sw._tier_map_refresh_task        = saved_task
            sw._tier_map_refresh_in_progress = saved_progress

    @pytest.mark.asyncio
    async def test_happy_path_rebuilds_cache(self):
        """
        Happy path: registry ready, assign_tiers returns a valid map.
        Cache must be populated, timestamp updated, and _tier_map_refresh_task
        must be None or done after the coroutine returns (#31 S2-POST-9).

        Fix 2 — patch target note: _refresh_tier_map uses lazy imports
        (inside the function body), so patching the source modules
        services.symbol_registry and services.tier_engine is correct.
        If those imports are ever hoisted to module level, the patch
        targets must be updated to services.stream_worker.<name>.
        """
        import services.stream_worker as sw

        sw._tier_map_cache = {}
        sw._tier_map_ts = 0.0

        reg = _make_registry(watchlist=["AAPL", "TSLA"])

        async def fake_assign_tiers(quotes):
            return {q.symbol: 1 for q in quotes}

        with patch("services.symbol_registry.get_registry", return_value=reg):
            with patch("services.tier_engine.assign_tiers", side_effect=fake_assign_tiers):
                from services.stream_worker import _refresh_tier_map
                await _refresh_tier_map()

        assert sw._tier_map_cache.get("AAPL") == "T1"
        assert sw._tier_map_cache.get("TSLA") == "T1"
        assert sw._tier_map_ts > 0.0
        # #31: _refresh_tier_map is a coroutine — no task is created inside it.
        # After awaiting it directly, _tier_map_refresh_task must be None or done.
        assert (
            sw._tier_map_refresh_task is None
            or sw._tier_map_refresh_task.done()
        ), "_tier_map_refresh_task must be None or done after _refresh_tier_map completes"

    @pytest.mark.asyncio
    async def test_registry_none_skips_update(self):
        """
        When get_registry() returns None, function returns early.
        Cache and timestamp must remain unchanged.
        """
        import services.stream_worker as sw

        sentinel = {"AAPL": "T1"}
        sw._tier_map_cache = dict(sentinel)
        original_ts = sw._tier_map_ts = 42.0

        with patch("services.symbol_registry.get_registry", return_value=None):
            from services.stream_worker import _refresh_tier_map
            await _refresh_tier_map()

        assert sw._tier_map_cache == sentinel
        assert sw._tier_map_ts == original_ts

    @pytest.mark.asyncio
    async def test_registry_not_ready_skips_update(self):
        """
        When registry.is_ready() returns False, function returns early.
        Cache and timestamp must remain unchanged.
        """
        import services.stream_worker as sw

        sentinel = {"TSLA": "T2"}
        sw._tier_map_cache = dict(sentinel)
        original_ts = sw._tier_map_ts = 99.0

        reg = _make_registry(ready=False)

        with patch("services.symbol_registry.get_registry", return_value=reg):
            from services.stream_worker import _refresh_tier_map
            await _refresh_tier_map()

        assert sw._tier_map_cache == sentinel
        assert sw._tier_map_ts == original_ts

    @pytest.mark.asyncio
    async def test_assign_tiers_exception_does_not_raise(self, caplog):
        """
        When assign_tiers raises, the exception must be caught and logged
        as a warning (#32 S2-POST-10). Cache must remain unchanged (non-fatal).

        #32: caplog asserts a WARNING-level entry is emitted containing
        meaningful context from the exception. A silent swallow (exception
        caught, nothing logged) will now fail this test.
        """
        import services.stream_worker as sw

        sentinel = {"SPY": "T1"}
        sw._tier_map_cache = dict(sentinel)
        sw._tier_map_ts = 77.0

        reg = _make_registry(watchlist=["SPY"])

        async def boom(quotes):
            raise RuntimeError("tier_engine down")

        with caplog.at_level(logging.WARNING, logger="stream_worker"):
            with patch("services.symbol_registry.get_registry", return_value=reg):
                with patch("services.tier_engine.assign_tiers", side_effect=boom):
                    from services.stream_worker import _refresh_tier_map
                    await _refresh_tier_map()  # must not raise

        assert sw._tier_map_cache == sentinel
        # #32: assert the warning was actually logged
        warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_records, "Expected a WARNING log entry when assign_tiers raises"
        assert any(
            "tier_engine down" in r.message or "_refresh_tier_map" in r.message
            for r in warning_records
        ), "WARNING log must contain meaningful context from the exception"

    @pytest.mark.asyncio
    async def test_int_tiers_converted_to_strings(self):
        """
        assign_tiers returns int tiers (1/2/3). Cache must store string
        values "T1"/"T2"/"T3" — not raw integers.
        """
        import services.stream_worker as sw

        sw._tier_map_cache = {}
        sw._tier_map_ts = 0.0

        reg = _make_registry(watchlist=["AAPL", "TSLA", "SPY"])

        async def fake_assign_tiers(quotes):
            mapping = {"AAPL": 1, "TSLA": 2, "SPY": 3}
            return {q.symbol: mapping.get(q.symbol, 3) for q in quotes}

        with patch("services.symbol_registry.get_registry", return_value=reg):
            with patch("services.tier_engine.assign_tiers", side_effect=fake_assign_tiers):
                from services.stream_worker import _refresh_tier_map
                await _refresh_tier_map()

        assert sw._tier_map_cache.get("AAPL") == "T1"
        assert sw._tier_map_cache.get("TSLA") == "T2"
        assert sw._tier_map_cache.get("SPY") == "T3"

    @pytest.mark.asyncio
    async def test_assign_tiers_returns_empty_dict(self):
        """
        #35 S2-POST-13: assign_tiers returns {} (e.g. tier_engine cold start).

        Expected behaviour:
          - _tier_map_cache is set to {} (no symbol has a tier)
          - _tier_map_ts > 0.0 (timestamp IS updated — refresh did complete)
          - No exception raised

        Downstream impact: every symbol falls back to T3 until the next
        refresh cycle, because ThresholdReconciler defaults missing symbols
        to T3. This is documented as an operational edge case in issue #35.
        """
        import services.stream_worker as sw

        sw._tier_map_cache = {"AAPL": "T1"}  # pre-existing cache
        sw._tier_map_ts = 0.0

        reg = _make_registry(watchlist=["AAPL", "TSLA"])

        async def fake_assign_tiers(quotes):
            return {}  # empty — tier_engine cold start scenario

        with patch("services.symbol_registry.get_registry", return_value=reg):
            with patch("services.tier_engine.assign_tiers", side_effect=fake_assign_tiers):
                from services.stream_worker import _refresh_tier_map
                await _refresh_tier_map()  # must not raise

        assert sw._tier_map_cache == {}, "Empty assign_tiers result must write empty cache"
        assert sw._tier_map_ts > 0.0, "Timestamp must be updated even when assign_tiers returns {}"


# ---------------------------------------------------------------------------
# Issue #27 — _process_tick registry lookup branch coverage
# ---------------------------------------------------------------------------

class TestProcessTickRegistryLookup:

    def test_get_registry_returns_none_uses_fallback_avg_volume(self):
        """
        When get_registry() returns None, avg_volume must fall back to 1.0.
        The tick must still be processed and land in _pending.
        """
        w = _make_worker()
        tick = _timesale(symbol="AAPL", last=100.0, size=5, volume=50_000)

        with patch("services.symbol_registry.get_registry", return_value=None):
            w._process_tick(tick)

        assert "AAPL" in w._pending
        # volume_ratio = 50_000 / 1.0 (fallback baseline)
        assert w._pending["AAPL"].volume_ratio == pytest.approx(50_000.0)

    def test_symbol_missing_from_avg_volume_uses_fallback(self):
        """
        Registry is present but the symbol is not in _avg_volume_by_ticker.
        .get() returns 0, which triggers the fallback to 1.0.

        Fix 6: is_ready is set explicitly here. Note that _process_tick does
        NOT currently gate on is_ready — avg_volume is read directly from
        _avg_volume_by_ticker. is_ready is set so this mock accurately
        reflects a live registry shape. If a future story adds an is_ready
        guard to _process_tick, this test will need a companion covering
        the not-ready branch.
        """
        w = _make_worker()
        tick = _timesale(symbol="NVDA", last=400.0, size=2, volume=10_000)

        reg = MagicMock()
        reg.is_ready.return_value = True  # Fix 6: explicit, documents intent
        reg._avg_volume_by_ticker = {}  # NVDA absent

        with patch("services.symbol_registry.get_registry", return_value=reg):
            w._process_tick(tick)

        assert "NVDA" in w._pending
        assert w._pending["NVDA"].volume_ratio == pytest.approx(10_000.0)

    def test_registry_lookup_exception_still_processes_tick(self):
        """
        If get_registry() raises, the exception must be swallowed and the
        tick must still be accumulated with fallback avg_volume=1.0.

        Note: this covers get_registry() itself raising. The inner path
        where reg._avg_volume_by_ticker.get() raises is tracked in #34.
        """
        w = _make_worker()
        tick = _timesale(symbol="TSLA", last=200.0, size=3, volume=1_000)

        with patch(
            "services.symbol_registry.get_registry",
            side_effect=Exception("registry exploded"),
        ):
            w._process_tick(tick)  # must not raise

        assert "TSLA" in w._pending
        assert w._pending["TSLA"].volume_ratio == pytest.approx(1_000.0)

    def test_inner_registry_exception_still_processes_tick(self):
        """
        #34 S2-POST-12: get_registry() succeeds and returns a non-None registry,
        but reg._avg_volume_by_ticker.get() raises a RuntimeError (e.g. a
        property getter that raises on access).

        Expected behaviour:
          - The exception is swallowed by the try/except in _process_tick
          - avg_volume falls back to 1.0
          - The tick still lands in _pending with volume_ratio = volume / 1.0
          - No exception propagates to the caller

        This is distinct from the get_registry() raise covered by
        test_registry_lookup_exception_still_processes_tick: here the
        registry object itself is returned, but its internal .get() raises.
        """
        w = _make_worker()
        tick = _timesale(symbol="MSFT", last=300.0, size=4, volume=8_000)

        reg = MagicMock()
        reg.is_ready.return_value = True
        # .get() raises instead of returning a value
        reg._avg_volume_by_ticker.get.side_effect = RuntimeError("attribute error")

        with patch("services.symbol_registry.get_registry", return_value=reg):
            w._process_tick(tick)  # must not raise

        assert "MSFT" in w._pending, "Tick must land in _pending despite inner registry exception"
        # avg_volume falls back to 1.0 → volume_ratio = 8_000 / 1.0
        assert w._pending["MSFT"].volume_ratio == pytest.approx(8_000.0)
