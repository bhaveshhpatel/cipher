"""
services/tier_engine.py — Feature 4A: Dynamic Symbol Tiering

DB-error safety:
  _fetch_thresholds() catches ConnectError / network exceptions and returns
  _DEFAULT_THRESHOLDS so callers like symbol_registry.build() never crash.
  assign_tiers() also has its own fallback to _SAFE_FALLBACK_THRESHOLDS
  (all T1/T2 minimums = inf → every symbol lands T3) when explicitly
  desired (e.g. tier_engine tests that expect T3 on DB failure).
  The two layers are separate:
    - _fetch_thresholds: network-safe, returns defaults silently
    - assign_tiers: if _fetch_thresholds still raises, falls back to safe
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

# Used by assign_tiers() when _fetch_thresholds() itself raises despite the
# internal try/except (shouldn't happen in practice, but test coverage demands it).
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

# Required keys that must be present in any thresholds dict passed to _classify.
_REQUIRED_THRESH_KEYS = (
    "t1_min_volume", "t1_min_last_price", "t1_min_oi",
    "t2_min_volume", "t2_min_last_price", "t2_min_oi",
)

_cache: dict        = {}
_cache_ts: float    = 0.0
_thresh_cache_ts: float = 0.0


@dataclass
class _TierParams:
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
    """Fetch active tier_thresholds row from Supabase with TTL cache.
    Always returns a dict — network errors fall back to _DEFAULT_THRESHOLDS.
    """
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
    try:
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
                _cache           = result
                _cache_ts        = time.monotonic()
                _thresh_cache_ts = _cache_ts
                return dict(result)
        log.warning("[tier_engine] DB fetch failed: HTTP %d — using defaults", resp.status_code)
    except Exception as e:
        log.warning("[tier_engine] _fetch_thresholds network error: %s — using defaults", e)

    return dict(_DEFAULT_THRESHOLDS)


def _classify(quote: "SymbolQuote", thresh: dict) -> int:
    """Classify a single quote into tier 1/2/3.

    Uses .get() with _DEFAULT_THRESHOLDS fallbacks for every key so that a
    partial or empty thresh dict (e.g. from a test that patched
    _fetch_thresholds to return {} without restoring) never raises KeyError.
    """
    vol   = quote.average_volume or quote.volume or 0
    price = quote.last_price or 0.0
    oi    = quote.open_interest or 0

    if (
        vol   >= thresh.get("t1_min_volume",     _DEFAULT_THRESHOLDS["t1_min_volume"])
        and price >= thresh.get("t1_min_last_price", _DEFAULT_THRESHOLDS["t1_min_last_price"])
        and oi    >= thresh.get("t1_min_oi",         _DEFAULT_THRESHOLDS["t1_min_oi"])
    ):
        return 1

    if (
        vol   >= thresh.get("t2_min_volume",     _DEFAULT_THRESHOLDS["t2_min_volume"])
        and price >= thresh.get("t2_min_last_price", _DEFAULT_THRESHOLDS["t2_min_last_price"])
        and oi    >= thresh.get("t2_min_oi",         _DEFAULT_THRESHOLDS["t2_min_oi"])
    ):
        return 2

    return 3


async def assign_tiers(
    quotes: list["SymbolQuote"],
    thresholds: Optional[dict] = None,
) -> dict[str, int]:
    if not quotes:
        return {}

    if thresholds is not None:
        # Guard: if caller passed a partial/empty dict, fill in defaults.
        thresh = {**_DEFAULT_THRESHOLDS, **thresholds}
    else:
        try:
            thresh = await _fetch_thresholds()
        except Exception as e:
            log.warning("[tier_engine] threshold fetch failed: %s — using safe fallback (all T3)", e)
            thresh = dict(_SAFE_FALLBACK_THRESHOLDS)

    # Final safety net: ensure all required keys are present.
    for key in _REQUIRED_THRESH_KEYS:
        if key not in thresh:
            log.warning(
                "[tier_engine] thresh missing key '%s' — patching from defaults", key
            )
            thresh[key] = _DEFAULT_THRESHOLDS[key]

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
    global _cache_ts, _thresh_cache_ts
    _cache_ts        = 0.0
    _thresh_cache_ts = 0.0


invalidate_thresholds_cache = invalidate_cache
