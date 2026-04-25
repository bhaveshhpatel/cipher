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
     ±$0.01 (rounding in different exchange feeds). The dedup key now
     rounds fill to 1dp instead of 2dp to absorb these micro-differences
     without conflating genuinely different strikes.
  5. exchange field is now properly used in sweep detection. Previously
     is_duplicate() accepted exchange but _process_trade() never passed it,
     so sweep detection always saw one unique exchange and never fired.
     Now tradier_stream.py passes trade_payload.get("exch") or
     trade_payload.get("exchange", "") through.
  6. Added dedup_stats() for observability — exposes total_seen,
     total_duplicates, total_sweeps counters via the /health endpoint.

Sweep detection:
  If the same contract (occ_symbol + size + fill_1dp) is reported by 3+
  distinct exchanges within sweep_window (8s), the canonical event is
  upgraded to trade_type=SWEEP and exchange_count reflects the real count.

Key design:
  dedup key  = (occ_symbol, size, round(fill, 1))
  canonical  = first event seen for this key
  duplicates = any subsequent event for same key within ttl_seconds
  Memory:    entries evicted 10s after TTL expires (lazy cleanup)
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
        cutoff = now - max(self._ttl, self._sweep_win)
        self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
        self._exchange_hits = defaultdict(list, {
            k: [(t, e) for t, e in v if t > cutoff]
            for k, v in self._exchange_hits.items()
            if any(t > cutoff for t, _ in v)
        })
        self._last_cleanup = now

    @staticmethod
    def _dedup_key(occ_symbol: str, size: int, fill: float) -> str:
        """
        Canonical dedup key.
        - fill rounded to 1dp: absorbs ±$0.01 feed rounding across exchanges
          while keeping genuinely different trades (e.g. $1.40 vs $1.50)
          correctly separate.
        - No time bucket: pure first-seen TTL comparison eliminates the
          bucket-boundary gap that existed with `int(ts // 2)` bucketing.
        """
        return f"{occ_symbol}|{size}|{fill:.1f}"

    @staticmethod
    def _contract_key(occ_symbol: str, size: int, fill: float) -> str:
        """Key for sweep exchange tracking — same grain as dedup key."""
        return f"{occ_symbol}|{size}|{fill:.1f}"

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

        Side effects:
          - Records first_seen timestamp for new canonical prints.
          - Appends (ts, exchange) to exchange_hits for sweep detection.
          - Increments observability counters.

        Parameters
        ----------
        occ_symbol : OCC contract string e.g. 'AAPL  260117C00180000'
        size       : contract count
        fill       : fill price (last)
        exchange   : exchange code from Tradier payload e.g. 'C', 'M', 'Q'
                     Pass empty string if not available — sweep detection
                     will degrade gracefully but not fire incorrectly.
        ts         : monotonic timestamp (time.monotonic()). If None, uses
                     current time. Always pass the event's arrival time
                     rather than letting the cache use wall-clock time.
        """
        now = ts if ts is not None else time.monotonic()
        self._cleanup()

        key = self._dedup_key(occ_symbol, size, fill)
        first_seen = self._seen.get(key)

        if first_seen is not None and (now - first_seen) < self._ttl:
            # Duplicate: same trade seen within TTL window
            self._total_duplicates += 1
            # Still track exchange for sweep detection on the canonical event
            ckey = self._contract_key(occ_symbol, size, fill)
            self._exchange_hits[ckey].append((now, exchange))
            return True

        # Canonical (first) print or TTL expired (treat as new trade)
        self._seen[key] = now
        self._total_seen += 1
        ckey = self._contract_key(occ_symbol, size, fill)
        self._exchange_hits[ckey].append((now, exchange))
        return False

    def get_exchange_count(self, occ_symbol: str, size: int, fill: float) -> int:
        """
        Returns the number of unique exchanges that have reported this trade
        within sweep_window. Call after is_duplicate() returns False.
        """
        ckey   = self._contract_key(occ_symbol, size, fill)
        now    = time.monotonic()
        cutoff = now - self._sweep_win
        recent_exchanges = [
            e for t, e in self._exchange_hits.get(ckey, []) if t > cutoff
        ]
        return len(set(e for e in recent_exchanges if e))  # ignore empty string

    def is_sweep(
        self,
        occ_symbol: str,
        size:       int,
        fill:       float,
    ) -> bool:
        """
        Returns True if this contract has printed on sweep_min_exchanges+
        distinct exchanges within sweep_window. Call AFTER is_duplicate()
        returns False (i.e. on the canonical print only).
        """
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
