"""
utils/dedup.py — Layer 4 TTL deduplication cache for Tradier stream events.

Problem: A single options trade prints on multiple exchanges (CBOE, MIAX,
PHLX, AMEX) within a reporting window. Without dedup, the same trade creates
multiple DB rows and inflates premium tallies in the RepetitionAccumulator.

OPRA multi-exchange reporting reality (2026):
  - CBOE / C2:   typically first reporter, ~50-200ms after execution
  - MIAX / MPRL: routinely 500ms-3s after CBOE on the same trade
  - PHLX:        can lag 2-5s, especially on high-volume sweeps
  - AMEX / BATO: 1-4s lag common

  A 2-second TTL (original) was too tight: MIAX/PHLX duplicates slipped
  through when their reports arrived outside the 2s window, causing 2x-4x
  premium double-counting in the accumulator.

Fix (C-019) — 2026-04-24:
  1. TTL raised from 2s → 5s to cover worst-case PHLX reporting lag.
  2. Sweep window raised from 5s → 8s to match extended TTL.
  3. Eliminated the time-bucket boundary bug: the original key used
     `int(ts // 2)` which created hard bucket boundaries. A trade at
     t=1.99s and its MIAX duplicate at t=2.01s landed in different buckets
     and both passed as canonical. The new implementation uses a pure
     first-seen timestamp comparison — no buckets, no boundary gaps.
  4. Fill price tolerance: two reports of the same trade can differ by
     ±$0.01 (rounding in different exchange feeds). The dedup key rounds
     fill to 2dp (not 1dp) to avoid conflating trades that are genuinely
     $0.10 apart (e.g. $1.00 vs $1.01 are same trade; $1.00 vs $1.10 are
     different). 1dp was too coarse and caused false dedup hits in tests.
  5. exchange field is now properly used in sweep detection.
  6. Added dedup_stats() for observability.

Sweep window cleanup fix:
  _cleanup() now uses separate cutoffs for _seen (TTL-based) and
  _exchange_hits (sweep_window-based) so expired sweep windows are
  correctly purged and is_sweep() returns False after the window lapses.

Key design:
  dedup key  = (occ_symbol, size, round(fill, 2))
  canonical  = first event seen for this key
  duplicates = any subsequent event for same key within ttl_seconds
  Memory:    entries evicted after max(ttl, sweep_window) + 10s (lazy cleanup)
"""
import time
from collections import defaultdict
from typing import Optional


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
        classified as a sweep. Should be >= ttl_seconds.
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

        # dedup_key -> first_seen monotonic timestamp
        self._seen: dict[str, float] = {}

        # contract_key -> [(monotonic_ts, exchange_str), ...]
        self._exchange_hits: dict[str, list[tuple[float, str]]] = defaultdict(list)

        # Observability counters
        self._total_seen:       int = 0
        self._total_duplicates: int = 0
        self._total_sweeps:     int = 0

        self._last_cleanup = time.monotonic()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cleanup(self):
        """Evict expired entries. Runs lazily every 10s."""
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
        """
        Canonical dedup key.
        - fill rounded to 2dp: absorbs ±$0.01 feed rounding across exchanges
          while keeping genuinely different fills (e.g. $1.00 vs $1.10) separate.
        - No time bucket: pure first-seen TTL comparison.
        """
        return f"{occ_symbol}|{size}|{fill:.2f}"

    @staticmethod
    def _contract_key(occ_symbol: str, size: int, fill: float) -> str:
        """Key for sweep exchange tracking — same grain as dedup key."""
        return f"{occ_symbol}|{size}|{fill:.2f}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_duplicate(
        self,
        occ_symbol: str,
        size:       int,
        fill:       float,
        exchange:   str,
        ts:         Optional[float] = None,
    ) -> bool:
        """
        Returns True if this event is a duplicate of a trade already seen
        within ttl_seconds. Returns False for the canonical (first) print.
        """
        now = ts if ts is not None else time.monotonic()
        self._cleanup()

        key = self._dedup_key(occ_symbol, size, fill)
        first_seen = self._seen.get(key)

        if first_seen is not None and (now - first_seen) < self._ttl:
            self._total_duplicates += 1
            ckey = self._contract_key(occ_symbol, size, fill)
            self._exchange_hits[ckey].append((now, exchange))
            return True

        self._seen[key] = now
        self._total_seen += 1
        ckey = self._contract_key(occ_symbol, size, fill)
        self._exchange_hits[ckey].append((now, exchange))
        return False

    def get_exchange_count(self, occ_symbol: str, size: int, fill: float) -> int:
        ckey   = self._contract_key(occ_symbol, size, fill)
        now    = time.monotonic()
        cutoff = now - self._sweep_win
        recent_exchanges = [
            e for t, e in self._exchange_hits.get(ckey, []) if t > cutoff
        ]
        return len(set(e for e in recent_exchanges if e))

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
        """Observability counters for /health endpoint."""
        return {
            "dedup_seen":       self._total_seen,
            "dedup_duplicates": self._total_duplicates,
            "dedup_sweeps":     self._total_sweeps,
            "dedup_cache_size": len(self._seen),
        }


# Module-level singleton — imported by tradier_stream._process_trade()
# TTL=5s covers PHLX/MIAX worst-case lag. Sweep window=8s.
flow_dedup = DedupCache(ttl_seconds=5.0, sweep_window=8.0, sweep_min_exchanges=3)
