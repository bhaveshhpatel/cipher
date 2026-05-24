"""
routers/signal_config.py — Admin API for signal strategy configuration.

REARCH-005: Signal Config Store

Endpoints:
  GET  /admin/signal-config
       Returns all signal_config rows with full metadata.
       Each row: { key, value (typed), value_type, description,
                   updated_at, updated_by }
       Falls back to in-process snapshot if the DB is unreachable so
       operators always see the currently-effective values.

  PATCH /admin/signal-config/{key}
        Updates a single signal config key identified by URL path param.
        Body: { "value": <new_value> }
        Enforces:
          1. Key existence  — 422 if key not in SIGNAL_CONFIG_TYPES
          2. Type coercion  — 422 if value cannot be cast to declared type
          3. Ordering invariants (detailed below) — 422 on violation
        On success: returns { "key", "value" (typed), "previous" (typed) }
        Calls reload_signal_config() immediately after DB write so the
        Signal Engine picks up the change within milliseconds.

Ordering invariants enforced on PATCH:
  Premium pyramid (Dimension 1):
    noteworthy_premium < block_premium < golden_sweep_premium
    — violating this collapses alert-level tiers and would make BLOCK
      never fire when GOLDEN already fired.
  DTE window (Dimension 4):
    min_dte < max_dte
    — equal or inverted DTE window passes zero episodes.
  Tier multipliers (Dimension 1 — PBE extension):
    0.0 < mult <= 1.0  for all t2/t3 multipliers
    — mult > 1.0 would scale a tier above its base, inverting tier economics.
    — mult <= 0.0 would zero-out or negate all thresholds for a tier.
  Ask-side pct floor (Dimension 2):
    0.0 < ask_side_pct_floor <= 1.0
  Score / count floors:
    steamroom_score_floor >= 1
    min_trade_count >= 1

Auth:
  Both endpoints require:  Authorization: Bearer <SERVICE_ROLE_KEY>
  Returns 403 on missing or wrong key (same pattern as ingestion_config).

REARCH-005 (2026-05-11)
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from services.signal_config_store import (
    SIGNAL_CONFIG_TYPES,
    get_all_rows,
    get_signal_config,
    get_param,
    reload_signal_config,
    update_signal_config,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin", "signal-config"])

_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Auth dependency — identical to ingestion_config router
# ---------------------------------------------------------------------------

def verify_service_role(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    """
    Verify the caller presents the Supabase service-role key.
    Returns None on success; raises HTTP 403 on failure.
    Returns 403 (not 401) so the endpoint is not easily discoverable.
    """
    service_role_key = os.environ.get("SERVICE_ROLE_KEY", "").strip()
    if not service_role_key:
        log.error("SERVICE_ROLE_KEY not set — /admin/signal-config is locked")
        raise HTTPException(status_code=403, detail="Admin API not configured")

    token = credentials.credentials.strip() if credentials else ""
    if token != service_role_key:
        log.warning("signal_config: unauthorized access attempt")
        raise HTTPException(status_code=403, detail="Forbidden")


# ---------------------------------------------------------------------------
# Type-cast helper
# ---------------------------------------------------------------------------

def _cast(value: Any, value_type: str) -> Any:
    """Cast a raw value to the declared Python type. Raises ValueError on failure."""
    if value_type == "int":
        return int(float(str(value)))
    if value_type == "float":
        return float(str(value))
    if value_type == "bool":
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("true", "1", "yes", "on")
    return str(value)


def _to_db_str(value: Any, value_type: str) -> str:
    if value_type == "bool":
        return "true" if value else "false"
    return str(value)


# ---------------------------------------------------------------------------
# Ordering invariant checker
# ---------------------------------------------------------------------------

# Base keys for premium pyramid — the ordering check reads current snapshot
# values so partial updates are validated against the current live config.
_PREMIUM_BASE_KEYS = (
    "sig.noteworthy_premium",
    "sig.block_premium",
    "sig.golden_sweep_premium",
)

_TIER_MULT_KEYS_ALL = {
    "sig.golden_sweep_premium_t2_mult",
    "sig.golden_sweep_premium_t3_mult",
    "sig.block_premium_t2_mult",
    "sig.block_premium_t3_mult",
    "sig.noteworthy_premium_t2_mult",
    "sig.noteworthy_premium_t3_mult",
}


def _check_ordering_invariants(key: str, cast_value: Any) -> str | None:
    """
    Validate ordering invariants for the given key and its proposed new value.

    Reads the current live snapshot for related keys via the public
    get_param() API so the check reflects the full config state after the
    proposed change is applied.  get_param() already handles snapshot
    fallback to _DEFAULTS internally — this function must NOT reach into
    _snapshot or _DEFAULTS directly (SA-002: no internal-state reaches
    across module boundaries).

    Returns an error string if an invariant is violated, or None if valid.
    """
    def _current(k: str) -> Any:
        """Read the current snapshot value for key k (before this PATCH)."""
        # Use the public get_param() API rather than importing _snapshot or
        # _DEFAULTS directly.  get_param() is synchronous and returns the
        # currently-cached value with _DEFAULTS fallback — exactly what we
        # need for an invariant pre-check.  (SA-002)
        return get_param(k)

    # Simulate the snapshot after this proposed update.
    def _effective(k: str) -> Any:
        if k == key:
            return cast_value
        return _current(k)

    # 1. Premium pyramid: noteworthy < block < golden
    if key in _PREMIUM_BASE_KEYS:
        noteworthy = _effective("sig.noteworthy_premium")
        block      = _effective("sig.block_premium")
        golden     = _effective("sig.golden_sweep_premium")
        if not (noteworthy < block < golden):
            return (
                f"Premium ordering invariant violated after setting {key}={cast_value}: "
                f"noteworthy({noteworthy}) < block({block}) < golden({golden}) must hold. "
                f"Current values — noteworthy={_current('sig.noteworthy_premium')}, "
                f"block={_current('sig.block_premium')}, "
                f"golden={_current('sig.golden_sweep_premium')}."
            )

    # 2. DTE window: min_dte < max_dte
    if key in ("sig.min_dte", "sig.max_dte"):
        min_dte = _effective("sig.min_dte")
        max_dte = _effective("sig.max_dte")
        if min_dte >= max_dte:
            return (
                f"DTE window invariant violated: min_dte({min_dte}) must be "
                f"strictly less than max_dte({max_dte}). "
                f"Current values — min_dte={_current('sig.min_dte')}, "
                f"max_dte={_current('sig.max_dte')}."
            )

    # 3. Tier multipliers: 0.0 < mult <= 1.0
    if key in _TIER_MULT_KEYS_ALL:
        mult = cast_value
        if not (0.0 < mult <= 1.0):
            return (
                f"Tier multiplier invariant violated: {key}={mult} must be in (0.0, 1.0]. "
                f"A multiplier > 1.0 would scale a lower tier ABOVE its Tier-1 base, "
                f"inverting tier economics. A multiplier <= 0.0 would zero or negate "
                f"all effective thresholds for that tier."
            )

    # 4. Ask-side pct floor: 0.0 < floor <= 1.0
    if key == "sig.ask_side_pct_floor":
        if not (0.0 < cast_value <= 1.0):
            return (
                f"ask_side_pct_floor={cast_value} must be in (0.0, 1.0]. "
                f"A floor of 0.0 disables the ask-side gate entirely; use "
                f"sig.require_ask_side=false to intentionally disable it."
            )

    # 5. Minimum floors: steamroom_score_floor >= 1, min_trade_count >= 1
    if key == "sig.steamroom_score_floor":
        if cast_value < 1:
            return (
                f"steamroom_score_floor={cast_value} must be >= 1. "
                f"A floor of 0 would emit every episode regardless of score."
            )

    if key == "sig.min_trade_count":
        if cast_value < 1:
            return (
                f"min_trade_count={cast_value} must be >= 1. "
                f"A count of 0 would match single-trade micro-episodes."
            )

    return None


# ---------------------------------------------------------------------------
# Request body
# ---------------------------------------------------------------------------

class PatchSignalConfigRequest(BaseModel):
    value: Any


# ---------------------------------------------------------------------------
# GET /admin/signal-config
# ---------------------------------------------------------------------------

@router.get("/signal-config")
async def get_signal_config_endpoint(
    _auth: None = Depends(verify_service_role),
):
    """
    Return all signal_config rows with full metadata.

    Response shape:
      {
        "sig.golden_sweep_premium": {
          "value": 1000000.0,
          "value_type": "float",
          "description": "...",
          "updated_at": "2026-05-11T...",
          "updated_by": "admin"
        },
        ...
      }

    Falls back to in-process snapshot if the DB is unreachable — operators
    always see the currently-effective values even during a DB blip.
    Requires Authorization: Bearer <SERVICE_ROLE_KEY>.
    """
    rows = await get_all_rows()

    if rows:
        result: dict[str, Any] = {}
        for row in rows:
            k = row.get("key", "")
            if not k:
                continue
            vtype = SIGNAL_CONFIG_TYPES.get(k) or row.get("value_type", "float")
            try:
                typed_value = _cast(row.get("value", ""), vtype)
            except (ValueError, TypeError) as exc:
                log.warning("signal_config GET: malformed row %s: %s", k, exc)
                typed_value = row.get("value")
            result[k] = {
                "value":       typed_value,
                "value_type":  vtype,
                "description": row.get("description"),
                "updated_at":  row.get("updated_at"),
                "updated_by":  row.get("updated_by"),
            }
        return result

    # DB unreachable — serve in-process snapshot so operators can read
    # the currently-effective config even during a Supabase blip.
    log.warning("signal_config GET: DB unreachable — serving in-process snapshot")
    snapshot = await get_signal_config()
    fallback: dict[str, Any] = {}
    for k, v in snapshot.items():
        fallback[k] = {
            "value":       v,
            "value_type":  SIGNAL_CONFIG_TYPES.get(k, "float"),
            "description": None,
            "updated_at":  None,
            "updated_by":  None,
        }
    return fallback


# ---------------------------------------------------------------------------
# PATCH /admin/signal-config/{key}
# ---------------------------------------------------------------------------

@router.patch("/signal-config/{key:path}")
async def patch_signal_config(
    key: str,
    request: PatchSignalConfigRequest,
    _auth: None = Depends(verify_service_role),
):
    """
    Update a single signal config key.

    Path param: key — e.g. sig.golden_sweep_premium
    Body:        { "value": <new_value> }

    Returns: { "key": str, "value": <typed>, "previous": <typed> }

    Raises:
      422 — unknown key (not in SIGNAL_CONFIG_TYPES)
      422 — value cannot be cast to declared type
      422 — ordering invariant violation (premium pyramid, DTE window, etc.)
      403 — missing or wrong Authorization header
      500 — DB write failed

    On success the in-process snapshot is force-refreshed immediately so
    the Signal Engine (REARCH-006) picks up the change within milliseconds
    rather than waiting up to 30s for the TTL to expire.

    Requires Authorization: Bearer <SERVICE_ROLE_KEY>.
    """
    # 1. Key existence check
    if key not in SIGNAL_CONFIG_TYPES:
        known = sorted(SIGNAL_CONFIG_TYPES.keys())
        raise HTTPException(
            status_code=422,
            detail={
                "error":      "unknown_key",
                "message":    f"'{key}' is not a recognised signal config key.",
                "known_keys": known,
            },
        )

    value_type = SIGNAL_CONFIG_TYPES[key]

    # 2. Type coercion check
    try:
        cast_value = _cast(request.value, value_type)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error":      "type_error",
                "message":    f"Cannot cast value '{request.value}' to declared type '{value_type}' for key '{key}': {exc}",
                "key":        key,
                "value_type": value_type,
            },
        ) from exc

    # 3. Capture previous value before writing
    previous = get_param(key)

    # 4. Ordering invariant check
    invariant_error = _check_ordering_invariants(key, cast_value)
    if invariant_error:
        raise HTTPException(
            status_code=422,
            detail={
                "error":   "ordering_invariant",
                "message": invariant_error,
                "key":     key,
                "value":   cast_value,
            },
        )

    # 5. DB write via signal_config_store
    db_str = _to_db_str(cast_value, value_type)
    success = await update_signal_config(key, db_str, updated_by="admin")
    if not success:
        raise HTTPException(
            status_code=500,
            detail=f"DB write failed for '{key}'. Check backend logs for details.",
        )

    # 6. Force immediate snapshot refresh so Signal Engine picks up change
    await reload_signal_config()

    log.info(
        "signal_config PATCH: %s = %s (was %s) [type=%s]",
        key, cast_value, previous, value_type,
    )

    return {
        "key":      key,
        "value":    cast_value,
        "previous": previous,
    }
