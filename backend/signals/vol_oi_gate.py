# ============================================================================
# vol_oi_gate.py
#
# DEPLOY NOTE (ING-012 QQ1 — 2026-05-09):
#   Gate 4 (Vol/OI Confirmation Gate) — evaluates whether a qualifying
#   RepetitionEpisode has sufficient open interest to confirm that the
#   detected flow has a liquid, institutionally-held underlying contract.
#
#   OI SOURCE (SA deliberation 2026-05-09):
#     open_interest comes exclusively from options_chain_cache — specifically
#     ContractMeta.open_interest populated during symbol_registry.build() via
#     Tradier /markets/options/chains. It is NOT read from the live timesale
#     stream (which does not carry OI at all). At gate-evaluation time, OI is
#     already in-process memory — no HTTP call is made here.
#
#   TIER SOURCE (SA deliberation 2026-05-09):
#     Tier is the POST-BUILD reclassified tier from symbol_registry._tier_map,
#     NOT the bootstrap tier used during _build_ticker(). The registry exposes
#     this via get_meta(occ_symbol).tier or influence_tier_int(ticker).
#
#   GATE LOGIC:
#     1. Disabled by default: gate_config_store key 'vol_oi_min_ratio' returns
#        0.0 for all tiers at cold start → gate is a no-op.
#     2. When enabled, gate checks:
#          volume / open_interest >= vol_oi_min_ratio
#        where volume = ep.trade_count (episode event count, not exchange fill_count)
#        and   open_interest = registry.get_oi(ep.occ_symbol)
#     3. Fail-open: None OI, 0 OI, missing registry, or any exception → PASS.
#        This matches the require_oi gate (ING-010-OI) fail-open semantics.
#     4. vol_oi_ratio + raw oi + raw volume are returned in the result dict
#        for downstream bus payload enrichment and observability.
#
#   CACHE (PBE deliberation 2026-05-09):
#     OI is cached per OCC symbol with TTL = _OI_CACHE_TTL_S (default 60s).
#     On a cache hit, no registry call is made. Cache is invalidated on TTL
#     expiry only — no explicit invalidation API (OI does not change intra-day
#     within a meaningful window; chain_store refresh cadence is ~5 min).
#
#   QA NOTES (2026-05-09):
#     - Default ratio 0.0 → all episodes pass (zero behaviour change at deploy).
#     - Gate must be testable with a mock registry (no live Tradier dependency).
#     - Fail-open must be verified: None OI → PASS, exception → PASS, 0 OI → PASS.
#     - vol_oi_ratio must be 0.0 when gate is disabled (not None).
#     - Test file: tests/test_vol_oi_gate.py
# ============================================================================
"""
Vol/OI Confirmation Gate (Gate 4 / ING-012 QQ1).

Evaluates whether a RepetitionEpisode has sufficient open interest relative
to episode volume to confirm institutional presence in the contract.

OI is sourced from the in-memory symbol registry (options_chain_cache path),
not from the live stream. No HTTP calls are made at gate-evaluation time.

Usage::

    from signals.vol_oi_gate import vol_oi_gate

    result = vol_oi_gate.check(ep, registry)
    if not result["passed"]:
        return  # drop episode
    payload["vol_oi_ratio"] = result["vol_oi_ratio"]
    payload["oi"]           = result["oi"]
"""

import logging
import time
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ING-010-IMPORT pattern: import store, not gate_config_store.
try:
    from services.gate_config_store import store as _gate_cfg
except Exception:  # pragma: no cover
    _gate_cfg = None  # type: ignore[assignment]

# Cache TTL seconds.  OI refreshes at chain-build cadence (~5 min on Railway).
# 60s ensures at most one registry lookup per OCC symbol per minute.
_OI_CACHE_TTL_S: float = 60.0

# Config key used to read the min Vol/OI ratio per tier from gate_config_store.
_CONFIG_KEY: str = "vol_oi_min_ratio"


