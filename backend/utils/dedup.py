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
        How long to consider a (occ_symbol, size, fill) combination as
        the same trade. Set to 5s to cover worst-case PHLX/MIAX lag.
    sweep_window : float
        Window in which 3+ exchange reports on the same contract are
        classified as a sweep.
    sweep_min_exchanges : int
        Minimum unique exchange count to declare a sweep.
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
        """The configured TTL in seconds."""
        return self._ttl

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cleanup(self):
        now = time.time()
        if now - self._last_cleanup < 10.0:
            return
        ttl_cutoff   = now - self._ttl
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
    ) -> bool:
        """
        Two call signatures:

        1. is_duplicate(occ_symbol: str, size: int, fill: float, exchange: str)
           -- original multi-arg form used by tradier_stream

        2. is_duplicate(event)  -- tests pass a plain object/SimpleNamespace with
           .ticker/.strike/.expiry/.contract_type attributes; key built from those.
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
            return self._is_dup_by_raw_key(raw_key, _exch, ts)
        else:
            occ_symbol = str(event_or_occ_symbol)
            _size      = int(size)
            _fill      = float(fill)
            _exch      = str(exchange) if exchange is not None else ''
            key = make_key(occ_symbol, _size, _fill)
            return self._is_dup_by_raw_key(key, _exch, ts)

    def _is_dup_by_raw_key(self, key: str, exchange: str, ts: Optional[float]) -> bool:
        now = ts if ts is not None else time.time()
        self._cleanup()

        first_seen = self._seen.get(key)
        if first_seen is not None and (now - first_seen) < self._ttl:
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
