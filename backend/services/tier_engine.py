"""
services/tier_engine.py — Feature 4A: Dynamic Symbol Tiering

Classifies each symbol in the universe into a tier based on the
admin-configurable thresholds stored in the `tier_thresholds` table.

Tier definitions:
  T1 — Liquid large-caps  (SPY, AAPL, TSLA, NVDA …)
         volume >= t1_min_volume AND price >= t1_min_last_price AND avg_chain_oi >= t1_min_oi
  T2 — Mid-cap optionable (HOOD, SOFI, RIVN …)
         volume >= t2_min_volume AND price >= t2_min_last_price AND avg_chain_oi >= t2_min_oi
  T3 — Standard           (everything else that passes the universe filter)

OI source (Feature 4A-OI):
  quote.open_interest is populated by main.py from registry.get_oi_map()
  before assign_tiers() is called.

Caching:
  Thresholds are fetched from DB and cached for CACHE_TTL seconds (default 300).
  Cache is invalidated when invalidate_cache() / invalidate_thresholds_cache() is called.

Test hooks:
  assign_tiers(quotes, thresholds=None) — pass a thresholds dict to bypass DB fetch.
  invalidate_thresholds_cache()         — alias for invalidate_cache() used by tests.
  _thresh_cache_ts                      — exposed for test assertions.

DB-error safety:
  When _fetch_thresholds() raises (e.g. DNS failure in CI), assign_tiers() falls
  back to _SAFE_FALLBACK_THRESHOLDS which sets all minimums to infinity so every
  symbol lands in T3 (safest / least-privileged tier).  Tests assert result == 3
  for this path.
"""
import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import httpx

if TYPE_CHECKING:
    from services.symbols_loader import SymbolQuote

log = logging.getLogger("tier_engine")

_SUPABASE_URL: Optional[str] = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY: Optional[str] = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")
)

CACHE_TTL = 300  # seconds

# Default thresholds — mirrors migration 011 defaults
_DEFAULT_THRESHOLDS: dict = {
    "t1_min_volume":     20_000_000,
    "t1_min_last_price": 10.0,
    "t1_min_oi":         1_000,
    "t1_atm_pct":        0.20,
    "t1_max_dte":        90,

    "t2_min_volume":     2_000_000,
    "t2_min_last_price": 10.0,
    "t2_min_oi":         500,
    "t2_atm_pct":        0.15,
    "t2_max_dte":        60,

    "t3_min_volume":     500_000,
    "t3_min_last_price": 1.0,
    "t3_min_oi":         100,
    "t3_atm_pct":        0.10,
    "t3_max_dte":        30,
}

# Safe fallback used when the DB is unreachable — all minimums set to
# infinity so _classify() falls through to T3 for every symbol.
_SAFE_FALLBACK_THRESHOLDS: dict = {
    "t1_min_volume":     float("inf"),
    "t1_min_last_price": float("inf"),
    "t1_min_oi":         float("inf"),
    "t1_atm_pct":        0.20,
    "t1_max_dte":        90,

    "t2_min_volume":     float("inf"),
    "t2_min_last_price": float("inf"),
    "t2_min_oi":         float("inf"),
    "t2_atm_pct":        0.15,
    "t2_max_dte":        60,

    "t3_min_volume":     0,
    "t3_min_last_price": 0.0,
    "t3_min_oi":         0,
    "t3_atm_pct":        0.10,
    "t3_max_dte":        30,
}

_cache: dict        = {}
_cache_ts: float    = 0.0
_thresh_cache_ts: float = 0.0   # exposed alias used by tests


@dataclass
class _TierParams:
    """Container for per-tier filter parameters. min_oi defaults to 0 for test convenience."""
    atm_pct: float
    max_dte: int
    min_oi:  int = 0


def _headers() -> dict:
    return {
        "apikey":        _SUPABASE_KEY or "",
        "Authorization": f"Bearer {_SUPABASE_KEY or ''}",
        "Content-Type":  "application/json",
    }