class _OiCache:
    """Thread-safe, TTL-expiring per-OCC-symbol OI cache."""

    def __init__(self, ttl_s: float = _OI_CACHE_TTL_S) -> None:
        self._ttl = ttl_s
        self._lock = threading.Lock()
        # {occ_symbol: (oi_value: Optional[float], expires_at: float)}
        self._store: Dict[str, tuple] = {}

    def get(self, occ_symbol: str) -> tuple:
        """Return (hit: bool, oi: Optional[float])."""
        with self._lock:
            entry = self._store.get(occ_symbol)
            if entry is None:
                return False, None
            oi_val, expires_at = entry
            if time.monotonic() >= expires_at:
                del self._store[occ_symbol]
                return False, None
            return True, oi_val

    def put(self, occ_symbol: str, oi_val: Optional[float]) -> None:
        with self._lock:
            self._store[occ_symbol] = (oi_val, time.monotonic() + self._ttl)

    def invalidate(self, occ_symbol: str) -> None:
        with self._lock:
            self._store.pop(occ_symbol, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


def _resolve_oi(occ_symbol: str, registry: Any, cache: "_OiCache") -> Optional[float]:
    """
    Resolve OI for an OCC symbol.

    Lookup order:
      1. In-process cache (TTL hit).
      2. registry.get_oi(occ_symbol)  — reads ContractMeta.open_interest.
      3. registry.get_meta(occ_symbol).open_interest  — fallback attribute path.

    Returns None on any failure (gate will fail-open).
    """
    hit, cached_oi = cache.get(occ_symbol)
    if hit:
        return cached_oi

    oi: Optional[float] = None
    try:
        if hasattr(registry, "get_oi"):
            raw = registry.get_oi(occ_symbol)
            oi = float(raw) if raw is not None else None
        elif hasattr(registry, "get_meta"):
            meta = registry.get_meta(occ_symbol)
            if meta is not None:
                raw = getattr(meta, "open_interest", None)
                oi = float(raw) if raw is not None else None
    except Exception as exc:  # pragma: no cover
        logger.warning("vol_oi_gate: OI lookup failed for %s: %s", occ_symbol, exc)
        oi = None

    cache.put(occ_symbol, oi)
    return oi


def _resolve_min_ratio(tier_int: int) -> float:
    """
    Read vol_oi_min_ratio from gate_config_store for the given tier.
    Returns 0.0 (gate disabled) when store is unavailable or key not set.
    """
    if _gate_cfg is None:
        return 0.0
    try:
        val = _gate_cfg.get(_CONFIG_KEY, tier_int)
        return float(val) if val is not None else 0.0
    except Exception:  # pragma: no cover
        return 0.0


class VolOiGate:
    """
    Vol/OI Confirmation Gate.

    Instantiate once (module-level singleton ``vol_oi_gate``) and call
    ``check(ep, registry)`` from the emit path.

    Parameters
    ----------
    ttl_s:
        OI cache TTL in seconds.  Defaults to 60.
    """

    def __init__(self, ttl_s: float = _OI_CACHE_TTL_S) -> None:
        self._cache = _OiCache(ttl_s=ttl_s)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(
        self,
        ep: Any,
        registry: Any,
        *,
        tier_int: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Evaluate the Vol/OI gate for a RepetitionEpisode.

        Parameters
        ----------
        ep:
            RepetitionEpisode instance.  Must have:
              - ep.occ_symbol  (str) — standard OCC format
              - ep.trade_count (int) — episode event count (not fill_count)
              - ep.ticker      (str) — underlying ticker
        registry:
            symbol_registry instance with get_oi() or get_meta().
        tier_int:
            Override tier (1/2/3).  When None, resolved from registry via
            influence_tier_int(ep.ticker).  Defaults to tier 1 (strict) when
            registry does not expose the method.

        Returns
        -------
        dict with keys:
          passed       bool   — True = episode passes gate (or gate disabled)
          vol_oi_ratio float  — computed ratio; 0.0 when gate disabled or OI=0
          oi           float  — resolved open_interest; 0.0 when unknown
          volume       int    — ep.trade_count used in ratio
          min_ratio    float  — threshold applied (0.0 = gate disabled)
          gate_active  bool   — False when min_ratio == 0.0
        """
        occ_symbol  = getattr(ep, "occ_symbol", "") or ""
        volume      = int(getattr(ep, "trade_count", 0))
        ticker      = getattr(ep, "ticker", "") or ""

        # Resolve tier
        if tier_int is None:
            tier_int = self._resolve_tier(ticker, registry)

        # Read gate threshold
        min_ratio = _resolve_min_ratio(tier_int)

        # Gate disabled (default)
        if min_ratio == 0.0:
            return {
                "passed":       True,
                "vol_oi_ratio": 0.0,
                "oi":           0.0,
                "volume":       volume,
                "min_ratio":    0.0,
                "gate_active":  False,
            }

        # Resolve OI
        oi_raw = _resolve_oi(occ_symbol, registry, self._cache)
        oi     = float(oi_raw) if oi_raw is not None else 0.0

        # Fail-open: OI unknown or zero
        if oi <= 0.0:
            logger.debug(
                "vol_oi_gate: PASS (fail-open) %s OI=%.0f volume=%d tier=%d",
                occ_symbol, oi, volume, tier_int,
            )
            return {
                "passed":       True,
                "vol_oi_ratio": 0.0,
                "oi":           0.0,
                "volume":       volume,
                "min_ratio":    min_ratio,
                "gate_active":  True,
            }

        ratio  = volume / oi
        passed = ratio >= min_ratio

        if not passed:
            logger.debug(
                "vol_oi_gate: DROP %s ratio=%.4f < min=%.4f OI=%.0f volume=%d tier=%d",
                occ_symbol, ratio, min_ratio, oi, volume, tier_int,
            )

        return {
            "passed":       passed,
            "vol_oi_ratio": ratio,
            "oi":           oi,
            "volume":       volume,
            "min_ratio":    min_ratio,
            "gate_active":  True,
        }

    # ------------------------------------------------------------------
    # Cache management (test helpers + future admin API)
    # ------------------------------------------------------------------

    def invalidate(self, occ_symbol: str) -> None:
        """Force cache eviction for a single OCC symbol."""
        self._cache.invalidate(occ_symbol)

    def clear_cache(self) -> None:
        """Clear entire OI cache (e.g. after a full chain refresh)."""
        self._cache.clear()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_tier(ticker: str, registry: Any) -> int:
        """Read post-build reclassified tier from registry.

        Tries influence_tier_int() first (symbol_registry public API).
        Falls back to tier 1 (strict default — matches ING-010-OI semantics).
        """
        if registry is None:
            return 1
        try:
            if hasattr(registry, "influence_tier_int"):
                t = registry.influence_tier_int(ticker)
                return int(t) if t is not None else 1
            if hasattr(registry, "_tier_map"):
                return int(registry._tier_map.get(ticker, 1))
        except Exception:  # pragma: no cover
            pass
        return 1


# ---------------------------------------------------------------------------
# Module-level singleton — import and call vol_oi_gate.check(ep, registry).
# ---------------------------------------------------------------------------
vol_oi_gate = VolOiGate()
