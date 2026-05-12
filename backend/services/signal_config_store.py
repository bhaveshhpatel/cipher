"""
services/signal_config_store.py — Runtime signal configuration store.

Reads the `signal_config` Supabase table and provides a live-config layer
for the Signal Engine (REARCH-006) without requiring a service restart.

Design:
  - get_signal_config() returns a frozen snapshot dict of all current
    key→typed-value pairs.  Callers never hold a reference to the internal
    cache — they always receive a fresh copy via the atomic snapshot.
  - A 30-second in-process TTL cache prevents per-episode DB hammering on
    the hot path.  The Signal Engine is called once per episode close; 30s
    is deliberately shorter than ingestion_config's 60s because signal
    thresholds drive alert emission and operators expect faster propagation
    after a live-tune.
  - reload_signal_config() forces an immediate DB refresh and atomic swap,
    called from the admin PATCH endpoint after a write succeeds.
  - get_param(key, default) is the hot-path accessor — reads from the
    current snapshot with a typed fallback, never hits the DB.
  - SIGNAL_CONFIG_TYPES is the authoritative type registry for all known
    signal config keys.  Used by the admin PATCH endpoint (REARCH-008) for
    422 validation and by _cast() for typed DB reads.
  - All DB access uses the service-role key (bypasses RLS).
  - validate_signal_config() is called at startup to warn on missing rows.

WSJ Steamroom 5-Dimension Knob Map:

  Dimension 1 — Premium Threshold (alert tier boundaries):
    sig.golden_sweep_premium    float   1_000_000   >= this → GOLDEN
    sig.block_premium           float   500_000     >= this → BLOCK
    sig.noteworthy_premium      float   50_000      >= this → NOTEWORTHY
    (below noteworthy_premium + above ingestion floor → WATCH)

  Dimension 2 — Ask-Side Execution:
    sig.require_ask_side        bool    True        gate: episode must be ask-side dominant
    sig.ask_side_pct_floor      float   0.6         gate: ask_side_pct >= this value

  Dimension 3 — Vol > OI:
    sig.require_vol_gt_oi       bool    True        gate: vol_oi_signal=True OR vol/oi > 1.0

  Dimension 4 — DTE Quality (signal-layer refinement above ingestion floors):
    sig.min_dte                 int     5           gate: dte >= this (ingestion floor is 1)
    sig.max_dte                 int     60          gate: dte <= this (ingestion ceiling is 90)

  Dimension 5 — Repetition / Clustering:
    sig.min_trade_count         int     2           gate: episode trade_count >= this

  Scoring:
    sig.steamroom_score_floor   int     3           emit only if steamroom_score >= this

Keys stored in `signal_config` table mirror the constants below.
validate_signal_config() warns at startup for any missing rows so operators
know which knobs are silently using hardcoded defaults.

Streaming boundary: this module is read-only from the signal engine's
perspective.  It never touches the streaming worker, Tradier client, OCC
parser, chain cache, or registry sync.
"""

import logging
import os
import time
from typing import Any, Optional

import httpx

log = logging.getLogger("signal_config_store")

_SUPABASE_URL: Optional[str] = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY: Optional[str] = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")
)

_TABLE = "signal_config"
_CACHE_TTL = 30  # seconds — shorter than ingestion_config (60s) for faster signal-knob propagation

# ---------------------------------------------------------------------------
# SIGNAL_CONFIG_TYPES — authoritative type registry
#
# Every key that REARCH-006 reads must be declared here.
# Used by:
#   - _cast() to coerce DB text values to the correct Python type
#   - validate_signal_config() to detect missing DB rows at startup
#   - Admin PATCH endpoint (REARCH-008) for 422 key-unknown validation
#   - Backtest engine (REARCH-014) for config_override deep-merge type safety
#
# Format:  "key_name": "type_string"
# Valid type_string values: "float", "int", "bool"
# ---------------------------------------------------------------------------
SIGNAL_CONFIG_TYPES: dict[str, str] = {
    # Dimension 1 — Premium Threshold
    "sig.golden_sweep_premium": "float",
    "sig.block_premium":        "float",
    "sig.noteworthy_premium":   "float",

    # Dimension 2 — Ask-Side Execution
    "sig.require_ask_side":     "bool",
    "sig.ask_side_pct_floor":   "float",

    # Dimension 3 — Vol > OI
    "sig.require_vol_gt_oi":    "bool",

    # Dimension 4 — DTE Quality (signal-layer, above ingestion floors)
    "sig.min_dte":              "int",
    "sig.max_dte":              "int",

    # Dimension 5 — Repetition / Clustering
    "sig.min_trade_count":      "int",

    # Scoring gate
    "sig.steamroom_score_floor": "int",
}

