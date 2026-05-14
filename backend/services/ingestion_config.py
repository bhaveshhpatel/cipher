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
  - validate_ingestion_config() is called at startup to warn on missing rows.

Keys stored (mirrors config.py / symbol_registry defaults):
  REGISTRY_MAX_DTE              int        90
  REGISTRY_ATM_RANGE_PCT        float      0.15
  REGISTRY_MIN_OI               int        1     (RC-3: raised from 0 — filters illiquid contracts)
  REGISTRY_REFRESH_MINS         int        30
  REGISTRY_EXPIRY_DAY_REFRESH_MINS int     15
  REGISTRY_BUILD_CONCURRENCY    int        50    (RC-3: was missing; added to defaults)
  UNIVERSE_MIN_PRICE            float      1.0
  UNIVERSE_MIN_VOLUME           int        500000
  EXCLUDED_SYMBOLS              json_list  ""    (comma-separated tickers; empty = use built-in list)

RC-3 FIX (2026-04-27):
  REGISTRY_BUILD_CONCURRENCY was not in _DEFAULTS and had no DB row.
  Code silently used hardcoded fallback 50 in symbol_registry.py with
  no observability. Fix:
    1. Added to _DEFAULTS with value 50.
    2. Added validate_ingestion_config() — called from main.py lifespan
       to warn on any expected key missing from the DB at startup.
    3. REGISTRY_MIN_OI default raised from 0 → 1 to filter zero-OI
       contracts that inflate registry size with illiquid noise.
  DB row must be inserted manually (SQL in PR description).

EXCLUDED_SYMBOLS (2026-05-14):
  New json_list key.  Empty string means "use the built-in _DEFAULT_EXCLUDED
  list in symbols_loader.py".  A non-empty comma-separated value completely
  replaces the built-in list for the next universe reload — no deploy needed.
  Admin page: PATCH /admin/ingestion-config {key: EXCLUDED_SYMBOLS, value: "SPY,QQQ"}
  DB row: INSERT INTO ingestion_config (key, value, value_type, description)
          VALUES ('EXCLUDED_SYMBOLS', '', 'json_list',
                  'Comma-separated tickers to exclude from the CBOE universe before any Tradier API calls. Empty = use built-in default list.');
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

# RC-3: REGISTRY_BUILD_CONCURRENCY added; REGISTRY_MIN_OI raised from 0 → 1
# 2026-05-14: EXCLUDED_SYMBOLS added (json_list; empty string = use built-in default)
_DEFAULTS: dict[str, Any] = {
    "REGISTRY_MAX_DTE":               90,
    "REGISTRY_ATM_RANGE_PCT":         0.15,
    "REGISTRY_MIN_OI":                1,      # was 0; raised to filter zero-OI illiquid contracts
    "REGISTRY_REFRESH_MINS":          30,
    "REGISTRY_EXPIRY_DAY_REFRESH_MINS": 15,
    "REGISTRY_BUILD_CONCURRENCY":     50,     # RC-3: was missing from _DEFAULTS and DB
    "UNIVERSE_MIN_PRICE":             1.0,
    "UNIVERSE_MIN_VOLUME":            500_000,
    "EXCLUDED_SYMBOLS":               "",     # empty = use _DEFAULT_EXCLUDED in symbols_loader.py
}

# All keys that MUST have a row in the ingestion_config table.
# validate_ingestion_config() warns at startup for any missing rows.
_EXPECTED_DB_KEYS: frozenset[str] = frozenset(_DEFAULTS.keys())


def _headers() -> dict:
    return {
        "apikey":        _SUPABASE_KEY or "",
        "Authorization": f"Bearer {_SUPABASE_KEY or ''}",
        "Content-Type":  "application/json",
    }


def _cast(value: str, value_type: str) -> Any:
    """
    Cast a raw DB string value to its typed Python representation.

    value_type options:
      int       → int (via float to handle "500000.0" from some DB drivers)
      float     → float
      json_list → str  (kept as comma-separated string; callers parse as needed)
      str / *   → str  (passthrough)

    json_list is stored and returned as a plain comma-separated string.
    symbols_loader._load_excluded_symbols() splits on "," itself so it can
    apply its own upper-casing and strip logic.  Returning a str here keeps
    the ingestion_config layer simple and avoids introducing a list type into
    the cache dict (which would break the int/float consumers).
    """
    try:
        if value_type == "int":
            return int(float(value))
        if value_type == "float":
            return float(value)
        # json_list and str both pass through unchanged
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


async def validate_ingestion_config() -> list[str]:
    """
    RC-3: Startup validator — checks that every key in _EXPECTED_DB_KEYS
    has a corresponding row in the ingestion_config DB table.

    Called from main.py lifespan on startup (non-blocking, non-fatal).
    Returns list of missing key names. Logs WARNING for each missing key
    so operators know which knobs are silently using hardcoded defaults.

    If Supabase is not configured, returns [] (nothing to validate).
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return []

    url = f"{_SUPABASE_URL}/rest/v1/{_TABLE}?select=key"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers())
        if resp.status_code != 200:
            log.warning(
                "[ingestion_config] validate: DB fetch failed HTTP %d — skipping validation",
                resp.status_code,
            )
            return []
        db_keys = {row["key"] for row in resp.json() if row.get("key")}
        missing = sorted(_EXPECTED_DB_KEYS - db_keys)
        if missing:
            for key in missing:
                log.warning(
                    "[ingestion_config] MISSING DB ROW: key='%s' default=%r "
                    "— using hardcoded default. Insert row into ingestion_config to enable "
                    "live tuning without restart.",
                    key, _DEFAULTS.get(key),
                )
        else:
            log.info("[ingestion_config] All %d expected config keys present in DB", len(_EXPECTED_DB_KEYS))
        return missing
    except Exception as e:
        log.warning("[ingestion_config] validate error (non-fatal): %s", e)
        return []
