"""
services/ingestion_config.py — Runtime ingestion configuration store.

Reads and writes the `ingestion_config` Supabase table, providing a
live-config layer for the Layer 1 OCC Symbol Registry and universe
pipeline knobs without requiring a service restart.

Design:
  - get_config() returns a dict of all current key→typed-value pairs.
  - A 60-second in-process TTL cache prevents per-build DB hammering.
    (The registry rebuilds every 30 min by default — 60s TTL is fine.)
  - update_config() writes a single key back and invalidates the cache.
  - All DB access uses the service-role key (bypasses RLS).

Keys stored (mirrors config.py / symbol_registry defaults):
  REGISTRY_MAX_DTE              int    90
  REGISTRY_ATM_RANGE_PCT        float  0.15
  REGISTRY_MIN_OI               int    0
  REGISTRY_REFRESH_MINS         int    30
  REGISTRY_EXPIRY_DAY_REFRESH_MINS int 15
  REGISTRY_OI_DELTA_THRESHOLD   float  0.20   ← Issue 6 Part 2
  UNIVERSE_MIN_PRICE            float  1.0
  UNIVERSE_MIN_VOLUME           int    500000
"""
import logging
import os
import time
from typing import Any, Optional

import httpx

log = logging.getLogger("ingestion_config")

_SUPABASE_URL: Optional[str] = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY: Optional[str] = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")
)

_TABLE = "ingestion_config"
_CACHE_TTL = 60  # seconds

_cache: dict[str, Any] = {}
_cache_ts: float = 0.0

_DEFAULTS: dict[str, Any] = {
    "REGISTRY_MAX_DTE":                  90,
    "REGISTRY_ATM_RANGE_PCT":            0.15,
    "REGISTRY_MIN_OI":                   0,
    "REGISTRY_REFRESH_MINS":             30,
    "REGISTRY_EXPIRY_DAY_REFRESH_MINS":  15,
    "REGISTRY_OI_DELTA_THRESHOLD":       0.20,   # Issue 6 Part 2: skip chain re-fetch if OI delta < this
    "UNIVERSE_MIN_PRICE":                1.0,
    "UNIVERSE_MIN_VOLUME":               500_000,
}


def _headers() -> dict:
    return {
        "apikey":        _SUPABASE_KEY or "",
        "Authorization": f"Bearer {_SUPABASE_KEY or ''}",
        "Content-Type":  "application/json",
    }


def _cast(value: str, value_type: str) -> Any:
    try:
        if value_type == "int":
            return int(float(value))
        if value_type == "float":
            return float(value)
        return value
    except (TypeError, ValueError):
        return value


async def get_config(force_refresh: bool = False) -> dict[str, Any]:
    """
    Return all ingestion config values as a typed dict.
    Uses a 60-second TTL cache. Falls back to _DEFAULTS on DB error.
    """
    global _cache, _cache_ts

    if not force_refresh and _cache and (time.monotonic() - _cache_ts) < _CACHE_TTL:
        return dict(_cache)

    if not _SUPABASE_URL or not _SUPABASE_KEY:
        log.warning("[ingestion_config] Supabase not configured — using defaults")
        return dict(_DEFAULTS)

    url = f"{_SUPABASE_URL}/rest/v1/{_TABLE}?select=key,value,value_type"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers())
        if resp.status_code == 200:
            rows = resp.json()
            result = dict(_DEFAULTS)
            for row in rows:
                key        = row.get("key", "")
                raw_value  = row.get("value", "")
                value_type = row.get("value_type", "float")
                if key:
                    result[key] = _cast(raw_value, value_type)
            _cache    = result
            _cache_ts = time.monotonic()
            return dict(result)
        log.warning(f"[ingestion_config] DB fetch failed: HTTP {resp.status_code} — using defaults")
    except Exception as e:
        log.warning(f"[ingestion_config] DB fetch error: {e} — using defaults")

    return dict(_DEFAULTS)


async def get_all_rows() -> list[dict]:
    """Return full rows (including description, updated_at, updated_by) for admin UI."""
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return []
    url = (
        f"{_SUPABASE_URL}/rest/v1/{_TABLE}"
        "?select=key,value,value_type,description,updated_at,updated_by&order=id.asc"
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers())
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        log.error(f"[ingestion_config] get_all_rows error: {e}")
    return []


async def update_config(key: str, value: str, updated_by: str = "admin") -> bool:
    """
    Update a single config key in the DB and invalidate the in-process cache.
    Returns True on success.
    """
    global _cache_ts

    if not _SUPABASE_URL or not _SUPABASE_KEY:
        log.error("[ingestion_config] Cannot update — Supabase not configured")
        return False

    url = f"{_SUPABASE_URL}/rest/v1/{_TABLE}?key=eq.{key}"
    payload = {"value": str(value), "updated_by": updated_by}
    headers = {**_headers(), "Prefer": "return=minimal"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.patch(url, headers=headers, json=payload)
        if resp.status_code in (200, 204):
            _cache_ts = 0.0  # invalidate cache
            log.info(f"[ingestion_config] Updated {key}={value} by {updated_by}")
            return True
        log.error(f"[ingestion_config] Update failed for {key}: HTTP {resp.status_code} — {resp.text[:200]}")
    except Exception as e:
        log.error(f"[ingestion_config] Update error for {key}: {e}")
    return False
