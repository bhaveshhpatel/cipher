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
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
    prices: dict | None = None,
    oi: dict | None = None,
    ready: bool = True,
) -> MagicMock:
    """Build a minimal symbol registry mock."""
    reg = MagicMock()
    reg.is_ready.return_value = ready
    reg._watchlist = watchlist or ["AAPL", "TSLA"]
    reg._avg_volume_by_ticker = avg_volume or {"AAPL": 10_000, "TSLA": 5_000}
    reg.stock_price = lambda ticker: (prices or {}).get(ticker, 100.0)
    reg._oi_by_ticker = oi or {}
    return reg


# ---------------------------------------------------------------------------
# Issue #26 — _refresh_tier_map branch coverage
# ---------------------------------------------------------------------------

class TestRefreshTierMap:

    @pytest.mark.asyncio
    async def test_happy_path_rebuilds_cache(self):
        """
        Happy path: registry ready, assign_tiers returns a valid map.
        Cache must be populated and timestamp updated.
        Patches the lazy imports inside _refresh_tier_map via their
        fully-qualified module paths.
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
    async def test_assign_tiers_exception_does_not_raise(self):
        """
        When assign_tiers raises, the exception must be caught and logged
        as a warning. Cache must remain unchanged (non-fatal).
        """
        import services.stream_worker as sw

        sentinel = {"SPY": "T1"}
        sw._tier_map_cache = dict(sentinel)
        sw._tier_map_ts = 77.0

        reg = _make_registry(watchlist=["SPY"])

        async def boom(quotes):
            raise RuntimeError("tier_engine down")

        with patch("services.symbol_registry.get_registry", return_value=reg):
            with patch("services.tier_engine.assign_tiers", side_effect=boom):
                from services.stream_worker import _refresh_tier_map
                await _refresh_tier_map()  # must not raise

        assert sw._tier_map_cache == sentinel

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
        """
        w = _make_worker()
        tick = _timesale(symbol="NVDA", last=400.0, size=2, volume=10_000)

        reg = MagicMock()
        reg._avg_volume_by_ticker = {}  # NVDA absent

        with patch("services.symbol_registry.get_registry", return_value=reg):
            w._process_tick(tick)

        assert "NVDA" in w._pending
        assert w._pending["NVDA"].volume_ratio == pytest.approx(10_000.0)

    def test_registry_lookup_exception_still_processes_tick(self):
        """
        If get_registry() raises, the exception must be swallowed and the
        tick must still be accumulated with fallback avg_volume=1.0.
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