async def _fetch_thresholds(force: bool = False) -> dict:
    """Fetch active tier_thresholds row from Supabase with TTL cache."""
    global _cache, _cache_ts, _thresh_cache_ts

    if not force and _cache and (time.monotonic() - _cache_ts) < CACHE_TTL:
        return dict(_cache)

    if not _SUPABASE_URL or not _SUPABASE_KEY:
        log.warning("[tier_engine] Supabase not configured — using default thresholds")
        return dict(_DEFAULT_THRESHOLDS)

    url = (
        f"{_SUPABASE_URL}/rest/v1/tier_thresholds"
        "?is_active=eq.true&order=id.desc&limit=1"
    )
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(url, headers=_headers())
    if resp.status_code == 200:
        rows = resp.json()
        if rows:
            row = rows[0]
            result = {
                "t1_min_volume":     int(row.get("t1_min_volume",     _DEFAULT_THRESHOLDS["t1_min_volume"])),
                "t1_min_last_price": float(row.get("t1_min_last_price", _DEFAULT_THRESHOLDS["t1_min_last_price"])),
                "t1_min_oi":         int(row.get("t1_min_oi",         _DEFAULT_THRESHOLDS["t1_min_oi"])),
                "t1_atm_pct":        float(row.get("t1_atm_pct",        _DEFAULT_THRESHOLDS["t1_atm_pct"])),
                "t1_max_dte":        int(row.get("t1_max_dte",         _DEFAULT_THRESHOLDS["t1_max_dte"])),

                "t2_min_volume":     int(row.get("t2_min_volume",     _DEFAULT_THRESHOLDS["t2_min_volume"])),
                "t2_min_last_price": float(row.get("t2_min_last_price", _DEFAULT_THRESHOLDS["t2_min_last_price"])),
                "t2_min_oi":         int(row.get("t2_min_oi",         _DEFAULT_THRESHOLDS["t2_min_oi"])),
                "t2_atm_pct":        float(row.get("t2_atm_pct",        _DEFAULT_THRESHOLDS["t2_atm_pct"])),
                "t2_max_dte":        int(row.get("t2_max_dte",         _DEFAULT_THRESHOLDS["t2_max_dte"])),

                "t3_min_volume":     int(row.get("t3_min_volume",     _DEFAULT_THRESHOLDS["t3_min_volume"])),
                "t3_min_last_price": float(row.get("t3_min_last_price", _DEFAULT_THRESHOLDS["t3_min_last_price"])),
                "t3_min_oi":         int(row.get("t3_min_oi",         _DEFAULT_THRESHOLDS["t3_min_oi"])),
                "t3_atm_pct":        float(row.get("t3_atm_pct",        _DEFAULT_THRESHOLDS["t3_atm_pct"])),
                "t3_max_dte":        int(row.get("t3_max_dte",         _DEFAULT_THRESHOLDS["t3_max_dte"])),
            }
            _cache          = result
            _cache_ts       = time.monotonic()
            _thresh_cache_ts = _cache_ts
            return dict(result)
    log.warning("[tier_engine] DB fetch failed: HTTP %d — using defaults", resp.status_code)
    return dict(_DEFAULT_THRESHOLDS)


def _classify(quote: "SymbolQuote", thresh: dict) -> int:
    vol   = quote.average_volume or quote.volume or 0
    price = quote.last_price or 0.0
    oi    = quote.open_interest or 0

    if (
        vol   >= thresh["t1_min_volume"]
        and price >= thresh["t1_min_last_price"]
        and oi    >= thresh["t1_min_oi"]
    ):
        return 1

    if (
        vol   >= thresh["t2_min_volume"]
        and price >= thresh["t2_min_last_price"]
        and oi    >= thresh["t2_min_oi"]
    ):
        return 2

    return 3


async def assign_tiers(
    quotes: list["SymbolQuote"],
    thresholds: Optional[dict] = None,
) -> dict[str, int]:
    """
    Classify a list of SymbolQuotes into tiers.

    Args:
        quotes:     list of SymbolQuote objects with open_interest populated.
        thresholds: optional pre-built thresholds dict (bypasses DB fetch).
                    Used by tests and admin endpoints to inject custom values.

    Returns dict[symbol -> tier (1|2|3)].

    DB-error safety: if _fetch_thresholds() raises (DNS failure in CI), falls
    back to _SAFE_FALLBACK_THRESHOLDS so every symbol is assigned T3.
    """
    if not quotes:
        return {}

    if thresholds is not None:
        thresh = thresholds
    else:
        try:
            thresh = await _fetch_thresholds()
        except Exception as e:
            log.warning("[tier_engine] threshold fetch failed: %s — using safe fallback (all T3)", e)
            thresh = dict(_SAFE_FALLBACK_THRESHOLDS)

    result: dict[str, int] = {}
    t_counts = {1: 0, 2: 0, 3: 0}
    for q in quotes:
        t = _classify(q, thresh)
        result[q.symbol] = t
        t_counts[t] += 1

    log.info(
        "[tier_engine] Tier assignment complete: T1=%d T2=%d T3=%d (total=%d)",
        t_counts[1], t_counts[2], t_counts[3], len(quotes),
    )
    return result


def invalidate_cache() -> None:
    """Force next assign_tiers() call to re-fetch thresholds from DB."""
    global _cache_ts, _thresh_cache_ts
    _cache_ts        = 0.0
    _thresh_cache_ts = 0.0


# Alias used by tests and admin router
invalidate_thresholds_cache = invalidate_cache