# _DEFAULTS: used as a last-resort fallback when Supabase is unreachable.
# All values reflect the WSJ Steamroom defaults documented in the roadmap.
_DEFAULTS: dict[str, Any] = {
    # Dimension 1
    "sig.golden_sweep_premium": 1_000_000.0,
    "sig.block_premium":        500_000.0,
    "sig.noteworthy_premium":   50_000.0,

    # Dimension 2
    "sig.require_ask_side":     True,
    "sig.ask_side_pct_floor":   0.6,

    # Dimension 3
    "sig.require_vol_gt_oi":    True,

    # Dimension 4
    "sig.min_dte":              5,
    "sig.max_dte":              60,

    # Dimension 5
    "sig.min_trade_count":      2,

    # Scoring gate
    "sig.steamroom_score_floor": 3,
}

# All keys that MUST have a row in the signal_config DB table.
_EXPECTED_DB_KEYS: frozenset[str] = frozenset(_DEFAULTS.keys())

# ---------------------------------------------------------------------------
# Internal snapshot state — atomic reference swap pattern
#
# _snapshot is replaced as a whole object on every successful reload so that
# callers on the hot path never observe a partially-updated config dict.
# GIL semantics in CPython make this assignment atomic for dict references;
# no lock is needed for reads.
# ---------------------------------------------------------------------------
_snapshot: dict[str, Any] = dict(_DEFAULTS)
_snapshot_ts: float = 0.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    return {
        "apikey":        _SUPABASE_KEY or "",
        "Authorization": f"Bearer {_SUPABASE_KEY or ''}",
        "Content-Type":  "application/json",
    }


def _cast(value: str, value_type: str) -> Any:
    """Coerce a raw DB text value to the Python type declared in SIGNAL_CONFIG_TYPES."""
    try:
        if value_type == "int":
            return int(float(value))
        if value_type == "float":
            return float(value)
        if value_type == "bool":
            return str(value).lower() in ("1", "true", "yes", "on")
        return value
    except (TypeError, ValueError):
        return value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_signal_config(force_refresh: bool = False) -> dict[str, Any]:
    """
    Return a copy of the current signal config snapshot.

    Uses a 30-second TTL cache.  Falls back to _DEFAULTS on DB error so the
    Signal Engine always has a valid config to run against.

    Callers on the hot path should prefer get_param() to avoid the overhead
    of copying the full dict on every episode evaluation.
    """
    global _snapshot, _snapshot_ts

    if not force_refresh and _snapshot and (time.monotonic() - _snapshot_ts) < _CACHE_TTL:
        return dict(_snapshot)

    refreshed = await _fetch_from_db()
    if refreshed is not None:
        _snapshot = refreshed          # atomic reference swap
        _snapshot_ts = time.monotonic()
        return dict(_snapshot)

    # DB unreachable — return current snapshot (may be stale) or defaults
    if _snapshot:
        log.warning("[signal_config_store] DB unreachable — serving stale snapshot (age=%.0fs)",
                    time.monotonic() - _snapshot_ts)
        return dict(_snapshot)

    log.warning("[signal_config_store] DB unreachable and no snapshot — using hardcoded defaults")
    return dict(_DEFAULTS)


async def reload_signal_config() -> dict[str, Any]:
    """
    Force an immediate DB refresh and atomic snapshot swap.

    Called from the admin PATCH endpoint (REARCH-008) immediately after a
    successful write so the Signal Engine picks up the change within milliseconds
    rather than waiting up to 30s for the TTL to expire.

    Returns the freshly-loaded config dict (or the current snapshot on DB error).
    """
    return await get_signal_config(force_refresh=True)


def get_param(key: str, default: Any = None) -> Any:
    """
    Hot-path accessor — returns a single value from the current snapshot
    without copying the full dict or touching the DB.

    This is the function REARCH-006 calls on every episode evaluation.
    Thread-safe under CPython GIL: dict reads are atomic for key lookups.

    Args:
        key:     A key from SIGNAL_CONFIG_TYPES (e.g. "sig.min_dte").
        default: Returned if key is absent from the current snapshot.
                 Callers should pass a typed literal so downstream code
                 never receives None from a missing config row.

    Returns:
        The typed value from the current snapshot, or `default` if missing.
    """
    return _snapshot.get(key, default)


async def get_all_rows() -> list[dict]:
    """
    Return full rows (key, value, value_type, description, updated_at, updated_by)
    for the admin Signal Strategy panel (REARCH-008).
    """
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
        log.error("[signal_config_store] get_all_rows HTTP %d: %s",
                  resp.status_code, resp.text[:200])
    except Exception as exc:
        log.error("[signal_config_store] get_all_rows error: %s", exc)
    return []


