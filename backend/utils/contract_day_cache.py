# =============================================================================
# contract_day_cache.py  (ING-007 — multiday repeat gate)
#
# DEPLOY NOTE (ING-007 / PBE — 2026-05-04):
#   New module. No schema change required at runtime — idx_flow_events_contract_day
#   was applied to Supabase via Supabase MCP migration 2026-05-04 (S2.5).
#
#   PRODUCTION IMPACT: Gate 5 (multiday repeat) will suppress episodes that
#   previously emitted on day-1 of a contract's activity. Monitor signal
#   volume on first deploy — if a contract only appeared today, prior_days_active
#   will be 0 and the episode will not emit until day 2+.
#
#   ROLLBACK: Pass require_multiday=False to RepetitionAccumulator at
#   instantiation to disable Gate 5 without a full PR revert.
#
#   TTL: Cache entries expire after ttl_seconds (default 300s / 5 min).
#   Each unique (ticker, contract_type, strike, expiry) 4-tuple is cached
#   independently. Stale entries are evicted lazily on access.
#
#   DB QUERY: Uses DATE_TRUNC('day', NOW()) as the upper bound (exclusive)
#   so today's activity never counts as a "prior" day. Lower bound is
#   NOW() - INTERVAL '5 days' (5 calendar days, not trading days).
#   qualifying_flow = True when premium >= 50_000 (configurable).
# =============================================================================
"""
contract_day_cache.py — async DB lookback cache for ING-007 multiday repeat gate.

Provides ContractDayCache, a lightweight async-safe TTL cache that queries
flow_events for prior-day activity on a given (ticker, contract_type, strike,
expiry) 4-tuple.

Design decisions (ING-007 deliberation 2026-05-04):

  Lookback window: 5 calendar days (not trading days). SA decision: calendar
  days are simpler, deterministic, and avoid dependency on a trading calendar.
  Weekend gaps accepted — a contract active Mon + Wed across a weekend still
  shows prior_days_active >= 2.

  DATE_TRUNC ceiling: upper bound is DATE_TRUNC('day', NOW()) exclusive so
  today never counts as a prior day. SA + PBE joint decision.

  Dual counters: prior_days_active (all qualifying flow days) and
  prior_days_aggressive (subset with is_aggressive=TRUE). Gate 5 in the
  accumulator uses prior_days_active >= 2 as the qualifying threshold.
  prior_days_aggressive is persisted for signal enrichment / future gates.

  qualifying_flow threshold: premium >= 50_000 per event to count toward
  a qualifying day. SA decision: prevents 1-contract noise fills from
  inflating the day counter.

  TTL: 300 seconds default. PBE decision: cache per 4-tuple independently
  so high-velocity tickers (SPY, QQQ) don't block cache refreshes for
  low-activity tickers. Lazy eviction on access — no background sweep.

  4-tuple cache key: (ticker, contract_type, strike, expiry). PBE decision:
  matches the episode key in RepetitionAccumulator._episode_key() exactly.
  strike is stored as a string-formatted float to avoid float hash drift.

  HTTP client: uses the same httpx + Supabase REST pattern as flow_store.py.
  No new dependencies required.

  Threading: asyncio.Lock per cache instance. ContractDayCache is instantiated
  once at startup and shared across all ingest_tick() calls from the stream
  worker. Lock ensures concurrent ticks for the same 4-tuple do not issue
  redundant DB queries during a cache miss."""

import asyncio
import logging
import os
import time
from typing import Dict, Optional, Tuple

import httpx

log = logging.getLogger("contract_day_cache")

_SUPABASE_URL: Optional[str] = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY: Optional[str] = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")
)

# Default qualifying-flow premium floor (SA decision: 50_000)
_DEFAULT_QUALIFYING_PREMIUM: float = 50_000.0

# Default lookback window in calendar days (SA decision: 5)
_DEFAULT_LOOKBACK_DAYS: int = 5

# Default TTL in seconds (PBE decision: 300)
_DEFAULT_TTL_SECONDS: int = 300


# ---------------------------------------------------------------------------
# Cache entry dataclass (plain tuple for zero-dep speed)
# ---------------------------------------------------------------------------
# CacheEntry: (prior_days_active, prior_days_aggressive, expires_at)
CacheEntry = Tuple[int, int, float]


