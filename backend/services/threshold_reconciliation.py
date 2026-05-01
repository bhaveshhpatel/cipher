"""
apex/s1 — Threshold Reconciliation Service
===========================================
Reconciles live OI / premium / volume readings against per-symbol
dynamic thresholds computed by the tier engine.  On each reconcile pass:

  1. Pull the current tier map (T1/T2/T3) from tier_engine.
  2. For every active symbol compare its metrics against tier thresholds.
  3. Emit a ThresholdBreach event on the async bus for any symbol that
     has crossed its breach level since the last reconcile cycle.
  4. Return a ReconcileResult summary that callers (e.g. stream_worker)
     can log or route downstream.

Design constraints
------------------
- Zero external I/O: all data comes from in-process stores / bus.
- Fully async; callers must await reconcile().
- Thread-safe: a single asyncio.Lock serialises concurrent reconcile calls
  so back-pressure from a slow tier_engine never races with a fast stream.
- Idempotent: calling reconcile twice with identical state emits zero
  duplicate breach events (dedup via last-seen cache keyed on
  (symbol, breach_type, epoch-minute)).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

class BreachType(str, Enum):
    OI_SPIKE       = "oi_spike"
    PREMIUM_FLOOD  = "premium_flood"
    VOLUME_SURGE   = "volume_surge"
    OI_COLLAPSE    = "oi_collapse"


@dataclass
class SymbolMetrics:
    symbol:         str
    oi_delta:       float   # change in open interest since last snapshot
    premium_usd:    float   # dollar premium on the flow event
    volume_ratio:   float   # current volume / 20-day avg volume
    timestamp:      float = field(default_factory=time.time)


@dataclass
class ThresholdBreach:
    symbol:       str
    breach_type:  BreachType
    observed:     float
    threshold:    float
    tier:         str          # "T1", "T2", "T3"
    timestamp:    float = field(default_factory=time.time)


@dataclass
class ReconcileResult:
    checked:   int = 0
    breaches:  List[ThresholdBreach] = field(default_factory=list)
    skipped:   int = 0          # symbols with incomplete metrics
    elapsed_ms: float = 0.0

    @property
    def breach_count(self) -> int:
        return len(self.breaches)


# ---------------------------------------------------------------------------
# Per-tier threshold config  (tunable via Settings in a future pass)
# ---------------------------------------------------------------------------

_TIER_THRESHOLDS: Dict[str, Dict[str, float]] = {
    "T1": {
        "oi_spike_pct":      0.10,   # 10 % OI increase = breach
        "oi_collapse_pct":  -0.15,   # 15 % OI decrease
        "premium_usd":    250_000,   # $250 k single-event premium
        "volume_ratio":       3.0,   # 3× avg volume
    },
    "T2": {
        "oi_spike_pct":      0.20,
        "oi_collapse_pct":  -0.25,
        "premium_usd":    100_000,
        "volume_ratio":       4.0,
    },
    "T3": {
        "oi_spike_pct":      0.35,
        "oi_collapse_pct":  -0.40,
        "premium_usd":     50_000,
        "volume_ratio":       6.0,
    },
}

_DEFAULT_TIER = "T3"  # fall-back for unknown symbols


# ---------------------------------------------------------------------------
# Dedup key helpers
# ---------------------------------------------------------------------------

def _epoch_minute(ts: float) -> int:
    """Quantise timestamp to the nearest minute for dedup bucketing."""
    return int(ts // 60)


def _breach_key(symbol: str, breach_type: BreachType, ts: float) -> Tuple[str, str, int]:
    return (symbol, breach_type.value, _epoch_minute(ts))


# ---------------------------------------------------------------------------
# Core reconciler
# ---------------------------------------------------------------------------

class ThresholdReconciler:
    """
    Stateful reconciler.  Instantiate once per process; call reconcile()
    on every ingestion tick or on a scheduled cadence.
    """

    def __init__(self) -> None:
        self._lock: asyncio.Lock = asyncio.Lock()
        # (symbol, breach_type_value, epoch_minute) -> True
        self._seen: Dict[Tuple[str, str, int], bool] = {}
        self._seen_cap = 10_000   # evict oldest when over cap

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def reconcile(
        self,
        metrics_by_symbol: Dict[str, SymbolMetrics],
        tier_map: Dict[str, str],          # symbol -> "T1"|"T2"|"T3"
        emit_fn: Optional[object] = None,  # async callable(ThresholdBreach)
    ) -> ReconcileResult:
        """
        Run one reconcile pass.

        Parameters
        ----------
        metrics_by_symbol:
            Mapping of symbol -> SymbolMetrics for the current tick.
        tier_map:
            Mapping of symbol -> tier string, typically from tier_engine.
        emit_fn:
            Optional async coroutine that receives each ThresholdBreach.
            If None, breaches are collected but not forwarded.

        Returns
        -------
        ReconcileResult with breach list and timing.
        """
        async with self._lock:
            return await self._run(metrics_by_symbol, tier_map, emit_fn)

    def reset_dedup_cache(self) -> None:
        """Clear the dedup cache; useful between sessions or in tests."""
        self._seen.clear()

    def get_thresholds_for_tier(self, tier: str) -> Dict[str, float]:
        """Return the threshold config for a given tier (read-only copy)."""
        return dict(_TIER_THRESHOLDS.get(tier, _TIER_THRESHOLDS[_DEFAULT_TIER]))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _run(
        self,
        metrics_by_symbol: Dict[str, SymbolMetrics],
        tier_map: Dict[str, str],
        emit_fn: Optional[object],
    ) -> ReconcileResult:
        t0 = time.monotonic()
        result = ReconcileResult()

        for symbol, m in metrics_by_symbol.items():
            if not self._metrics_complete(m):
                result.skipped += 1
                continue

            tier  = tier_map.get(symbol, _DEFAULT_TIER)
            thres = _TIER_THRESHOLDS.get(tier, _TIER_THRESHOLDS[_DEFAULT_TIER])
            result.checked += 1

            new_breaches = self._evaluate(m, tier, thres)
            for breach in new_breaches:
                key = _breach_key(symbol, breach.breach_type, breach.timestamp)
                if key in self._seen:
                    continue  # dedup
                self._seen[key] = True
                self._maybe_evict()
                result.breaches.append(breach)
                if emit_fn is not None:
                    try:
                        await emit_fn(breach)
                    except Exception as exc:  # pragma: no cover
                        logger.warning("emit_fn raised: %s", exc)

        result.elapsed_ms = (time.monotonic() - t0) * 1000
        return result

    @staticmethod
    def _metrics_complete(m: SymbolMetrics) -> bool:
        """Return False if any metric is NaN / None — skip incomplete rows."""
        try:
            return all(v is not None for v in (
                m.oi_delta, m.premium_usd, m.volume_ratio
            ))
        except Exception:
            return False

    @staticmethod
    def _evaluate(
        m: SymbolMetrics,
        tier: str,
        thres: Dict[str, float],
    ) -> List[ThresholdBreach]:
        breaches: List[ThresholdBreach] = []

        # OI spike
        if m.oi_delta >= thres["oi_spike_pct"]:
            breaches.append(ThresholdBreach(
                symbol=m.symbol,
                breach_type=BreachType.OI_SPIKE,
                observed=m.oi_delta,
                threshold=thres["oi_spike_pct"],
                tier=tier,
                timestamp=m.timestamp,
            ))

        # OI collapse
        if m.oi_delta <= thres["oi_collapse_pct"]:
            breaches.append(ThresholdBreach(
                symbol=m.symbol,
                breach_type=BreachType.OI_COLLAPSE,
                observed=m.oi_delta,
                threshold=thres["oi_collapse_pct"],
                tier=tier,
                timestamp=m.timestamp,
            ))

        # Premium flood
        if m.premium_usd >= thres["premium_usd"]:
            breaches.append(ThresholdBreach(
                symbol=m.symbol,
                breach_type=BreachType.PREMIUM_FLOOD,
                observed=m.premium_usd,
                threshold=thres["premium_usd"],
                tier=tier,
                timestamp=m.timestamp,
            ))

        # Volume surge
        if m.volume_ratio >= thres["volume_ratio"]:
            breaches.append(ThresholdBreach(
                symbol=m.symbol,
                breach_type=BreachType.VOLUME_SURGE,
                observed=m.volume_ratio,
                threshold=thres["volume_ratio"],
                tier=tier,
                timestamp=m.timestamp,
            ))

        return breaches

    def _maybe_evict(self) -> None:
        """Simple LRU-style eviction: drop oldest half when over cap."""
        if len(self._seen) > self._seen_cap:
            evict_count = self._seen_cap // 2
            keys = list(self._seen.keys())
            for k in keys[:evict_count]:
                del self._seen[k]


# ---------------------------------------------------------------------------
# Module-level singleton (mirrors pattern used by swarm_engine, signal_store)
# ---------------------------------------------------------------------------

_reconciler: Optional[ThresholdReconciler] = None


def get_reconciler() -> ThresholdReconciler:
    """Return the process-wide ThresholdReconciler singleton."""
    global _reconciler
    if _reconciler is None:
        _reconciler = ThresholdReconciler()
    return _reconciler


async def reconcile(
    metrics_by_symbol: Dict[str, SymbolMetrics],
    tier_map: Dict[str, str],
    emit_fn: Optional[object] = None,
) -> ReconcileResult:
    """Module-level convenience wrapper around the singleton reconciler."""
    return await get_reconciler().reconcile(metrics_by_symbol, tier_map, emit_fn)
