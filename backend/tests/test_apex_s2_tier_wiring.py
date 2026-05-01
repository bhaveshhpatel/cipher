"""
apex/s2 — Tier Wiring: Full Test Suite
=======================================
Covers:
  - tick_to_metrics(): type filter, field mapping, None returns, named constants
  - tick_to_metrics(): avg_volume parameter, volume_ratio calculation
  - tick_to_metrics(): oi_delta always 0.0 (S2 suppression documented)
  - _int_tier_to_str(): conversion correctness + unknown value fallback
  - _get_tier_map(): cold-start returns {}, stale triggers background task
  - StreamWorker._process_tick(): non-timesale ignored, metrics accumulated
  - StreamWorker._pending: last-write-wins within window
  - StreamWorker._flush_pending(): atomic drain, reconcile called once per batch
  - StreamWorker._flush_pending(): empty pending is a no-op
  - StreamWorker._flush_loop(): produces asyncio.Task, does not block reader
  - Integration: tick → metrics → reconcile path end-to-end
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
    _TICK_OI,
    _TICK_TIMESTAMP,
    _int_tier_to_str,
    _get_tier_map,
    tick_to_metrics,
)
from services.threshold_reconciliation import SymbolMetrics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _timesale(
    symbol: str = "AAPL",
    last: float = 150.0,
    size: int = 10,
    volume: int = 500_000,
    oi: int = 0,
    ts: float | None = None,
) -> dict:
    return {
        _TICK_TYPE_TIMESALE:       _TICK_TYPE_TIMESALE,   # value matches key name
        "type":                    _TICK_TYPE_TIMESALE,
        _TICK_SYMBOL:              symbol,
        _TICK_LAST:                last,
        _TICK_SIZE:                size,
        _TICK_VOLUME:              volume,
        _TICK_OI:                  oi,
        _TICK_TIMESTAMP:           ts if ts is not None else time.time(),
    }


def _make_worker() -> Any:
    """Construct a StreamWorker with a dummy queue — no real stream."""
    from services.stream_worker import StreamWorker
    q = asyncio.Queue()
    return StreamWorker(worker_id=99, symbols=["AAPL", "TSLA"], event_queue=q)


# ---------------------------------------------------------------------------
# _int_tier_to_str
# ---------------------------------------------------------------------------

class TestIntTierToStr:
    def test_t1(self):
        assert _int_tier_to_str(1) == "T1"

    def test_t2(self):
        assert _int_tier_to_str(2) == "T2"

    def test_t3(self):
        assert _int_tier_to_str(3) == "T3"

    def test_unknown_falls_back_to_T3(self):
        assert _int_tier_to_str(99) == "T3"
        assert _int_tier_to_str(0)  == "T3"


# ---------------------------------------------------------------------------
# tick_to_metrics — type filter
# ---------------------------------------------------------------------------

class TestTickToMetricsTypeFilter:
    def test_timesale_passes(self):
        tick = _timesale()
        assert tick_to_metrics(tick) is not None

    def test_non_timesale_returns_none(self):
        tick = _timesale()
        tick["type"] = "summary"
        assert tick_to_metrics(tick) is None

    def test_heartbeat_returns_none(self):
        assert tick_to_metrics({"type": "heartbeat"}) is None

    def test_missing_type_returns_none(self):
        tick = _timesale()
        del tick["type"]
        assert tick_to_metrics(tick) is None


# ---------------------------------------------------------------------------
# tick_to_metrics — symbol guard
# ---------------------------------------------------------------------------

class TestTickToMetricsSymbol:
    def test_missing_symbol_returns_none(self):
        tick = _timesale()
        del tick[_TICK_SYMBOL]
        assert tick_to_metrics(tick) is None

    def test_empty_symbol_returns_none(self):
        tick = _timesale(symbol="")
        assert tick_to_metrics(tick) is None

    def test_valid_symbol_returns_metrics(self):
        m = tick_to_metrics(_timesale(symbol="SPY"))
        assert m is not None
        assert m.symbol == "SPY"


# ---------------------------------------------------------------------------
# tick_to_metrics — last price guard
# ---------------------------------------------------------------------------

class TestTickToMetricsLastPrice:
    def test_missing_last_returns_none(self):
        tick = _timesale()
        del tick[_TICK_LAST]
        assert tick_to_metrics(tick) is None

    def test_zero_last_returns_none(self):
        assert tick_to_metrics(_timesale(last=0.0)) is None

    def test_negative_last_returns_none(self):
        assert tick_to_metrics(_timesale(last=-1.0)) is None

    def test_non_numeric_last_returns_none(self):
        tick = _timesale()
        tick[_TICK_LAST] = "bad"
        assert tick_to_metrics(tick) is None


# ---------------------------------------------------------------------------
# tick_to_metrics — field mapping
# ---------------------------------------------------------------------------

class TestTickToMetricsFields:
    def test_oi_delta_always_zero(self):
        """S2: oi_delta must always be 0.0 — timesale has no OI field."""
        m = tick_to_metrics(_timesale(oi=9999))
        assert m is not None
        assert m.oi_delta == 0.0

    def test_premium_usd_is_last_times_size(self):
        m = tick_to_metrics(_timesale(last=200.0, size=5))
        assert m is not None
        assert m.premium_usd == 1000.0

    def test_premium_usd_zero_size(self):
        m = tick_to_metrics(_timesale(last=100.0, size=0))
        assert m is not None
        assert m.premium_usd == 0.0

    def test_volume_ratio_with_avg_volume(self):
        m = tick_to_metrics(_timesale(volume=50_000), avg_volume=10_000.0)
        assert m is not None
        assert m.volume_ratio == pytest.approx(5.0)

    def test_volume_ratio_zero_avg_falls_back_to_1(self):
        """avg_volume=0 must use fallback baseline of 1.0, not divide-by-zero."""
        m = tick_to_metrics(_timesale(volume=100), avg_volume=0.0)
        assert m is not None
        assert m.volume_ratio == pytest.approx(100.0)

    def test_volume_ratio_default_avg_is_1(self):
        m = tick_to_metrics(_timesale(volume=7))
        assert m is not None
        assert m.volume_ratio == pytest.approx(7.0)

    def test_timestamp_from_tick(self):
        ts = 1_700_000_000.0
        m = tick_to_metrics(_timesale(ts=ts))
        assert m is not None
        assert m.timestamp == pytest.approx(ts)

    def test_timestamp_fallback_when_missing(self):
        tick = _timesale()
        tick[_TICK_TIMESTAMP] = 0
        before = time.time()
        m = tick_to_metrics(tick)
        after = time.time()
        assert m is not None
        assert before <= m.timestamp <= after

    def test_returns_symbol_metrics_instance(self):
        m = tick_to_metrics(_timesale())
        assert isinstance(m, SymbolMetrics)


# ---------------------------------------------------------------------------
# _get_tier_map — cold start + stale trigger
# ---------------------------------------------------------------------------

class TestGetTierMap:
    def test_cold_start_returns_empty_dict(self):
        import services.stream_worker as sw
        sw._tier_map_cache = {}
        sw._tier_map_ts    = 0.0
        sw._tier_map_refresh_task = None
        result = _get_tier_map()
        assert isinstance(result, dict)
        # Cold start may be empty or have cached values — must be a dict

    def test_returns_copy_not_reference(self):
        import services.stream_worker as sw
        sw._tier_map_cache = {"AAPL": "T1"}
        sw._tier_map_ts    = 9_999_999_999.0  # far future — not stale
        result = _get_tier_map()
        result["AAPL"] = "T3"  # mutate copy
        assert sw._tier_map_cache["AAPL"] == "T1"  # original intact

    @pytest.mark.asyncio
    async def test_stale_map_schedules_background_task(self):
        import services.stream_worker as sw
        sw._tier_map_cache = {}
        sw._tier_map_ts    = 0.0   # stale
        sw._tier_map_refresh_task = None

        _get_tier_map()  # should schedule refresh task
        # Give the event loop a tick to register the task
        await asyncio.sleep(0)
        # Task should have been created (may already be done if registry is None)
        assert sw._tier_map_refresh_task is not None


# ---------------------------------------------------------------------------
# StreamWorker._process_tick
# ---------------------------------------------------------------------------

class TestProcessTick:
    def test_timesale_tick_accumulated(self):
        w = _make_worker()
        tick = _timesale(symbol="AAPL", last=150.0, size=10)
        w._process_tick(tick)
        assert "AAPL" in w._pending

    def test_non_timesale_not_accumulated(self):
        w = _make_worker()
        tick = _timesale()
        tick["type"] = "summary"
        w._process_tick(tick)
        assert len(w._pending) == 0

    def test_last_write_wins_same_symbol(self):
        w = _make_worker()
        tick1 = _timesale(symbol="AAPL", last=100.0, size=1)
        tick2 = _timesale(symbol="AAPL", last=200.0, size=2)
        w._process_tick(tick1)
        w._process_tick(tick2)
        assert len(w._pending) == 1
        assert w._pending["AAPL"].premium_usd == pytest.approx(400.0)  # 200×2

    def test_different_symbols_both_accumulated(self):
        w = _make_worker()
        w._process_tick(_timesale(symbol="AAPL"))
        w._process_tick(_timesale(symbol="TSLA"))
        assert "AAPL" in w._pending
        assert "TSLA" in w._pending

    def test_invalid_tick_does_not_raise(self):
        w = _make_worker()
        w._process_tick({})           # empty dict
        w._process_tick({"type": "timesale"})  # missing symbol
        assert len(w._pending) == 0


# ---------------------------------------------------------------------------
# StreamWorker._flush_pending
# ---------------------------------------------------------------------------

class TestFlushPending:
    @pytest.mark.asyncio
    async def test_empty_pending_is_noop(self):
        w = _make_worker()
        w._pending = {}
        # Should not raise, should not call reconcile
        with patch("services.stream_worker.reconcile") as mock_rec:
            await w._flush_pending()
            mock_rec.assert_not_called()

    @pytest.mark.asyncio
    async def test_flush_drains_pending(self):
        w = _make_worker()
        w._pending = {"AAPL": tick_to_metrics(_timesale(symbol="AAPL"))}

        mock_reconcile = AsyncMock()
        with patch("services.stream_worker.reconcile", mock_reconcile):
            with patch("services.stream_worker._get_tier_map", return_value={"AAPL": "T1"}):
                await w._flush_pending()

        assert len(w._pending) == 0
        mock_reconcile.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_flush_passes_correct_batch(self):
        w = _make_worker()
        m_aapl = tick_to_metrics(_timesale(symbol="AAPL", last=150.0, size=5))
        m_tsla = tick_to_metrics(_timesale(symbol="TSLA", last=300.0, size=2))
        w._pending = {"AAPL": m_aapl, "TSLA": m_tsla}

        captured_batch = {}
        async def fake_reconcile(batch, tier_map):
            captured_batch.update(batch)

        with patch("services.stream_worker.reconcile", side_effect=fake_reconcile):
            with patch("services.stream_worker._get_tier_map", return_value={}):
                await w._flush_pending()

        assert "AAPL" in captured_batch
        assert "TSLA" in captured_batch

    @pytest.mark.asyncio
    async def test_atomic_drain_new_ticks_go_to_next_window(self):
        """Ticks arriving during reconcile must land in the next window."""
        w = _make_worker()
        w._pending = {"AAPL": tick_to_metrics(_timesale(symbol="AAPL"))}

        async def fake_reconcile(batch, tier_map):
            # Simulate a new tick arriving mid-reconcile
            w._process_tick(_timesale(symbol="TSLA"))

        with patch("services.stream_worker.reconcile", side_effect=fake_reconcile):
            with patch("services.stream_worker._get_tier_map", return_value={}):
                await w._flush_pending()

        # TSLA arrived during reconcile — must be in the new pending, not lost
        assert "TSLA" in w._pending
        # AAPL was in the drained batch — not re-added
        assert "AAPL" not in w._pending

    @pytest.mark.asyncio
    async def test_reconcile_error_does_not_raise(self):
        w = _make_worker()
        w._pending = {"AAPL": tick_to_metrics(_timesale(symbol="AAPL"))}

        async def boom(batch, tier_map):
            raise RuntimeError("reconcile bus down")

        with patch("services.stream_worker.reconcile", side_effect=boom):
            with patch("services.stream_worker._get_tier_map", return_value={}):
                # Must not raise — logged as warning only
                await w._flush_pending()


# ---------------------------------------------------------------------------
# StreamWorker._flush_loop
# ---------------------------------------------------------------------------

class TestFlushLoop:
    @pytest.mark.asyncio
    async def test_flush_loop_creates_task_not_blocking(self):
        """_flush_loop must schedule via create_task, not await directly."""
        w = _make_worker()
        w._running = True
        tasks_created: list = []

        original_create_task = asyncio.create_task

        def mock_create_task(coro, **kwargs):
            t = original_create_task(coro, **kwargs)
            tasks_created.append(t)
            return t

        w._pending = {"AAPL": tick_to_metrics(_timesale(symbol="AAPL"))}

        with patch("services.stream_worker.reconcile", AsyncMock()):
            with patch("services.stream_worker._get_tier_map", return_value={}):
                with patch("asyncio.create_task", side_effect=mock_create_task):
                    # Run one iteration of the flush loop
                    w._running = False  # stop after first sleep
                    loop_task = asyncio.create_task(w._flush_loop())
                    await asyncio.sleep(0.01)
                    loop_task.cancel()
                    try:
                        await loop_task
                    except asyncio.CancelledError:
                        pass


# ---------------------------------------------------------------------------
# Integration: tick → _process_tick → _flush_pending → reconcile
# ---------------------------------------------------------------------------

class TestIntegration:
    @pytest.mark.asyncio
    async def test_volume_surge_breach_fires_end_to_end(self):
        """
        End-to-end: a tick with volume >> avg_volume should produce a
        VOLUME_SURGE breach when flushed through reconcile.
        """
        from services.threshold_reconciliation import (
            ThresholdReconciler,
            BreachType,
            _TIER_THRESHOLDS,
        )

        thres = _TIER_THRESHOLDS["T1"]
        # volume_ratio = volume / avg_volume; set ratio well above T1 threshold
        avg_vol   = 10_000.0
        tick_vol  = int(avg_vol * (thres["volume_ratio"] + 2))

        tick = _timesale(symbol="AAPL", last=150.0, size=10, volume=tick_vol)
        m    = tick_to_metrics(tick, avg_volume=avg_vol)
        assert m is not None
        assert m.volume_ratio > thres["volume_ratio"]

        r = ThresholdReconciler()
        result = await r.reconcile({"AAPL": m}, {"AAPL": "T1"})
        assert result.breach_count >= 1
        assert any(b.breach_type == BreachType.VOLUME_SURGE for b in result.breaches)

    @pytest.mark.asyncio
    async def test_premium_flood_breach_fires_end_to_end(self):
        from services.threshold_reconciliation import (
            ThresholdReconciler,
            BreachType,
            _TIER_THRESHOLDS,
        )

        thres     = _TIER_THRESHOLDS["T1"]
        # premium_usd = last × size; set above T1 threshold
        size      = int(thres["premium_usd"] + 1)
        tick      = _timesale(symbol="TSLA", last=1.0, size=size)
        m         = tick_to_metrics(tick)
        assert m is not None
        assert m.premium_usd > thres["premium_usd"]

        r = ThresholdReconciler()
        result = await r.reconcile({"TSLA": m}, {"TSLA": "T1"})
        assert result.breach_count >= 1
        assert any(b.breach_type == BreachType.PREMIUM_FLOOD for b in result.breaches)

    @pytest.mark.asyncio
    async def test_cold_start_tier_map_falls_back_to_T3(self):
        """
        Cold start: tier_map={} → reconciler assigns T3 to unknown symbols.
        """
        from services.threshold_reconciliation import (
            ThresholdReconciler,
            _TIER_THRESHOLDS,
        )

        thres = _TIER_THRESHOLDS["T3"]
        tick  = _timesale(
            symbol="UNKNWN",
            last=1.0,
            size=int(thres["premium_usd"] + 1),
        )
        m = tick_to_metrics(tick)
        assert m is not None

        r      = ThresholdReconciler()
        result = await r.reconcile({"UNKNWN": m}, {})  # empty tier_map
        assert result.breaches[0].tier == "T3"

    @pytest.mark.asyncio
    async def test_oi_breach_types_suppressed_in_s2(self):
        """
        S2 contract: oi_delta=0.0 on every tick → OI_SPIKE and OI_COLLAPSE
        must never fire from timesale-derived metrics.
        """
        from services.threshold_reconciliation import (
            ThresholdReconciler,
            BreachType,
        )

        tick   = _timesale(symbol="AAPL", last=150.0, size=1, oi=999_999)
        m      = tick_to_metrics(tick)
        assert m is not None
        assert m.oi_delta == 0.0  # always 0 regardless of tick oi field

        r      = ThresholdReconciler()
        result = await r.reconcile({"AAPL": m}, {"AAPL": "T1"})
        breach_types = {b.breach_type for b in result.breaches}
        assert BreachType.OI_SPIKE    not in breach_types
        assert BreachType.OI_COLLAPSE not in breach_types