class ContractDayCache:
    """
    Async TTL cache for prior-day activity lookback (ING-007 Gate 5).

    Constructor parameters
    ----------------------
    ttl_seconds : int
        Cache entry lifetime in seconds. Default 300.
    lookback_days : int
        Calendar days to look back from DATE_TRUNC('day', NOW()). Default 5.
    qualifying_premium : float
        Minimum event premium for a day to count as active. Default 50_000.

    Usage
    -----
        cache = ContractDayCache()
        result = await cache.get(ticker, contract_type, strike, expiry)
        # result.prior_days_active  -> int
        # result.prior_days_aggressive -> int
    """

    def __init__(
        self,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
        qualifying_premium: float = _DEFAULT_QUALIFYING_PREMIUM,
    ) -> None:
        self._ttl         = ttl_seconds
        self._lookback    = lookback_days
        self._qual_prem   = qualifying_premium
        self._cache: Dict[Tuple[str, str, str, str], CacheEntry] = {}
        self._lock        = asyncio.Lock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_key(
        ticker: str,
        contract_type: str,
        strike: float,
        expiry: str,
    ) -> Tuple[str, str, str, str]:
        """
        4-tuple cache key. Strike normalised to fixed-precision string to
        avoid float hash drift (e.g. 500.0 vs 500.00000001).
        """
        return (ticker, contract_type, f"{strike:.2f}", expiry)

    def _is_fresh(self, entry: CacheEntry) -> bool:
        _, _, expires_at = entry
        return time.monotonic() < expires_at

    def _headers(self) -> dict:
        return {
            "apikey":        _SUPABASE_KEY,
            "Authorization": f"Bearer {_SUPABASE_KEY}",
            "Content-Type":  "application/json",
        }

    async def _fetch_from_db(
        self,
        ticker: str,
        contract_type: str,
        strike: float,
        expiry: str,
    ) -> Tuple[int, int]:
        """
        Execute the 6-param lookback query against flow_events via Supabase RPC.

        Returns (prior_days_active, prior_days_aggressive).

        Uses a raw SQL RPC call via /rest/v1/rpc/contract_day_lookback so the
        DATE_TRUNC ceiling and FILTER clause are evaluated server-side.

        Falls back to (0, 0) on any HTTP or parse error — Gate 5 will pass
        through (not block) on DB failure to avoid false suppression.
        """
        if not _SUPABASE_URL or not _SUPABASE_KEY:
            log.debug("[contract_day_cache] Supabase not configured — returning (0,0)")
            return (0, 0)

        # Build the RPC payload. The Postgres function contract_day_lookback
        # is defined in the S2.5 migration and accepts these exact param names.
        payload = {
            "p_ticker":        ticker,
            "p_contract_type": contract_type,
            "p_strike":        float(strike),
            "p_expiry":        expiry,
            "p_lookback_days": self._lookback,
            "p_min_premium":   self._qual_prem,
        }

        url = f"{_SUPABASE_URL}/rest/v1/rpc/contract_day_lookback"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, headers=self._headers(), json=payload)

            if resp.status_code in (200, 201):
                data = resp.json()
                # RPC returns a single row: {prior_days_active, prior_days_aggressive}
                if isinstance(data, list) and len(data) > 0:
                    row = data[0]
                elif isinstance(data, dict):
                    row = data
                else:
                    log.warning(
                        f"[contract_day_cache] unexpected RPC response shape: {data!r}"
                    )
                    return (0, 0)

                active     = int(row.get("prior_days_active", 0) or 0)
                aggressive = int(row.get("prior_days_aggressive", 0) or 0)
                return (active, aggressive)

            log.warning(
                f"[contract_day_cache] RPC failed {resp.status_code}: {resp.text[:200]}"
            )
            return (0, 0)

        except Exception as exc:
            log.error(f"[contract_day_cache] DB fetch exception: {exc}")
            return (0, 0)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(
        self,
        ticker: str,
        contract_type: str,
        strike: float,
        expiry: str,
    ) -> Tuple[int, int]:
        """
        Return (prior_days_active, prior_days_aggressive) for this 4-tuple.

        Hits cache if entry is fresh (within TTL). Otherwise issues one DB
        query under the lock (prevents concurrent cache-miss stampede for
        the same key on high-velocity tickers).

        Returns (0, 0) on DB error — Gate 5 treats this as "no prior days"
        and will suppress the episode. This is the safer failure mode:
        a DB outage should not cause a flood of unvalidated signals.
        """
        key = self._make_key(ticker, contract_type, strike, expiry)

        # Fast path: fresh cache hit (no lock)
        entry = self._cache.get(key)
        if entry and self._is_fresh(entry):
            active, aggressive, _ = entry
            return (active, aggressive)

        # Slow path: acquire lock, re-check (double-checked locking pattern)
        async with self._lock:
            entry = self._cache.get(key)
            if entry and self._is_fresh(entry):
                active, aggressive, _ = entry
                return (active, aggressive)

            active, aggressive = await self._fetch_from_db(
                ticker, contract_type, strike, expiry
            )
            expires_at = time.monotonic() + self._ttl
            self._cache[key] = (active, aggressive, expires_at)
            log.debug(
                f"[contract_day_cache] cache miss — {ticker} {contract_type} "
                f"strike={strike} expiry={expiry} "
                f"prior_days_active={active} prior_days_aggressive={aggressive}"
            )
            return (active, aggressive)

    def invalidate(self, ticker: str, contract_type: str, strike: float, expiry: str) -> None:
        """Force-expire a cache entry. Useful for testing and backfill scenarios."""
        key = self._make_key(ticker, contract_type, strike, expiry)
        self._cache.pop(key, None)

    def clear(self) -> None:
        """Clear all cache entries. Called at market close / stream restart."""
        self._cache.clear()
        log.info("[contract_day_cache] cache cleared")

    @property
    def size(self) -> int:
        """Number of entries currently in cache (includes stale entries)."""
        return len(self._cache)
