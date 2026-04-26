"""
utils/dedup.py — Layer 4 TTL deduplication cache for Tradier stream events.

Problem: A single options trade prints on multiple exchanges within a reporting
window. Without dedup, the same trade creates multiple DB rows and inflates
premium tallies in the RepetitionAccumulator.

Key design:
  dedup key  = (occ_symbol, size, round(fill, 2))
  canonical  = first event seen for this key
  duplicates = any subsequent event for same key within ttl_seconds
"""
import time
from collections import defaultdict
from typing import Any, Optional


def make_key(occ_symbol: str, size: int, fill: float) -> str:
    """
    Canonical dedup key — exported so tests can verify key construction.
    fill rounded to 2dp absorbs ±$0.01 feed rounding across exchanges.
    """
    return f"{occ_symbol}|{size}|{fill:.2f}"


class DedupCache:
    """
    Asyncio-safe TTL dedup cache with sweep detection.

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

        self._last_cleanup = time.monotonic()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def ttl_seconds(self) -> float:
        """The configured TTL in seconds (read-only, tests access this)."""
        return self._ttl

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cleanup(self):
        now = time.monotonic()
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
            # Single-object form: derive fields from the event object
            ev = event_or_occ_symbol
            ticker   = str(getattr(ev, 'ticker',        getattr(ev, 'occ_symbol', '')))
            strike   = float(getattr(ev, 'strike',      0))
            expiry   = str(getattr(ev, 'expiry',        ''))
            ctype    = str(getattr(ev, 'contract_type', ''))
            _size    = int(getattr(ev, 'size',          0))
            _fill    = float(getattr(ev, 'fill',        strike))  # use strike as fill proxy
            _exch    = str(getattr(ev, 'exchange',      ''))
            # Build a key that includes all event-identifying fields
            raw_key = f"{ticker}|{expiry}|{ctype}|{strike:.2f}|{_size}|{_fill:.2f}"
            return self._is_dup_by_raw_key(raw_key, _exch, ts)
        else:
            occ_symbol = str(event_or_occ_symbol)
            _size      = int(size)
            _fill      = float(fill)
            _exch      = str(exchange) if exchange is not None else ''
            key = make_key(occ_symbol, _size, _fill)
            return self._is_dup_by_raw_key(key, _exch, ts)

    def _is_dup_by_raw_key(self, key: str, exchange: str, ts: Optional[float]) -> bool:
        now = ts if ts is not None else time.monotonic()
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
        self._seen[key] = time.monotonic()

    def size(self) -> int:
        return len(self._seen)

    def clear(self) -> None:
        self._seen.clear()
        self._exchange_hits.clear()

    def evict_expired(self) -> int:
        now = time.monotonic()
        cutoff = now - self._ttl
        expired = [k for k, ts in self._seen.items() if ts <= cutoff]
        for k in expired:
            del self._seen[k]
        return len(expired)

    def get_exchange_count(self, occ_symbol: str, size: int, fill: float) -> int:
        ckey   = self._contract_key(occ_symbol, size, fill)
        now    = time.monotonic()
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
