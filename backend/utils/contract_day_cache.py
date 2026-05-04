"""
contract_day_cache.py — Async TTL cache for per-contract multi-day lookback.

Owns all cache + DB fetch logic for ING-007 multi-day repeat enrichment.
This module is READ-ONLY with respect to flow_events — it never writes.
The write path lives in flow_store.py.

Cold-cache behaviour (first-episode per session):
  The background asyncio.Queue in flow_store.py enqueues a ContractKey
  after the first episode for that contract arrives. Until the queue
  worker calls get_lookback() and the DB fetch completes, the cache
  entry for that key does not exist. Callers receive a zero-valued
  LookbackResult (prior_days_active=0, prior_days_aggressive=0) on
  the first episode. This is acceptable — is_multi_day_repeat is an
  enrichment flag, not a hard gate (SA-Q1, 2026-05-04).

is_aggressive cold-start lag:
  Existing flow_events rows have is_aggressive=FALSE (S2.5 column default).
  prior_days_aggressive will be 0 for all historical data until ~5 trading
  days of newly-flagged aggressive rows accumulate post-deploy. This lag
  is unavoidable and intentional — no backfill attempted (SA, 2026-05-04).

Cache TTL: 300 seconds. Stale reads are acceptable because is_multi_day_repeat
is enrichment-only. The cache is not consulted on the hot path — lookback
enrichment is async via background queue.

Dependencies: supabase client via db.client (same pattern as other services).
No new dependencies added (cachetools absent from requirements.txt;
redis is present but a network roundtrip per lookback is unnecessary for
an in-process TTL cache of this simplicity).
"""
import logging
import os
import time
from typing import Dict, Optional, Tuple

try:
    from typing import NamedTuple
except ImportError:  # pragma: no cover
    from typing_extensions import NamedTuple  # type: ignore

import httpx

log = logging.getLogger("contract_day_cache")

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

ContractKey = Tuple[str, str, float, str]  # (ticker, contract_type, strike, expiry)


class LookbackResult(NamedTuple):
    prior_days_active:     int    # COUNT(DISTINCT DATE(created_at)) on qualifying prior flow
    prior_days_aggressive: int    # same, filtered WHERE is_aggressive = TRUE
    fetched_at:            float  # time.monotonic() at fetch time


_ZERO_RESULT = LookbackResult(prior_days_active=0, prior_days_aggressive=0, fetched_at=0.0)

# ---------------------------------------------------------------------------
# Cache store — module-level dict, manual TTL pattern (PBE-Q1, 2026-05-04)
# ---------------------------------------------------------------------------

_cache: Dict[ContractKey, LookbackResult] = {}
_CACHE_TTL_S: float = 300.0  # 5 minutes


def _is_fresh(result: LookbackResult) -> bool:
    """Return True if the cached result is within TTL."""
    return (time.monotonic() - result.fetched_at) < _CACHE_TTL_S


# ---------------------------------------------------------------------------
# Supabase connection helpers
# ---------------------------------------------------------------------------

_SUPABASE_URL: Optional[str] = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY: Optional[str] = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")
)


def _is_configured() -> bool:
    return bool(_SUPABASE_URL and _SUPABASE_KEY)


def _headers() -> dict:
    return {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type":  "application/json",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_lookback(key: ContractKey, min_premium: float) -> LookbackResult:
    """
    Return cached LookbackResult if fresh; otherwise fetch from DB, cache, and return.

    key        : 4-tuple (ticker, contract_type, strike, expiry)
    min_premium: DTE-tier floor passed from accumulator context — no hardcoding in query

    On DB unavailability or error: returns _ZERO_RESULT and logs a warning.
    The caller (flow_store queue worker) does not propagate exceptions.
    """
    cached = _cache.get(key)
    if cached is not None and _is_fresh(cached):
        return cached

    result = await _fetch_from_db(key, min_premium)
    _cache[key] = result
    return result


async def _fetch_from_db(key: ContractKey, min_premium: float) -> LookbackResult:
    """
    Execute the ING-007 6-param lookback query against flow_events.

    Query shape (PBE decision, 2026-05-04):
        SELECT
            COUNT(DISTINCT DATE(created_at))                                       AS prior_days_active,
            COUNT(DISTINCT DATE(created_at)) FILTER (WHERE is_aggressive = TRUE)   AS prior_days_aggressive
        FROM flow_events
        WHERE ticker        = $1
          AND contract_type = $2
          AND strike        = $3
          AND expiry        = $4
          AND created_at   >= NOW() - INTERVAL '5 days'
          AND created_at   <  DATE_TRUNC('day', NOW())   -- exclude today
          AND premium      >= $5                         -- DTE-tier floor

    DATE_TRUNC ceiling is critical: today's flow must not count toward
    prior_days_active. Today's episode is what is being evaluated.

    Uses Supabase PostgREST RPC endpoint to execute the aggregation.
    Falls back to _ZERO_RESULT on any error so the hot path is never blocked.
    """
    if not _is_configured():
        log.debug("[contract_day_cache] not configured — returning zero result")
        return LookbackResult(prior_days_active=0, prior_days_aggressive=0, fetched_at=time.monotonic())

    ticker, contract_type, strike, expiry = key

    payload = {
        "p_ticker":        ticker,
        "p_contract_type": contract_type,
        "p_strike":        float(strike),
        "p_expiry":        expiry,
        "p_min_premium":   float(min_premium),
    }

    url = f"{_SUPABASE_URL}/rest/v1/rpc/get_contract_prior_days"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, headers=_headers(), json=payload)

        if resp.status_code == 200:
            data = resp.json()
            active     = int(data.get("prior_days_active", 0) or 0)
            aggressive = int(data.get("prior_days_aggressive", 0) or 0)
            result = LookbackResult(
                prior_days_active=active,
                prior_days_aggressive=aggressive,
                fetched_at=time.monotonic(),
            )
            log.debug(
                f"[contract_day_cache] {ticker} {contract_type} {strike} {expiry}: "
                f"active={active} aggressive={aggressive}"
            )
            return result

        log.warning(
            f"[contract_day_cache] DB fetch failed {resp.status_code}: {resp.text[:200]}"
        )
        return LookbackResult(prior_days_active=0, prior_days_aggressive=0, fetched_at=time.monotonic())

    except Exception as exc:
        log.warning(f"[contract_day_cache] _fetch_from_db exception: {exc}")
        return LookbackResult(prior_days_active=0, prior_days_aggressive=0, fetched_at=time.monotonic())


def clear_cache() -> None:
    """Clear all cached entries. Used in tests to reset state between cases."""
    _cache.clear()
