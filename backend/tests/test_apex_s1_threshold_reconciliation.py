"""
apex/s1 — Threshold Reconciliation: Full Test Suite
====================================================
Covers:
  - BreachType enum values
  - SymbolMetrics / ThresholdBreach / ReconcileResult dataclasses
  - _epoch_minute + _breach_key helpers
  - ThresholdReconciler._metrics_complete (static) — None AND NaN guards
  - ThresholdReconciler._evaluate (static) — all four breach paths
  - ThresholdReconciler.get_thresholds_for_tier — known + unknown tier
  - ThresholdReconciler.reset_dedup_cache
  - ThresholdReconciler.reconcile — happy path, dedup, skip incomplete,
    emit_fn forwarding, emit_fn exception resilience, lock serialisation,
    elapsed_ms populated
  - Module-level get_reconciler singleton + reconcile() wrapper
  - _maybe_evict under cap pressure
"""

from __future__ import annotations

import asyncio
import time
from typing import Dict, List

import pytest

from services.threshold_reconciliation import (
    BreachType,
    ReconcileResult,
    SymbolMetrics,
    ThresholdBreach,
    ThresholdReconciler,
    _breach_key,
    _epoch_minute,
    _TIER_THRESHOLDS,
    get_reconciler,
    reconcile,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _metrics(
    symbol: str = "AAPL",
    oi_delta: float = 0.0,
    premium_usd: float = 0.0,
    volume_ratio: float = 1.0,
    ts: float | None = None,
) -> SymbolMetrics:
    return SymbolMetrics(
        symbol=symbol,
        oi_delta=oi_delta,
        premium_usd=premium_usd,
        volume_ratio=volume_ratio,
        timestamp=ts if ts is not None else time.time(),
    )


# ---------------------------------------------------------------------------
# Enum
# ---------------------------------------------------------------------------

class TestBreachType:
    def test_values_are_strings(self):
        assert BreachType.OI_SPIKE.value       == "oi_spike"
        assert BreachType.PREMIUM_FLOOD.value  == "premium_flood"
        assert BreachType.VOLUME_SURGE.value   == "volume_surge"
        assert BreachType.OI_COLLAPSE.value    == "oi_collapse"

    def test_all_four_members(self):
        assert len(BreachType) == 4


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

class TestDataclasses:
    def test_symbol_metrics_defaults(self):
        m = SymbolMetrics(symbol="SPY", oi_delta=0.1, premium_usd=1000, volume_ratio=2.0)
        assert m.symbol == "SPY"
        assert isinstance(m.timestamp, float)

    def test_threshold_breach_defaults(self):
        b = ThresholdBreach(
            symbol="SPY", breach_type=BreachType.OI_SPIKE,
            observed=0.15, threshold=0.10, tier="T1"
        )
        assert isinstance(b.timestamp, float)
        assert b.tier == "T1"

    def test_reconcile_result_breach_count(self):
        r = ReconcileResult(checked=5)
        assert r.breach_count == 0
        r.breaches.append(
            ThresholdBreach(symbol="X", breach_type=BreachType.OI_SPIKE,
                            observed=0.2, threshold=0.1, tier="T1")
        )
        assert r.breach_count == 1

    def test_reconcile_result_defaults(self):
        r = ReconcileResult()
        assert r.checked == 0
        assert r.skipped == 0
        assert r.elapsed_ms == 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_epoch_minute_quantises(self):
        ts = 1_700_000_000.0
        assert _epoch_minute(ts) == int(ts // 60)

    def test_epoch_minute_same_minute(self):
        base = 1_700_000_000.0
        assert _epoch_minute(base) == _epoch_minute(base + 30.0)

    def test_epoch_minute_different_minute(self):
        base = 1_700_000_000.0
        assert _epoch_minute(base) != _epoch_minute(base + 61.0)

    def test_breach_key_structure(self):
        ts = 1_700_000_000.0
        key = _breach_key("AAPL", BreachType.OI_SPIKE, ts)
        assert key == ("AAPL", "oi_spike", _epoch_minute(ts))


# ---------------------------------------------------------------------------
# _metrics_complete — None AND NaN guards
# ---------------------------------------------------------------------------

class TestMetricsComplete:
    def test_complete_returns_true(self):
        assert ThresholdReconciler._metrics_complete(_metrics()) is True

    def test_none_oi_returns_false(self):
        m = _metrics()
        m.oi_delta = None  # type: ignore[assignment]
        assert ThresholdReconciler._metrics_complete(m) is False

    def test_none_premium_returns_false(self):
        m = _metrics()
        m.premium_usd = None  # type: ignore[assignment]
        assert ThresholdReconciler._metrics_complete(m) is False

    def test_none_volume_returns_false(self):
        m = _metrics()
        m.volume_ratio = None  # type: ignore[assignment]
        assert ThresholdReconciler._metrics_complete(m) is False

    def test_nan_oi_delta_returns_false(self):
        """NaN oi_delta must be treated as incomplete — not a valid breach signal."""
        m = _metrics(oi_delta=float("nan"))
        assert ThresholdReconciler._metrics_complete(m) is False

    def test_nan_premium_usd_returns_false(self):
        m = _metrics(premium_usd=float("nan"))
        assert ThresholdReconciler._metrics_complete(m) is False

    def test_nan_volume_ratio_returns_false(self):
        m = _metrics(volume_ratio=float("nan"))
        assert ThresholdReconciler._metrics_complete(m) is False

    @pytest.mark.asyncio
    async def test_nan_skipped_in_reconcile(self):
        """End-to-end: NaN metric must increment skipped, not checked."""
        m = _metrics(oi_delta=float("nan"))
        r = ThresholdReconciler()
        result = await r.reconcile({"AAPL": m}, {"AAPL": "T1"})
        assert result.skipped == 1
        assert result.checked == 0


# ---------------------------------------------------------------------------
# _evaluate (static): individual breach paths
# ---------------------------------------------------------------------------

class TestEvaluate:
    def _thres(self, tier: str = "T1") -> Dict:
        return dict(_TIER_THRESHOLDS[tier])

    def test_no_breach_below_all_thresholds(self):
        m = _metrics(oi_delta=0.01, premium_usd=1000, volume_ratio=1.0)
        assert ThresholdReconciler._evaluate(m, "T1", self._thres("T1")) == []

    def test_oi_spike_breach(self):
        thres = self._thres("T1")
        m = _metrics(oi_delta=thres["oi_spike_pct"])  # exact boundary
        breaches = ThresholdReconciler._evaluate(m, "T1", thres)
        types = [b.breach_type for b in breaches]
        assert BreachType.OI_SPIKE in types

    def test_oi_spike_above_threshold(self):
        thres = self._thres("T1")
        m = _metrics(oi_delta=thres["oi_spike_pct"] + 0.05)
        breaches = ThresholdReconciler._evaluate(m, "T1", thres)
        assert any(b.breach_type == BreachType.OI_SPIKE for b in breaches)

    def test_oi_collapse_breach(self):
        thres = self._thres("T1")
        m = _metrics(oi_delta=thres["oi_collapse_pct"])  # exact boundary
        breaches = ThresholdReconciler._evaluate(m, "T1", thres)
        assert any(b.breach_type == BreachType.OI_COLLAPSE for b in breaches)

    def test_oi_collapse_below_threshold(self):
        thres = self._thres("T1")
        m = _metrics(oi_delta=thres["oi_collapse_pct"] - 0.05)
        breaches = ThresholdReconciler._evaluate(m, "T1", thres)
        assert any(b.breach_type == BreachType.OI_COLLAPSE for b in breaches)

    def test_premium_flood_breach(self):
        thres = self._thres("T1")
        m = _metrics(premium_usd=thres["premium_usd"])
        breaches = ThresholdReconciler._evaluate(m, "T1", thres)
        assert any(b.breach_type == BreachType.PREMIUM_FLOOD for b in breaches)

    def test_volume_surge_breach(self):
        thres = self._thres("T2")
        m = _metrics(volume_ratio=thres["volume_ratio"])
        breaches = ThresholdReconciler._evaluate(m, "T2", thres)
        assert any(b.breach_type == BreachType.VOLUME_SURGE for b in breaches)

    def test_multiple_breaches_same_symbol(self):
        thres = self._thres("T3")
        m = _metrics(
            oi_delta=thres["oi_spike_pct"] + 0.1,
            premium_usd=thres["premium_usd"] + 1,
            volume_ratio=thres["volume_ratio"] + 1,
        )
        breaches = ThresholdReconciler._evaluate(m, "T3", thres)
        breach_types = {b.breach_type for b in breaches}
        assert BreachType.OI_SPIKE      in breach_types
        assert BreachType.PREMIUM_FLOOD in breach_types
        assert BreachType.VOLUME_SURGE  in breach_types

    def test_breach_observed_and_threshold_populated(self):
        thres = self._thres("T1")
        m = _metrics(oi_delta=thres["oi_spike_pct"] + 0.05)
        breach = next(
            b for b in ThresholdReconciler._evaluate(m, "T1", thres)
            if b.breach_type == BreachType.OI_SPIKE
        )
        assert breach.observed   == m.oi_delta
        assert breach.threshold  == thres["oi_spike_pct"]
        assert breach.tier       == "T1"
        assert breach.symbol     == m.symbol


# ---------------------------------------------------------------------------
# get_thresholds_for_tier
# ---------------------------------------------------------------------------

class TestGetThresholdsForTier:
    def setup_method(self):
        self.r = ThresholdReconciler()

    def test_known_tier_returns_copy(self):
        t = self.r.get_thresholds_for_tier("T1")
        assert t == _TIER_THRESHOLDS["T1"]
        t["oi_spike_pct"] = 999  # mutate copy
        assert _TIER_THRESHOLDS["T1"]["oi_spike_pct"] != 999  # original intact

    def test_unknown_tier_falls_back_to_T3(self):
        t = self.r.get_thresholds_for_tier("TX_UNKNOWN")
        assert t == _TIER_THRESHOLDS["T3"]

    def test_all_three_tiers_have_four_keys(self):
        for tier in ("T1", "T2", "T3"):
            t = self.r.get_thresholds_for_tier(tier)
            assert len(t) == 4


# ---------------------------------------------------------------------------
# reconcile() — async integration tests
# ---------------------------------------------------------------------------

class TestReconcile:
    def setup_method(self):
        self.r = ThresholdReconciler()

    @pytest.mark.asyncio
    async def test_empty_input_returns_zero(self):
        result = await self.r.reconcile({}, {})
        assert result.checked    == 0
        assert result.breach_count == 0
        assert result.skipped    == 0

    @pytest.mark.asyncio
    async def test_clean_symbol_no_breach(self):
        m = _metrics(oi_delta=0.001, premium_usd=100, volume_ratio=0.5)
        result = await self.r.reconcile({"AAPL": m}, {"AAPL": "T1"})
        assert result.checked      == 1
        assert result.breach_count == 0

    @pytest.mark.asyncio
    async def test_oi_spike_detected(self):
        thres = _TIER_THRESHOLDS["T1"]
        m = _metrics(oi_delta=thres["oi_spike_pct"] + 0.05)
        result = await self.r.reconcile({"AAPL": m}, {"AAPL": "T1"})
        assert result.breach_count == 1
        assert result.breaches[0].breach_type == BreachType.OI_SPIKE

    @pytest.mark.asyncio
    async def test_elapsed_ms_populated(self):
        result = await self.r.reconcile({}, {})
        assert result.elapsed_ms >= 0.0

    @pytest.mark.asyncio
    async def test_incomplete_metrics_skipped(self):
        m = _metrics()
        m.oi_delta = None  # type: ignore[assignment]
        result = await self.r.reconcile({"AAPL": m}, {"AAPL": "T1"})
        assert result.skipped    == 1
        assert result.checked    == 0
        assert result.breach_count == 0

    @pytest.mark.asyncio
    async def test_dedup_same_minute_no_duplicate(self):
        thres = _TIER_THRESHOLDS["T1"]
        ts = time.time()
        m = _metrics(oi_delta=thres["oi_spike_pct"] + 0.05, ts=ts)
        await self.r.reconcile({"AAPL": m}, {"AAPL": "T1"})
        result2 = await self.r.reconcile({"AAPL": m}, {"AAPL": "T1"})
        assert result2.breach_count == 0

    @pytest.mark.asyncio
    async def test_dedup_different_minute_allows_refire(self):
        thres = _TIER_THRESHOLDS["T1"]
        ts1 = 1_700_000_000.0
        ts2 = ts1 + 61.0
        m1 = _metrics(oi_delta=thres["oi_spike_pct"] + 0.05, ts=ts1)
        m2 = _metrics(oi_delta=thres["oi_spike_pct"] + 0.05, ts=ts2)
        r1 = await self.r.reconcile({"AAPL": m1}, {"AAPL": "T1"})
        r2 = await self.r.reconcile({"AAPL": m2}, {"AAPL": "T1"})
        assert r1.breach_count == 1
        assert r2.breach_count == 1

    @pytest.mark.asyncio
    async def test_emit_fn_called_for_each_breach(self):
        thres = _TIER_THRESHOLDS["T1"]
        m = _metrics(
            oi_delta=thres["oi_spike_pct"] + 0.05,
            premium_usd=thres["premium_usd"] + 1,
        )
        calls: List[ThresholdBreach] = []

        async def capture(b: ThresholdBreach) -> None:
            calls.append(b)

        result = await self.r.reconcile({"AAPL": m}, {"AAPL": "T1"}, emit_fn=capture)
        assert len(calls) == result.breach_count

    @pytest.mark.asyncio
    async def test_emit_fn_none_does_not_crash(self):
        thres = _TIER_THRESHOLDS["T1"]
        m = _metrics(oi_delta=thres["oi_spike_pct"] + 0.05)
        result = await self.r.reconcile({"AAPL": m}, {"AAPL": "T1"}, emit_fn=None)
        assert result.breach_count >= 1

    @pytest.mark.asyncio
    async def test_emit_fn_exception_does_not_halt_remaining_symbols(self):
        """A crashing emit_fn must not abort processing of subsequent symbols."""
        thres = _TIER_THRESHOLDS["T1"]

        async def boom(_breach: ThresholdBreach) -> None:
            raise RuntimeError("emit bus down")

        metrics = {
            "AAPL": _metrics("AAPL", oi_delta=thres["oi_spike_pct"] + 0.05),
            "TSLA": _metrics("TSLA", oi_delta=thres["oi_spike_pct"] + 0.05),
        }
        result = await self.r.reconcile(metrics, {"AAPL": "T1", "TSLA": "T1"}, emit_fn=boom)
        assert result.breach_count == 2
        assert result.checked == 2

    @pytest.mark.asyncio
    async def test_unknown_symbol_falls_back_to_T3(self):
        thres = _TIER_THRESHOLDS["T3"]
        m = _metrics(oi_delta=thres["oi_spike_pct"] + 0.05)
        result = await self.r.reconcile({"UNKNWN": m}, {})
        assert result.breach_count >= 1
        assert result.breaches[0].tier == "T3"

    @pytest.mark.asyncio
    async def test_multiple_symbols(self):
        thres_t1 = _TIER_THRESHOLDS["T1"]
        thres_t2 = _TIER_THRESHOLDS["T2"]
        tier_map = {"AAPL": "T1", "TSLA": "T2", "MSFT": "T1"}
        metrics = {
            "AAPL": _metrics("AAPL", oi_delta=thres_t1["oi_spike_pct"] + 0.05),
            "TSLA": _metrics("TSLA", volume_ratio=thres_t2["volume_ratio"] + 1),
            "MSFT": _metrics("MSFT", oi_delta=0.001),
        }
        result = await self.r.reconcile(metrics, tier_map)
        assert result.checked == 3
        assert result.breach_count >= 2

    @pytest.mark.asyncio
    async def test_reset_dedup_cache_allows_refire(self):
        thres = _TIER_THRESHOLDS["T1"]
        ts = time.time()
        m = _metrics(oi_delta=thres["oi_spike_pct"] + 0.05, ts=ts)
        await self.r.reconcile({"AAPL": m}, {"AAPL": "T1"})
        self.r.reset_dedup_cache()
        result2 = await self.r.reconcile({"AAPL": m}, {"AAPL": "T1"})
        assert result2.breach_count >= 1

    @pytest.mark.asyncio
    async def test_lock_serialises_concurrent_calls(self):
        m = _metrics(oi_delta=0.001)
        r1, r2 = await asyncio.gather(
            self.r.reconcile({"AAPL": m}, {"AAPL": "T1"}),
            self.r.reconcile({"TSLA": m}, {"TSLA": "T1"}),
        )
        assert r1.checked + r2.checked == 2


# ---------------------------------------------------------------------------
# _maybe_evict under cap pressure
# ---------------------------------------------------------------------------

class TestMaybeEvict:
    @pytest.mark.asyncio
    async def test_evicts_when_over_cap(self):
        r = ThresholdReconciler()
        r._seen_cap = 10

        for i in range(12):
            ts = float(i * 120)
            m = _metrics(symbol="SYM", oi_delta=_TIER_THRESHOLDS["T1"]["oi_spike_pct"] + 0.05, ts=ts)
            await r.reconcile({"SYM": m}, {"SYM": "T1"})

        assert len(r._seen) <= r._seen_cap


# ---------------------------------------------------------------------------
# Module-level singleton + reconcile() wrapper
# ---------------------------------------------------------------------------

class TestModuleLevel:
    def test_get_reconciler_returns_singleton(self):
        import services.threshold_reconciliation as mod
        mod._reconciler = None
        r1 = get_reconciler()
        r2 = get_reconciler()
        assert r1 is r2

    def test_get_reconciler_creates_instance(self):
        import services.threshold_reconciliation as mod
        mod._reconciler = None
        r = get_reconciler()
        assert isinstance(r, ThresholdReconciler)

    @pytest.mark.asyncio
    async def test_module_reconcile_wrapper(self):
        import services.threshold_reconciliation as mod
        mod._reconciler = None
        result = await reconcile({}, {})
        assert isinstance(result, ReconcileResult)
        assert result.checked == 0