async def update_signal_config(key: str, value: str, updated_by: str = "admin") -> bool:
    """
    Update a single config key in the DB and immediately reload the snapshot.

    Args:
        key:        Must be a key present in SIGNAL_CONFIG_TYPES; caller is
                    responsible for 422-validating unknown keys before calling.
        value:      Raw string value (DB stores everything as text).
        updated_by: Audit trail field; defaults to "admin".

    Returns:
        True on success, False on any DB or network error.
    """
    global _snapshot_ts

    if not _SUPABASE_URL or not _SUPABASE_KEY:
        log.error("[signal_config_store] Cannot update — Supabase not configured")
        return False

    url = f"{_SUPABASE_URL}/rest/v1/{_TABLE}?key=eq.{key}"
    payload = {"value": str(value), "updated_by": updated_by}
    headers = {**_headers(), "Prefer": "return=minimal"}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.patch(url, headers=headers, json=payload)
        if resp.status_code in (200, 204):
            _snapshot_ts = 0.0   # invalidate TTL so next get_signal_config() forces reload
            log.info("[signal_config_store] Updated %s=%s by %s", key, value, updated_by)
            return True
        log.error("[signal_config_store] Update failed for %s: HTTP %d — %s",
                  key, resp.status_code, resp.text[:200])
    except Exception as exc:
        log.error("[signal_config_store] Update error for %s: %s", key, exc)
    return False


async def validate_signal_config() -> list[str]:
    """
    Startup validator — checks that every key in _EXPECTED_DB_KEYS has a
    corresponding row in the signal_config DB table.

    Called from main.py lifespan on startup (non-blocking, non-fatal).
    Returns a list of missing key names.  Logs WARNING for each missing key
    so operators know which Signal Engine knobs are silently using defaults.

    If Supabase is not configured (e.g. local dev without env vars), returns
    [] immediately — nothing to validate.
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return []

    url = f"{_SUPABASE_URL}/rest/v1/{_TABLE}?select=key"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers())
        if resp.status_code != 200:
            log.warning(
                "[signal_config_store] validate: DB fetch failed HTTP %d — skipping validation",
                resp.status_code,
            )
            return []
        db_keys = {row["key"] for row in resp.json() if row.get("key")}
        missing = sorted(_EXPECTED_DB_KEYS - db_keys)
        if missing:
            for key in missing:
                log.warning(
                    "[signal_config_store] MISSING DB ROW: key='%s' default=%r "
                    "— using hardcoded default.  Insert row into signal_config to enable "
                    "live tuning without restart.",
                    key,
                    _DEFAULTS.get(key),
                )
        else:
            log.info(
                "[signal_config_store] All %d expected signal config keys present in DB",
                len(_EXPECTED_DB_KEYS),
            )
        return missing
    except Exception as exc:
        log.warning("[signal_config_store] validate error (non-fatal): %s", exc)
        return []


# ---------------------------------------------------------------------------
# Internal DB fetch — separated so reload_signal_config() can call it cleanly
# ---------------------------------------------------------------------------

async def _fetch_from_db() -> Optional[dict[str, Any]]:
    """
    Fetch all rows from `signal_config`, cast values using SIGNAL_CONFIG_TYPES,
    and return a new snapshot dict seeded with _DEFAULTS.

    Returns None if Supabase is not configured or the request fails, so callers
    can distinguish "no data" from an empty config.
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        log.warning("[signal_config_store] Supabase not configured — using defaults")
        return None

    url = f"{_SUPABASE_URL}/rest/v1/{_TABLE}?select=key,value,value_type"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=_headers())
        if resp.status_code == 200:
            rows = resp.json()
            result = dict(_DEFAULTS)   # seed with defaults so missing rows never produce None
            for row in rows:
                key        = row.get("key", "")
                raw_value  = row.get("value", "")
                # Prefer SIGNAL_CONFIG_TYPES for the canonical type; fall back to
                # the row's own value_type column so future ad-hoc keys still work.
                value_type = SIGNAL_CONFIG_TYPES.get(key) or row.get("value_type", "float")
                if key:
                    result[key] = _cast(raw_value, value_type)
            log.debug("[signal_config_store] Loaded %d signal config keys from DB", len(rows))
            return result
        log.warning(
            "[signal_config_store] DB fetch failed: HTTP %d — keeping current snapshot",
            resp.status_code,
        )
    except Exception as exc:
        log.warning("[signal_config_store] DB fetch error: %s — keeping current snapshot", exc)
    return None
