"""
utils/dedup.py — 2-second TTL deduplication cache for Tradier stream events.

Problem: A single options trade prints on multiple exchanges (N, C, M, Q)
within ~200ms. Without dedup, the same trade creates 4 DB rows.

Solution: Key each event on (occ_symbol, size, fill_price_2dp, time_bucket_2s).
If seen within the window → drop as duplicate. If first seen → pass through
and mark as canonical. Sweep detection: if the same contract hits 3+
different exchanges → mark trade_type = SWEEP.
"""
import time
from collections import defaultdict
from typing import Optional


class DedupCache:
    """
    Thread-safe (asyncio-safe) TTL dedup cache.
    Uses pure Python dicts with manual TTL — no external dependencies.
    """

    def __init__(self, ttl_seconds: float = 2.0, sweep_window: float = 5.0, sweep_min_exchanges: int = 3):
        self._ttl = ttl_seconds
        self._sweep_window = sweep_window
        self._sweep_min = sweep_min_exchanges
        # {dedup_key: first_seen_ts}
        self._seen: dict[str, float] = {}
        # {contract_key: [(ts, exchange), ...]}
        self._exchange_hits: dict[str, list[tuple[float, str]]] = defaultdict(list)
        self._last_cleanup = time.monotonic()

    def _cleanup(self):
        """Evict expired entries — runs every ~10s."""
        now = time.monotonic()
        if now - self._last_cleanup < 10.0:
            return
        cutoff = now - max(self._ttl, self._sweep_window)
        self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        self._exchange_hits = defaultdict(list, {
            k: [(t, e) for t, e in v if t > cutoff]
            for k, v in self._exchange_hits.items()
            if any(t > cutoff for t, _ in v)
        })
        self._last_cleanup = now

    def _dedup_key(self, occ_symbol: str, size: int, fill: float, ts: float) -> str:
        """Bucket fill price to 2dp and time to 2s window."""
        time_bucket = int(ts // 2)
        return f"{occ_symbol}|{size}|{fill:.2f}|{time_bucket}"

    def is_duplicate(self, occ_symbol: str, size: int, fill: float, exchange: str, ts: Optional[float] = None) -> bool:
        """
        Returns True if this event is a duplicate (same trade seen on another exchange).
        Returns False if this is the first/canonical print — caller should persist it.
        Also tracks exchange hits for sweep detection.
        """
        now = ts or time.monotonic()
        self._cleanup()

        key = self._dedup_key(occ_symbol, size, fill, now)
        if key in self._seen:
            return True  # duplicate — drop it

        self._seen[key] = now
        # Track exchange hits for sweep detection
        contract_key = f"{occ_symbol}|{size}|{fill:.2f}"
        self._exchange_hits[contract_key].append((now, exchange))
        return False

    def is_sweep(self, occ_symbol: str, size: int, fill: float) -> bool:
        """
        Returns True if this contract has printed on 3+ exchanges within
        the sweep_window — indicating a sweep order.
        Call AFTER is_duplicate() returns False (i.e. on the canonical print).
        """
        contract_key = f"{occ_symbol}|{size}|{fill:.2f}"
        now = time.monotonic()
        cutoff = now - self._sweep_window
        recent = [e for t, e in self._exchange_hits.get(contract_key, []) if t > cutoff]
        unique_exchanges = len(set(recent))
        return unique_exchanges >= self._sweep_min


# Module-level singleton used across the codebase
flow_dedup = DedupCache(ttl_seconds=2.0, sweep_window=5.0, sweep_min_exchanges=3)
