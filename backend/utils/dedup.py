"""
utils/dedup.py — Layer 4 TTL deduplication cache for Tradier stream events.

Problem: A single options trade prints on multiple exchanges within a reporting
window. Without dedup, the same trade creates multiple DB rows and inflates
premium tallies in the RepetitionAccumulator.

Key design:
  dedup key  = (occ_symbol, size, round(fill, 2))
  canonical  = first event seen for this key
  duplicates = any subsequent event for same key within ttl_seconds

Public API:
  make_key(event_or_occ_symbol, size=None, fill=None) -> str
    - make_key(event)            — single-arg form for test compatibility
    - make_key(occ, size, fill)  — original multi-arg form
  DedupCache — TTL cache class; internal store accessible via ._cache (alias for ._seen)

NOTE on time source:
  _seen stores wall-clock timestamps (time.time()) so that test code which
  backdates entries via `cache._cache[key] = time.time() - N` correctly
  triggers expiry. monotonic() was previously used but is incompatible with
  wall-clock backdating in tests.

ING-010 (dedup tier-awareness — 2026-05-07):
  _is_dup_by_raw_key() now accepts an optional tier_int (1/2/3). When provided,
  the effective TTL is read live from gate_config_store.get("dedup_window_ms",
  tier_int) and converted to seconds. Falls back to self._ttl (construction-time
  default 5.0s) when tier_int is None, store not loaded, or key absent.

  _cleanup() uses _effective_cleanup_ttl() = max(self._ttl, max dedup_window_ms
  across all tiers from gate_config_store / 1000). This ensures keys are not
  evicted before the widest tier window expires.

Fix (ING-010-IMPORT 2026-05-07): import store as gate_config_store.
  Both _resolve_tier_ttl() and _effective_cleanup_ttl() previously imported
  `gate_config_store` by name from the module — that symbol does not exist.
  The module exports `store`. Both methods caught the ImportError silently via
  bare `except Exception: pass` and fell back to self._ttl (5.0s flat), meaning
  the tier-aware path has never executed. Fixed to:
      from services.gate_config_store import store as gate_config_store
"""
import time
from collections import defaultdict
from typing import Any, Optional


def make_key(event_or_occ_symbol: Any, size: Optional[int] = None, fill: Optional[float] = None) -> str:
    """
    Canonical dedup key — two call forms:

    1. make_key(occ_symbol: str, size: int, fill: float)
       Original positional form used by tradier_stream.

    2. make_key(event)
       Single-arg form used by tests — derives key from event attributes:
       ticker, expiry, contract_type, strike.
    """
    if size is None and fill is None:
        # Single-arg event form
        ev = event_or_occ_symbol
        ticker   = str(getattr(ev, 'ticker',        getattr(ev, 'occ_symbol', '')))
        strike   = getattr(ev, 'strike', 0)
        expiry   = str(getattr(ev, 'expiry',        ''))
        ctype    = str(getattr(ev, 'contract_type', ''))
        _strike  = float(strike) if strike is not None else 0.0
        return f"{ticker}|{expiry}|{ctype}|{_strike:.2f}"
    # Multi-arg form
    occ_symbol = str(event_or_occ_symbol)
    _size = int(size)
    _fill = float(fill)
    return f"{occ_symbol}|{_size}|{_fill:.2f}"


