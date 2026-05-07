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

ING-010 (2026-05-07):
  _TIER_THRESHOLDS retained as compile-time FALLBACK only.
  _get_tier_thresholds(tier) reads live values from gate_config_store
  for premium_usd, volume_ratio, oi_spike_pct, oi_collapse_pct.
  Falls back per-key to _TIER_THRESHOLDS on cold-start or missing key.
  ThresholdReconciler._run() and get_thresholds_for_tier() now use
  _get_tier_thresholds() so hot-reloads propagate to every reconcile pass.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

# ING-010: tier-aware gate config store singleton.
from services.gate_config_store import gate_config_store

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
# Per-tier threshold FALLBACK config
# Used when gate_config_store has not yet loaded or a specific key is absent.
# ING-010: this dict is a cold-start safety net, NOT the live source of truth.
# Live values are read via _get_tier_thresholds() from gate_config_store.
# ---------------------------------------------------------------------------

_TIER_THRESHOLDS: Dict[str, Dict[str, float]] = {
    "T1": {
        "oi_spike_pct":      0.10,   # 10 % OI increase = breach
        "oi_collapse_pct":  -0.15,   # 15 % OI decrease
        "premium_usd":    250_000,   # $250 k single-event premium
        "volume_ratio":       3.0,   # 3\u00d7 avg volume
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

# Mapping from tier string ("T1"/"T2"/"T3") to int (1/2/3) used by
# gate_config_store.get(key, tier_int).
_TIER_STR_TO_INT: Dict[str, int] = {"T1": 1, "T2": 2, "T3": 3}


# ---------------------------------------------------------------------------
# ING-010: Live threshold resolver
# ---------------------------------------------------------------------------

def _get_tier_thresholds(tier: str) -> Dict[str, float]:
    """
    Return the effective threshold config for a given tier string.

    Resolution order for each key:
      1. gate_config_store.get(key, tier_int)  — live, hot-reloadable.
      2. _TIER_THRESHOLDS[tier][key]           — compile-time fallback.

    Keys resolved from gate_config_store:
      "premium_usd"     <- gate_config_store.get("premium_usd", tier_int)
      "volume_ratio"    <- gate_config_store.get("volume_ratio", tier_int)
      "oi_spike_pct"    <- gate_config_store.get("oi_spike_pct", tier_int)
      "oi_collapse_pct" <- gate_config_store.get("oi_collapse_pct", tier_int)

    Falls back to _TIER_THRESHOLDS per-key (not per-tier wholesale) so that
    a partial gate_config_store load (e.g. only premium_usd configured)
    still benefits from live values for the keys that are present.

    Never raises. Safe on the hot reconcile path.
    """
    fallback = _TIER_THRESHOLDS.get(tier, _TIER_THRESHOLDS[_DEFAULT_TIER])
    tier_int = _TIER_STR_TO_INT.get(tier, 3)

    def _live(key: str, fb: float) -> float:
        try:
            val = gate_config_store.get(key, tier_int)
            if val is not None:
                return float(val)
        except Exception:
            pass
        return fb

    return {
        "premium_usd":     _live("premium_usd",     fallback["premium_usd"]),
        "volume_ratio":    _live("volume_ratio",    fallback["volume_ratio"]),
        "oi_spike_pct":    _live("oi_spike_pct",    fallback["oi_spike_pct"]),
        "oi_collapse_pct": _live("oi_collapse_pct", fallback["oi_collapse_pct"]),
    }


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
        """Return the effective threshold config for a given tier.

        ING-010: now reads live values from gate_config_store via
        _get_tier_thresholds() so callers (tests, admin endpoints) see
        the same values the reconciler uses at runtime.
        """
        return _get_tier_thresholds(tier)

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
            # ING-010: read live thresholds from gate_config_store;
            # falls back per-key to _TIER_THRESHOLDS on cold-start.
            thres = _get_tier_thresholds(tier)
            result.checked += 1

            new_breaches = self._evaluate(m, tier, thres)
            for breach in new_breaches:
                key = _breach_key(symbol, breach.breach_type, breach.timestamp)
                if key in self._seen:
                    continue  # dedup
                self._seen[key] = True
                result.breaches.append(breach)
                if emit_fn is not None:
                    try:
                        await emit_fn(breach)
                    except Exception as exc:
                        logger.warning("emit_fn raised: %s", exc)

            # Evict once per symbol after all its breaches are processed
            self._maybe_evict()

        result.elapsed_ms = (time.monotonic() - t0) * 1000
        return result

    @staticmethod
    def _metrics_complete(m: SymbolMetrics) -> bool:
        """Return False if any metric is NaN or None — skip incomplete rows."""
        try:
            for v in (m.oi_delta, m.premium_usd, m.volume_ratio):
                if v is None or math.isnan(v):
                    return False
            return True
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
