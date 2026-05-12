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
  - get_effective_premium_threshold(alert_level, notional_tier) is the
    tier-aware threshold helper for REARCH-006.  It applies the PBE
    multiplier extension: base_threshold * tier_multiplier.
  - SIGNAL_CONFIG_TYPES is the authoritative type registry for all known
    signal config keys.  Used by the admin PATCH endpoint (REARCH-008) for
    422 validation and by _cast() for typed DB reads.
  - All DB access uses the service-role key (bypasses RLS).
  - validate_signal_config() is called at startup to warn on missing rows.

WSJ Steamroom 5-Dimension Knob Map:

  Dimension 1 — Premium Threshold (tier-aware via PBE multiplier extension):
    sig.golden_sweep_premium        float   1_000_000   Tier-1 base >= this → GOLDEN
    sig.block_premium               float   500_000     Tier-1 base >= this → BLOCK
    sig.noteworthy_premium          float   50_000      Tier-1 base >= this → NOTEWORTHY
    sig.golden_sweep_premium_t2_mult float  0.5         Tier-2 multiplier for GOLDEN base
    sig.golden_sweep_premium_t3_mult float  0.2         Tier-3 multiplier for GOLDEN base
    sig.block_premium_t2_mult       float   0.5         Tier-2 multiplier for BLOCK base
    sig.block_premium_t3_mult       float   0.2         Tier-3 multiplier for BLOCK base
    sig.noteworthy_premium_t2_mult  float   0.5         Tier-2 multiplier for NOTEWORTHY base
    sig.noteworthy_premium_t3_mult  float   0.2         Tier-3 multiplier for NOTEWORTHY base

  Effective thresholds at defaults:
    Alert Level   Tier-1      Tier-2      Tier-3
    GOLDEN        $1,000,000  $500,000    $200,000
    BLOCK         $500,000    $250,000    $100,000
    NOTEWORTHY    $50,000     $25,000     $10,000

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
from typing import Any, List, Optional

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
# Tier-multiplier key map — used by get_effective_premium_threshold()
#
# Maps (alert_level_key, notional_tier) -> multiplier_config_key.
# Tier-1 always uses a multiplier of 1.0 (no scaling — it IS the base).
# notional_tier values match flow_episodes.notional_tier from REARCH-004:
#   "tier1", "tier2", "tier3"
# ---------------------------------------------------------------------------
_TIER_MULT_KEYS: dict[tuple[str, str], Optional[str]] = {
    ("sig.golden_sweep_premium", "tier1"): None,   # 1.0 — base is Tier-1
    ("sig.golden_sweep_premium", "tier2"): "sig.golden_sweep_premium_t2_mult",
    ("sig.golden_sweep_premium", "tier3"): "sig.golden_sweep_premium_t3_mult",
    ("sig.block_premium",        "tier1"): None,
    ("sig.block_premium",        "tier2"): "sig.block_premium_t2_mult",
    ("sig.block_premium",        "tier3"): "sig.block_premium_t3_mult",
    ("sig.noteworthy_premium",   "tier1"): None,
    ("sig.noteworthy_premium",   "tier2"): "sig.noteworthy_premium_t2_mult",
    ("sig.noteworthy_premium",   "tier3"): "sig.noteworthy_premium_t3_mult",
}

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
    # Dimension 1 — Premium Threshold (base, Tier-1)
    "sig.golden_sweep_premium":          "float",
    "sig.block_premium":                 "float",
    "sig.noteworthy_premium":            "float",

    # Dimension 1 — Tier multipliers (PBE extension)
    "sig.golden_sweep_premium_t2_mult":  "float",
    "sig.golden_sweep_premium_t3_mult":  "float",
    "sig.block_premium_t2_mult":         "float",
    "sig.block_premium_t3_mult":         "float",
    "sig.noteworthy_premium_t2_mult":    "float",
    "sig.noteworthy_premium_t3_mult":    "float",

    # Dimension 2 — Ask-Side Execution
    "sig.require_ask_side":              "bool",
    "sig.ask_side_pct_floor":            "float",

    # Dimension 3 — Vol > OI
    "sig.require_vol_gt_oi":             "bool",

    # Dimension 4 — DTE Quality (signal-layer, above ingestion floors)
    "sig.min_dte":                       "int",
    "sig.max_dte":                       "int",

    # Dimension 5 — Repetition / Clustering
    "sig.min_trade_count":               "int",

    # Scoring gate
    "sig.steamroom_score_floor":         "int",
}