class DedupCache:
    """
    TTL dedup cache with sweep detection.

    Uses wall-clock time (time.time()) for all timestamps so that
    test backdating via `cache._cache[key] = time.time() - N` works correctly.

    Parameters
    ----------
    ttl_seconds : float
        Cold-start / fallback TTL (seconds). Used when gate_config_store has
        not yet loaded or no tier_int is supplied to is_duplicate().
        Default: 5.0s (covers worst-case PHLX/MIAX lag).
    sweep_window : float
        Window in which 3+ exchange reports on the same contract are
        classified as a sweep.
    sweep_min_exchanges : int
        Minimum unique exchange count to declare a sweep.

    ING-010 tier-aware TTL:
        Pass tier_int (1/2/3) to is_duplicate() to resolve the effective TTL
        live from gate_config_store.get("dedup_window_ms", tier_int).
        When tier_int is omitted or the store has not loaded, self._ttl stands.
    """

    def __init__(
        self,
        ttl_seconds:         float = 5.0,
        sweep_window:        float = 8.0,
        sweep_min_exchanges: int   = 3,
    ):
        self._ttl        = ttl_seconds
        self._sweep_win  = sweep_window
        self._sweep_min  = sweep_min_exchanges

        self._seen: dict[str, float] = {}
        self._exchange_hits: dict[str, list[tuple[float, str]]] = defaultdict(list)

        self._total_seen:       int = 0
        self._total_duplicates: int = 0
        self._total_sweeps:     int = 0

        self._last_cleanup = time.time()

    # ------------------------------------------------------------------
    # Backward-compat: tests access cache._cache; internal store is _seen
    # ------------------------------------------------------------------

    @property
    def _cache(self) -> dict:
        """Alias for _seen — exposed for test introspection."""
        return self._seen

    @_cache.setter
    def _cache(self, value: dict) -> None:
        self._seen = value

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def ttl_seconds(self) -> float:
        """The configured (cold-start) TTL in seconds."""
        return self._ttl

    # ------------------------------------------------------------------
    # ING-010: live tier-aware TTL resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_tier_ttl(tier_int: Optional[int], fallback: float) -> float:
        """
        Read dedup_window_ms for tier_int from gate_config_store and convert
        to seconds. Returns `fallback` on any error or when store not loaded.

        O(1) in-memory read — never raises. Safe on the hot path.
        """
        if tier_int is None:
            return fallback
        try:
            from services.gate_config_store import store as gate_config_store
            raw_ms = gate_config_store.get("dedup_window_ms", tier_int)
            if raw_ms is not None and raw_ms > 0:
                return float(raw_ms) / 1000.0
        except Exception:
            pass
        return fallback

    def _effective_cleanup_ttl(self) -> float:
        """
        Cleanup must use the MAXIMUM TTL across all tiers so that keys within
        a wider tier window are not prematurely evicted.

        e.g. if T1=5s, T2=7s, T3=10s: cleanup evicts at 10s, not 5s.

        Falls back to self._ttl when store is not loaded (cold start).
        """
        try:
            from services.gate_config_store import store as gate_config_store
            max_ms = max(
                (gate_config_store.get("dedup_window_ms", t) or 0)
                for t in (1, 2, 3)
            )
            if max_ms > 0:
                return max(self._ttl, float(max_ms) / 1000.0)
        except Exception:
            pass
        return self._ttl

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cleanup(self):
        now = time.time()
        if now - self._last_cleanup < 10.0:
            return
        # Use the widest possible TTL to avoid premature eviction.
        cleanup_ttl  = self._effective_cleanup_ttl()
        ttl_cutoff   = now - cleanup_ttl
        sweep_cutoff = now - self._sweep_win
        self._seen = {k: v for k, v in self._seen.items() if v > ttl_cutoff}
        self._exchange_hits = defaultdict(list, {
            k: [(t, e) for t, e in v if t > sweep_cutoff]
            for k, v in self._exchange_hits.items()
            if any(t > sweep_cutoff for t, _ in v)
        })
        self._last_cleanup = now

    @staticmethod
    def _dedup_key(occ_symbol: str, size: int, fill: float) -> str:
        return make_key(occ_symbol, size, fill)

    @staticmethod
    def _contract_key(occ_symbol: str, size: int, fill: float) -> str:
        return make_key(occ_symbol, size, fill)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_duplicate(
        self,
        event_or_occ_symbol: Any,
        size:       Optional[int]   = None,
        fill:       Optional[float] = None,
        exchange:   Optional[str]   = None,
        ts:         Optional[float] = None,
        tier_int:   Optional[int]   = None,
    ) -> bool:
        """
        Two call signatures:

        1. is_duplicate(occ_symbol, size, fill, exchange, ts, tier_int)
           Multi-arg form used by tradier_stream. Pass tier_int (1/2/3) to
           enable live tier-aware TTL resolution from gate_config_store.

        2. is_duplicate(event)
           Single-arg form used by tests — builds key from event attributes.
           tier_int defaults to None; flat self._ttl is used.

        ING-010: when tier_int is supplied, the effective dedup window is
        resolved via gate_config_store.get("dedup_window_ms", tier_int).
        Falls back to self._ttl when store not loaded or returns None.
        """
        if size is None:
            # Single-object form
            ev = event_or_occ_symbol
            ticker   = str(getattr(ev, 'ticker',        getattr(ev, 'occ_symbol', '')))
            strike   = getattr(ev, 'strike', 0)
            expiry   = str(getattr(ev, 'expiry',        ''))
            ctype    = str(getattr(ev, 'contract_type', ''))
            _strike  = float(strike) if strike is not None else 0.0
            _size    = int(getattr(ev, 'size',     0))
            _fill    = float(getattr(ev, 'fill',   _strike))
            _exch    = str(getattr(ev, 'exchange', ''))
            raw_key  = f"{ticker}|{expiry}|{ctype}|{_strike:.2f}|{_size}|{_fill:.2f}"
            return self._is_dup_by_raw_key(raw_key, _exch, ts, tier_int)
        else:
            occ_symbol = str(event_or_occ_symbol)
            _size      = int(size)
            _fill      = float(fill)
            _exch      = str(exchange) if exchange is not None else ''
            key = make_key(occ_symbol, _size, _fill)
            return self._is_dup_by_raw_key(key, _exch, ts, tier_int)

    def _is_dup_by_raw_key(
        self,
        key:      str,
        exchange: str,
        ts:       Optional[float],
        tier_int: Optional[int] = None,
    ) -> bool:
        """
        ING-010: effective_ttl resolved per-call from gate_config_store when
        tier_int is provided. Falls back to self._ttl on cold start or missing key.
        """
        now = ts if ts is not None else time.time()
        self._cleanup()

        # Resolve effective TTL: live from store if tier_int is known.
        effective_ttl = self._resolve_tier_ttl(tier_int, self._ttl)

        first_seen = self._seen.get(key)
        if first_seen is not None and (now - first_seen) < effective_ttl:
            self._total_duplicates += 1
            self._exchange_hits[key].append((now, exchange))
            return True

        self._seen[key] = now
        self._total_seen += 1
        self._exchange_hits[key].append((now, exchange))
        return False

    def mark_seen(self, key: str) -> None:
        self._seen[key] = time.time()

    def size(self) -> int:
        return len(self._seen)

    def clear(self) -> None:
        self._seen.clear()
        self._exchange_hits.clear()

    def evict_expired(self) -> int:
        now = time.time()
        cutoff = now - self._ttl
        expired = [k for k, ts in self._seen.items() if ts <= cutoff]
        for k in expired:
            del self._seen[k]
        return len(expired)

    def get_exchange_count(self, occ_symbol: str, size: int, fill: float) -> int:
        ckey   = self._contract_key(occ_symbol, size, fill)
        now    = time.time()
        cutoff = now - self._sweep_win
        recent = [e for t, e in self._exchange_hits.get(ckey, []) if t > cutoff]
        return len(set(e for e in recent if e))

    def is_sweep(
        self,
        occ_symbol: str,
        size:       int,
        fill:       float,
    ) -> bool:
        count  = self.get_exchange_count(occ_symbol, size, fill)
        result = count >= self._sweep_min
        if result:
            self._total_sweeps += 1
        return result

    def dedup_stats(self) -> dict:
        return {
            "dedup_seen":       self._total_seen,
            "dedup_duplicates": self._total_duplicates,
            "dedup_sweeps":     self._total_sweeps,
            "dedup_cache_size": len(self._seen),
        }


# Module-level singleton
flow_dedup = DedupCache(ttl_seconds=5.0, sweep_window=8.0, sweep_min_exchanges=3)