# _DEFAULTS: used as a last-resort fallback when Supabase is unreachable.
# All values reflect the WSJ Steamroom defaults documented in the roadmap.
# Must stay in sync with migration seeds 030 + 031.
_DEFAULTS: dict[str, Any] = {
    # Dimension 1 — base (Tier-1)
    "sig.golden_sweep_premium":         1_000_000.0,
    "sig.block_premium":                500_000.0,
    "sig.noteworthy_premium":           50_000.0,

    # Dimension 1 — tier multipliers
    "sig.golden_sweep_premium_t2_mult": 0.5,
    "sig.golden_sweep_premium_t3_mult": 0.2,
    "sig.block_premium_t2_mult":        0.5,
    "sig.block_premium_t3_mult":        0.2,
    "sig.noteworthy_premium_t2_mult":   0.5,
    "sig.noteworthy_premium_t3_mult":   0.2,

    # Dimension 2
    "sig.require_ask_side":             True,
    "sig.ask_side_pct_floor":           0.6,

    # Dimension 3
    "sig.require_vol_gt_oi":            True,

    # Dimension 4
    "sig.min_dte":                      5,
    "sig.max_dte":                      60,

    # Dimension 5
    "sig.min_trade_count":              2,

    # Scoring gate
    "sig.steamroom_score_floor":        3,
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


def _cast(key: str, raw: str) -> Any:
    """Cast a raw DB string value to the registered Python type for *key*."""
    type_str = SIGNAL_CONFIG_TYPES.get(key)
    if type_str == "float":
        return float(raw)
    if type_str == "int":
        return int(raw)
    if type_str == "bool":
        return raw.strip().lower() in ("true", "1", "yes", "on")
    return raw  # unknown key — return as-is


def _fetch_from_db() -> dict[str, Any]:
    """
    Fetch all rows from signal_config and return a typed dict.

    Returns an empty dict on any network or parse error so the caller
    can fall back to _DEFAULTS without crashing the signal pipeline.
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        log.warning("[signal_config_store] SUPABASE_URL/KEY not set — using defaults")
        return {}

    url = f"{_SUPABASE_URL}/rest/v1/{_TABLE}?select=key,value"
    try:
        resp = httpx.get(url, headers=_headers(), timeout=5.0)
        resp.raise_for_status()
        rows = resp.json()
        return {row["key"]: _cast(row["key"], str(row["value"])) for row in rows if "key" in row and "value" in row}
    except Exception as exc:
        log.error("[signal_config_store] DB fetch failed: %s", exc)
        return {}


async def _async_fetch_from_db() -> dict[str, Any]:
    """
    Async variant of _fetch_from_db() using httpx.AsyncClient.
    Used by get_all_rows() and async_reload_signal_config().
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        log.warning("[signal_config_store] SUPABASE_URL/KEY not set — using defaults")
        return {}

    url = f"{_SUPABASE_URL}/rest/v1/{_TABLE}?select=key,value"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=_headers(), timeout=5.0)
        resp.raise_for_status()
        rows = resp.json()
        return {row["key"]: _cast(row["key"], str(row["value"])) for row in rows if "key" in row and "value" in row}
    except Exception as exc:
        log.error("[signal_config_store] async DB fetch failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Public module-level API — sync (used by signal engine hot path)
# ---------------------------------------------------------------------------

def reload_signal_config() -> dict[str, Any]:
    """
    Force an immediate DB refresh and atomically swap the snapshot.

    Sync version — used by the signal engine hot path and startup.
    For the async router version see reload_signal_config() below which
    is re-exported as an awaitable via the async alias.

    Returns the new snapshot dict (a copy — callers must not mutate it).
    """
    global _snapshot, _snapshot_ts
    fresh = {**_DEFAULTS, **_fetch_from_db()}
    _snapshot = fresh
    _snapshot_ts = time.monotonic()
    log.info("[signal_config_store] config reloaded (%d keys)", len(fresh))
    return dict(fresh)


def _maybe_refresh() -> None:
    """Refresh the snapshot if the TTL has expired."""
    global _snapshot, _snapshot_ts
    if time.monotonic() - _snapshot_ts >= _CACHE_TTL:
        reload_signal_config()


def get_signal_config() -> dict[str, Any]:
    """
    Return a copy of the current config snapshot (sync).

    Triggers a DB refresh if the 30s TTL has expired.  Callers receive a
    fresh copy and must not hold references across evaluation cycles.
    """
    _maybe_refresh()
    return dict(_snapshot)


def get_param(key: str, default: Any = None) -> Any:
    """
    Hot-path accessor.  Returns the typed value for *key* from the current
    snapshot, or *default* if the key is absent.

    Triggers a TTL-based refresh if needed but never blocks on a DB call
    directly — the refresh happens in _maybe_refresh() which returns the
    existing snapshot on error.
    """
    _maybe_refresh()
    return _snapshot.get(key, default)


def get_effective_premium_threshold(alert_level_key: str, notional_tier: str) -> Optional[float]:
    """
    Return the tier-adjusted dollar threshold for *alert_level_key*.

    Applies the PBE multiplier extension:
        effective = base_threshold * tier_multiplier

    Parameters
    ----------
    alert_level_key : str
        One of the premium config keys without the "sig." prefix, e.g.
        "golden_sweep_premium", "block_premium", "noteworthy_premium".
        Accepts both bare keys ("golden_sweep_premium") and prefixed keys
        ("sig.golden_sweep_premium") for callers that use either convention.
    notional_tier : str
        One of "T1", "T2", "T3" (episode-level tier from REARCH-004).
        Case-insensitive; normalised to lowercase "tier1"/"tier2"/"tier3"
        for the internal lookup map.

    Returns
    -------
    float or None
        The effective threshold, or None if the base key is not in the
        snapshot (should not happen in production — validate_signal_config
        warns at startup).
    """
    _maybe_refresh()

    # Normalise key — accept both "golden_sweep_premium" and "sig.golden_sweep_premium"
    if not alert_level_key.startswith("sig."):
        alert_level_key = f"sig.{alert_level_key}"

    # Normalise tier — "T1"/"T2"/"T3" → "tier1"/"tier2"/"tier3"
    tier_norm = notional_tier.strip().upper()
    tier_map  = {"T1": "tier1", "T2": "tier2", "T3": "tier3",
                 "TIER1": "tier1", "TIER2": "tier2", "TIER3": "tier3"}
    tier_key  = tier_map.get(tier_norm, "tier1")

    base = _snapshot.get(alert_level_key)
    if base is None:
        log.warning("[signal_config_store] base key %r not in snapshot", alert_level_key)
        return None

    mult_key = _TIER_MULT_KEYS.get((alert_level_key, tier_key))
    if mult_key is None:
        # Tier-1 — no multiplier, base IS the threshold
        return float(base)

    mult = _snapshot.get(mult_key)
    if mult is None:
        log.warning("[signal_config_store] multiplier key %r not in snapshot", mult_key)
        return float(base)

    return float(base) * float(mult)


def validate_signal_config() -> None:
    """
    Warn at startup for any expected DB keys that are absent from the snapshot.

    Does not raise — missing keys fall back to _DEFAULTS so the pipeline
    keeps running.  Operators should treat these warnings as configuration
    drift alerts.
    """
    _maybe_refresh()
    missing = _EXPECTED_DB_KEYS - set(_snapshot.keys())
    if missing:
        log.warning(
            "[signal_config_store] %d expected key(s) missing from DB, using hardcoded defaults: %s",
            len(missing),
            sorted(missing),
        )
    else:
        log.info("[signal_config_store] all %d signal config keys present", len(_EXPECTED_DB_KEYS))


# ---------------------------------------------------------------------------
# Async DB API — used by routers/signal_config.py (FastAPI async endpoints)
#
# These functions use httpx.AsyncClient so they do not block the event loop.
# The sync hot path (signal engine, get_param) is unaffected.
# ---------------------------------------------------------------------------

async def get_all_rows() -> List[dict]:
    """
    Fetch all rows from the signal_config table with full metadata.

    Returns a list of dicts with keys: key, value, value_type,
    description, updated_at, updated_by.

    Used by GET /admin/signal-config to return rich row metadata that the
    snapshot dict alone does not carry (description, updated_at, updated_by).

    Returns an empty list on any network or parse error so the router can
    fall back to the in-process snapshot.
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        log.warning("[signal_config_store] get_all_rows: SUPABASE creds not set")
        return []

    url = (
        f"{_SUPABASE_URL}/rest/v1/{_TABLE}"
        "?select=key,value,value_type,description,updated_at,updated_by"
        "&order=key.asc"
    )
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=_headers(), timeout=5.0)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.error("[signal_config_store] get_all_rows failed: %s", exc)
        return []


async def update_signal_config(key: str, value: str, updated_by: str = "admin") -> bool:
    """
    Upsert a single signal config key in the DB.

    Parameters
    ----------
    key : str
        The config key to update (must exist in SIGNAL_CONFIG_TYPES).
    value : str
        The new value serialised as a string (type coercion is the
        caller's responsibility — see routers/signal_config.py).
    updated_by : str
        Identity tag written to the updated_by column (default: "admin").

    Returns
    -------
    bool
        True on success, False on any DB error.
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        log.error("[signal_config_store] update_signal_config: SUPABASE creds not set")
        return False

    url = f"{_SUPABASE_URL}/rest/v1/{_TABLE}?key=eq.{key}"
    payload = {"value": value, "updated_by": updated_by}
    # Use PATCH (update existing row) with upsert fallback via Prefer header.
    headers = {
        **_headers(),
        "Prefer": "resolution=merge-duplicates,return=representation",
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.patch(url, headers=headers, json=payload, timeout=5.0)
        if resp.status_code in (200, 201, 204):
            log.info("[signal_config_store] updated %s = %s", key, value)
            return True
        log.error(
            "[signal_config_store] update_signal_config %s status=%d body=%s",
            key, resp.status_code, resp.text[:200],
        )
        return False
    except Exception as exc:
        log.error("[signal_config_store] update_signal_config %s failed: %s", key, exc)
        return False


async def async_reload_signal_config() -> dict[str, Any]:
    """
    Async version of reload_signal_config().

    Forces an immediate DB refresh using httpx.AsyncClient so it can be
    safely awaited from FastAPI async route handlers without blocking the
    event loop.

    Re-exported as `reload_signal_config` at module level so the router
    can `await reload_signal_config()` without code changes.
    """
    global _snapshot, _snapshot_ts
    fresh = {**_DEFAULTS, **await _async_fetch_from_db()}
    _snapshot = fresh
    _snapshot_ts = time.monotonic()
    log.info("[signal_config_store] config reloaded async (%d keys)", len(fresh))
    return dict(fresh)


async def async_get_signal_config() -> dict[str, Any]:
    """
    Async version of get_signal_config().

    Returns the current snapshot (triggering an async refresh if the TTL
    has expired).  Re-exported as `get_signal_config` at module level so
    the router can `await get_signal_config()` without code changes.
    """
    if time.monotonic() - _snapshot_ts >= _CACHE_TTL:
        await async_reload_signal_config()
    return dict(_snapshot)


# ---------------------------------------------------------------------------
# Re-export async versions under the names the router expects.
#
# routers/signal_config.py does:
#   from services.signal_config_store import get_signal_config, reload_signal_config
# and then awaits both.  We replace the sync module-level names with the
# async coroutine functions here.  The sync hot path is preserved via the
# _sync aliases below for any code that needs the blocking version.
#
# IMPORTANT: This must appear AFTER both the sync and async definitions.
# ---------------------------------------------------------------------------

# Keep sync originals accessible under explicit names for signal engine + tests
get_signal_config_sync   = get_signal_config
reload_signal_config_sync = reload_signal_config

# Replace module-level names with async versions for router compatibility
get_signal_config    = async_get_signal_config    # type: ignore[assignment]
reload_signal_config = async_reload_signal_config  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# SignalConfigStore — class wrapper for dependency injection
#
# signal_engine.py (REARCH-006) uses constructor injection so tests can pass
# a pre-seeded stub without touching the DB or the 30s TTL cache.
#
# This class is a thin facade over the module-level functional API above.
# All state lives in the module-level _snapshot dict — the class itself is
# stateless and instances are interchangeable.
#
# NOTE: get_all() delegates to get_signal_config_sync() (not the async
# re-export) because SignalEngine.evaluate_episode() is a sync call.
# ---------------------------------------------------------------------------

class SignalConfigStore:
    """
    Thin class wrapper over the module-level signal config functions.

    Exists solely so SignalEngine can accept a config_store via constructor
    injection (enabling clean test stubs) while production code continues to
    use the module-level singleton pattern via get_signal_config_store().

    All methods delegate directly to the module-level functions — there is
    no per-instance state.
    """

    def get_all(self) -> dict[str, Any]:
        """Return a copy of the current config snapshot (sync, delegates to get_signal_config_sync())."""
        return get_signal_config_sync()

    def get_param(self, key: str, default: Any = None) -> Any:
        """Hot-path single-key accessor (delegates to module-level get_param())."""
        return get_param(key, default)

    def get_effective_premium_threshold(
        self, alert_level_key: str, notional_tier: str
    ) -> Optional[float]:
        """
        Tier-adjusted threshold accessor (delegates to module-level
        get_effective_premium_threshold()).
        """
        return get_effective_premium_threshold(alert_level_key, notional_tier)

    def reload(self) -> dict[str, Any]:
        """Force a sync DB refresh (delegates to reload_signal_config_sync())."""
        return reload_signal_config_sync()


# ---------------------------------------------------------------------------
# Module-level SignalConfigStore singleton
# ---------------------------------------------------------------------------

_store_singleton: Optional[SignalConfigStore] = None


def get_signal_config_store() -> SignalConfigStore:
    """
    Return the module-level SignalConfigStore singleton.

    Used by get_engine() in signal_engine.py to wire up the production
    config store without constructing a new instance per evaluation cycle.
    """
    global _store_singleton
    if _store_singleton is None:
        _store_singleton = SignalConfigStore()
        log.info("[signal_config_store] SignalConfigStore singleton initialised")
    return _store_singleton
