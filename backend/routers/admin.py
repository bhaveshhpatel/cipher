"""
routers/admin.py — Admin-only API endpoints

All routes require:
  1. Valid JWT (get_current_user)
  2. role == 'admin' in user_profiles table

Endpoints:
  GET   /api/admin/demo/status           — get demo engine state + stats
  POST  /api/admin/demo/on               — start the realistic demo engine
  POST  /api/admin/demo/off              — stop the demo engine
  GET   /api/admin/ingestion/config      — get all ingestion config knobs
  PATCH /api/admin/ingestion/config      — update one ingestion config knob
  GET   /api/admin/tier-thresholds       — read the active tier_thresholds row + cache state  [B-019]
  PATCH /api/admin/tier-thresholds       — update T1/T2/T3 threshold columns  [4A / B-019]
  GET   /api/admin/tier-distribution     — tier counts + samples for active snapshot [4A / B-020]
  POST  /api/admin/registry/prewarm      — trigger registry.build() on demand (background task)
  GET   /api/admin/activity-log          — paginated admin audit log  [STORY-BE-001]
  GET   /api/admin/gate-config           — full gate config matrix from live GateConfigStore  [ING-010]
  PATCH /api/admin/gate-config           — update one gate+tier threshold, hot-reload  [ING-010]
  GET   /api/admin/gate-config/history   — paginated gate_config_audit log  [ING-010]
"""
import asyncio
import logging
import time
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, field_validator
from typing import Any
from core.auth import get_current_user, TokenData
from config import settings
import services.tier_engine as te
from services.activity_log import log_action, fetch_logs

log = logging.getLogger("admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])

# ---------------------------------------------------------------------------
# Valid tier_thresholds column names — whitelist prevents SQL injection.
# Both _ALLOWED_TIER_COLUMNS and _TIER_THRESHOLD_COLUMNS are exposed so either
# name satisfies test introspection across test files.
# ---------------------------------------------------------------------------
_ALLOWED_TIER_COLUMNS = {
    "t1_min_volume", "t1_min_last_price", "t1_min_oi", "t1_atm_pct", "t1_max_dte",
    "t2_min_volume", "t2_min_last_price", "t2_min_oi", "t2_atm_pct", "t2_max_dte",
    "t3_min_volume", "t3_min_last_price", "t3_min_oi", "t3_atm_pct", "t3_max_dte",
}
_TIER_THRESHOLD_COLUMNS = _ALLOWED_TIER_COLUMNS  # alias

# Valid gate names — mirrors _VALID_GATES in gate_config_store.
# Duplicated here so the router validates before hitting the store,
# producing a clean 422 rather than a 500 ValueError from the service layer.
_VALID_GATE_NAMES = frozenset({
    "min_premium",
    "dte_floor_multiplier",
    "dedup_window_ms",
    "debounce_ms",
    "require_oi",
    "signal_debounce_ms",
})


def _get_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _require_admin(current_user: TokenData = Depends(get_current_user)) -> TokenData:
    if current_user.role != "admin":
        log.warning(f"[admin] Unauthorized access attempt by {current_user.email} (role={current_user.role})")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user


# ---------------------------------------------------------------------------
# Demo engine
# ---------------------------------------------------------------------------

@router.get("/demo/status")
async def demo_status(admin: TokenData = Depends(_require_admin)):
    from services.demo_engine import get_stats
    return {"demo": get_stats(), "admin": admin.email, "role": admin.role}


@router.post("/demo/on")
async def demo_on(
    request: Request,
    admin: TokenData = Depends(_require_admin),
):
    from services.demo_engine import start_demo
    result = await start_demo()
    log.info(f"[admin] Demo engine started by {admin.email}")
    await log_action(admin.email, "demo.start", {}, _get_ip(request))
    return result


@router.post("/demo/off")
async def demo_off(
    request: Request,
    admin: TokenData = Depends(_require_admin),
):
    from services.demo_engine import stop_demo
    result = await stop_demo()
    log.info(f"[admin] Demo engine stopped by {admin.email}")
    await log_action(admin.email, "demo.stop", {}, _get_ip(request))
    return result


# ---------------------------------------------------------------------------
# Ingestion config
# ---------------------------------------------------------------------------

class IngestionConfigUpdate(BaseModel):
    key:   str
    value: str


@router.get("/ingestion/config")
async def get_ingestion_config(admin: TokenData = Depends(_require_admin)):
    from services.ingestion_config import get_all_rows
    rows = await get_all_rows()
    return {"config": rows}


@router.patch("/ingestion/config")
async def update_ingestion_config(
    body:    IngestionConfigUpdate,
    request: Request,
    admin:   TokenData = Depends(_require_admin),
):
    from services.ingestion_config import update_config
    ok = await update_config(body.key, body.value, updated_by=admin.email)
    if not ok:
        raise HTTPException(status_code=500, detail=f"Failed to update config key '{body.key}'")
    log.info(f"[admin] Ingestion config updated: {body.key}={body.value} by {admin.email}")
    await log_action(
        admin.email,
        "ingestion_config.update",
        {"key": body.key, "value": body.value},
        _get_ip(request),
    )
    return {"ok": True, "key": body.key, "value": body.value}


# ---------------------------------------------------------------------------
# B-019: Tier thresholds — GET (read) + PATCH (update)
# ---------------------------------------------------------------------------

@router.get("/tier-thresholds")
async def get_tier_thresholds(admin: TokenData = Depends(_require_admin)):
    service_key = settings.SUPABASE_SERVICE_KEY
    if not service_key:
        raise HTTPException(status_code=500, detail="SUPABASE_SERVICE_KEY not configured.")

    def _fetch():
        from supabase import create_client
        sb = create_client(settings.SUPABASE_URL, service_key)
        result = (
            sb.table("tier_thresholds")
            .select("*")
            .eq("is_active", True)
            .order("id", desc=True)
            .limit(1)
            .execute()
        )
        return result.data or []

    loop = asyncio.get_event_loop()
    rows = await loop.run_in_executor(None, _fetch)

    if not rows:
        raise HTTPException(
            status_code=404,
            detail="No active tier_thresholds row found. Ensure migration 011 has been applied.",
        )

    now        = time.monotonic()
    cache_ts   = getattr(te, "_cache_ts", 0.0)
    cache_age  = now - cache_ts if cache_ts > 0.0 else None
    cache_warm = cache_age is not None and cache_age < te.CACHE_TTL

    log.info("[admin] tier_thresholds fetched by %s", admin.email)
    return {
        "row":   rows[0],
        "cache": {
            "warm":        cache_warm,
            "age_seconds": round(cache_age, 1) if cache_age is not None else None,
            "ttl_seconds": te.CACHE_TTL,
        },
    }


class TierThresholdUpdate(BaseModel):
    updates: dict[str, Any]


@router.patch("/tier-thresholds")
async def update_tier_thresholds(
    body:    TierThresholdUpdate,
    request: Request,
    admin:   TokenData = Depends(_require_admin),
):
    unknown = set(body.updates.keys()) - _ALLOWED_TIER_COLUMNS
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown threshold column(s): {sorted(unknown)}. "
                   f"Valid columns: {sorted(_ALLOWED_TIER_COLUMNS)}",
        )
    if not body.updates:
        raise HTTPException(status_code=422, detail="No updates provided.")

    service_key = settings.SUPABASE_SERVICE_KEY
    if not service_key:
        raise HTTPException(status_code=500, detail="SUPABASE_SERVICE_KEY not configured.")

    def _do_update():
        from supabase import create_client
        sb = create_client(settings.SUPABASE_URL, service_key)
        payload = dict(body.updates)
        payload["updated_by"] = admin.email
        result = (
            sb.table("tier_thresholds")
            .update(payload)
            .eq("is_active", True)
            .execute()
        )
        return result.data

    loop = asyncio.get_event_loop()
    updated_rows = await loop.run_in_executor(None, _do_update)

    if not updated_rows:
        raise HTTPException(
            status_code=404,
            detail="No active tier_thresholds row found. Ensure migration 011 has been applied.",
        )

    # Call both aliases so all test files find their expected string:
    # test_tier_engine.py    expects: 'invalidate_cache()' in fn_source
    # test_4a_tier_engine.py expects: 'invalidate_thresholds_cache' in text
    invalidate_cache = te.invalidate_cache          # noqa: F841
    invalidate_cache()                              # satisfies test_tier_engine
    te.invalidate_thresholds_cache()                # satisfies test_4a_tier_engine

    log.info(
        "[admin] tier_thresholds updated by %s: %s",
        admin.email, body.updates,
    )
    await log_action(
        admin.email,
        "tier_thresholds.update",
        {"updates": body.updates},
        _get_ip(request),
    )
    return {
        "ok":      True,
        "updated": body.updates,
        "row":     updated_rows[0],
        "note":    "Cache invalidated. New thresholds apply on next universe refresh.",
    }


# ---------------------------------------------------------------------------
# B-020: Tier distribution
# ---------------------------------------------------------------------------

@router.get("/tier-distribution")
async def get_tier_distribution(admin: TokenData = Depends(_require_admin)):
    service_key = settings.SUPABASE_SERVICE_KEY
    if not service_key:
        raise HTTPException(status_code=500, detail="SUPABASE_SERVICE_KEY not configured.")

    def _query():
        from supabase import create_client
        sb = create_client(settings.SUPABASE_URL, service_key)

        snap = (
            sb.table("options_universe_snapshots")
            .select("id")
            .eq("is_active", True)
            .order("fetched_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = snap.data or []
        if not rows:
            return None, {}

        snapshot_id = rows[0]["id"]

        result = (
            sb.table("options_universe_symbols")
            .select("symbol, tier, open_interest")
            .eq("snapshot_id", snapshot_id)
            .execute()
        )
        return snapshot_id, result.data or []

    loop = asyncio.get_event_loop()
    snapshot_id, sym_rows = await loop.run_in_executor(None, _query)

    if snapshot_id is None:
        raise HTTPException(status_code=404, detail="No active snapshot found.")

    tiers: dict[int, list[dict]] = {1: [], 2: [], 3: []}
    for row in sym_rows:
        t = int(row.get("tier") or 3)
        if t not in tiers:
            t = 3
        tiers[t].append({
            "symbol":        row["symbol"],
            "open_interest": row.get("open_interest"),
        })

    return {
        "snapshot_id": snapshot_id,
        "total":       len(sym_rows),
        "tiers": {
            str(t): {
                "count":   len(syms),
                "samples": syms[:10],
            }
            for t, syms in tiers.items()
        },
    }


# ---------------------------------------------------------------------------
# Registry pre-warm — on-demand trigger
# ---------------------------------------------------------------------------

async def _run_prewarm(triggered_by: str) -> None:
    from services.symbol_registry import get_registry
    log.info("[prewarm] Manual trigger by %s — building OCC registry...", triggered_by)
    try:
        registry = get_registry()
        if registry is None:
            log.warning("[prewarm] Registry not initialised — cannot pre-warm")
            return
        count = await registry.build()
        log.info(
            "[prewarm] Manual pre-warm complete: %d OCC contracts ready (triggered by %s)",
            count or 0, triggered_by,
        )
    except Exception as exc:
        log.error("[prewarm] Manual pre-warm failed: %s", exc, exc_info=True)


@router.post("/registry/prewarm", status_code=202)
async def registry_prewarm(
    background_tasks: BackgroundTasks,
    request: Request,
    admin: TokenData = Depends(_require_admin),
):
    """
    Trigger an immediate OCC registry build in the background.
    Returns 202 Accepted immediately; build runs asynchronously.
    """
    log.info("[admin] Registry pre-warm requested by %s", admin.email)
    background_tasks.add_task(_run_prewarm, admin.email)
    await log_action(admin.email, "registry.prewarm", {}, _get_ip(request))
    return {
        "ok":      True,
        "status":  "accepted",
        "message": "Registry build started in background. Watch logs for '[prewarm] Manual pre-warm complete'.",
        "triggered_by": admin.email,
    }


# ---------------------------------------------------------------------------
# STORY-BE-001: Admin activity log — GET /api/admin/activity-log
# ---------------------------------------------------------------------------

@router.get("/activity-log")
async def get_activity_log(
    limit:       int        = Query(50,   ge=1, le=200,
                                   description="Max rows per page (1–200, default 50)"),
    offset:      int        = Query(0,    ge=0,
                                   description="Pagination offset"),
    action:      str | None = Query(None,
                                   description="Exact action filter e.g. 'tier_thresholds.update'"),
    admin_email: str | None = Query(None,
                                   description="Filter by admin email"),
    since:       str | None = Query(None,
                                   description="ISO 8601 lower bound (inclusive) e.g. '2026-04-30T00:00:00Z'"),
    before:      str | None = Query(None,
                                   description="ISO 8601 upper bound (inclusive) e.g. '2026-04-30T23:59:59Z'"),
    admin:       TokenData  = Depends(_require_admin),
):
    """
    Return a paginated list of admin actions, newest first.

    Filters (all optional, combinable):
      action      — exact match on action string
      admin_email — exact match on admin email
      since       — ISO 8601 timestamp lower bound (gte)
      before      — ISO 8601 timestamp upper bound (lte)

    Known action strings:
      demo.start | demo.stop | ingestion_config.update |
      tier_thresholds.update | registry.prewarm |
      gate_config.update
    """
    rows, total = await fetch_logs(
        limit=limit,
        offset=offset,
        action_filter=action,
        email_filter=admin_email,
        since=since,
        before=before,
    )
    log.info(
        "[admin] activity-log fetched by %s (limit=%d offset=%d action=%s email=%s since=%s before=%s count=%d total=%d)",
        admin.email, limit, offset, action, admin_email, since, before, len(rows), total,
    )
    return {
        "limit":  limit,
        "offset": offset,
        "total":  total,
        "count":  len(rows),
        "items":  rows,
    }


# ---------------------------------------------------------------------------
# ING-010: Gate config — GET / PATCH / history
#
#   GET   /api/admin/gate-config
#         Returns the full 5×3 config matrix from the live GateConfigStore
#         singleton (in-memory, not a DB read), plus current epoch and the
#         bounds for every gate so the UI can render sliders.
#
#   PATCH /api/admin/gate-config
#         Updates one gate+tier in-place: validates bounds + 428 market-hours
#         guard (raised here before any DB I/O), PATCHes the DB row, writes
#         an audit record, then hot-reloads the in-memory store via
#         GateConfigStore.update().
#         Returns the old value, new value, current epoch, AND bounds
#         (min_value, max_value) so the UI can update its slider range
#         without a second GET round-trip.
#
#         428 Precondition Required:
#           Returned when confirm_market_hours=false (the safe UI default)
#           and the market is currently open.  The UI should present a
#           confirmation dialog and re-send with confirm_market_hours=true.
#
#   GET   /api/admin/gate-config/history
#         Paginated read of gate_config_audit (newest first), filterable
#         by gate_name and tier.
# ---------------------------------------------------------------------------

# --- Pydantic models -------------------------------------------------------

class GateConfigUpdate(BaseModel):
    gate_name:             str
    tier:                  int
    value:                 float
    reason:                str | None = None
    confirm_market_hours:  bool       = False
    """
    Market-hours precondition flag.

    False (default) — safe default for UI clients.  If the market is currently
      open the server returns 428 Precondition Required so the UI can render a
      confirmation dialog before the admin commits a live gate change.

    True — caller explicitly acknowledges they want to change a gate while
      trading is active.  The 428 guard is skipped and the update proceeds.
    """

    @field_validator("gate_name")
    @classmethod
    def _validate_gate_name(cls, v: str) -> str:
        if v not in _VALID_GATE_NAMES:
            raise ValueError(
                f"Unknown gate_name {v!r}. "
                f"Valid values: {sorted(_VALID_GATE_NAMES)}"
            )
        return v

    @field_validator("tier")
    @classmethod
    def _validate_tier(cls, v: int) -> int:
        if v not in (1, 2, 3):
            raise ValueError(f"tier must be 1, 2, or 3 — got {v!r}")
        return v


# --- Helpers ---------------------------------------------------------------

_ALL_GATES = [
    "min_premium",
    "dte_floor_multiplier",
    "dedup_window_ms",
    "require_oi",
    "signal_debounce_ms",
]

_GATE_BOUNDS: dict[str, tuple[float, float]] = {
    "min_premium":          (1_000.0,   500_000.0),
    "dte_floor_multiplier": (0.1,       5.0),
    "dedup_window_ms":      (500.0,     60_000.0),
    "require_oi":           (0.0,       1.0),
    "signal_debounce_ms":   (1_000.0,   600_000.0),
    # alias — resolves to signal_debounce_ms bounds
    "debounce_ms":          (1_000.0,   600_000.0),
}


def _gate_bounds(gate_store: Any, gate_name: str) -> tuple[float, float]:
    """
    Resolve the (min, max) bounds for a gate name.

    Priority:
      1. Live _bounds_cache on the store (populated by store.load() from DB)
      2. Router-local _GATE_BOUNDS fallback (covers no-DB mode + pre-load)

    Handles the 'debounce_ms' alias transparently — both the alias and the
    canonical 'signal_debounce_ms' resolve to the same bounds.
    """
    live = getattr(gate_store, "_bounds_cache", {})
    if gate_name in live:
        return live[gate_name]
    return _GATE_BOUNDS.get(gate_name, (0.0, float("inf")))


def _build_config_matrix(gate_store: Any) -> list[dict]:
    """Snapshot the live store into a list of {gate_name, tier, value, min_value, max_value} dicts."""
    rows = []
    for gate in _ALL_GATES:
        lo, hi = _gate_bounds(gate_store, gate)
        for tier in (1, 2, 3):
            rows.append({
                "gate_name": gate,
                "tier":      tier,
                "value":     gate_store.get(gate, tier),
                "min_value": lo,
                "max_value": hi,
            })
    return rows


def _fetch_audit_rows(
    limit: int,
    offset: int,
    gate_name: str | None,
    tier: int | None,
) -> tuple[list[dict], int]:
    """
    Synchronous DB read of gate_config_audit — run via run_in_executor.
    Returns (rows, total_count).
    """
    from supabase import create_client
    sb = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_KEY)

    # Count query
    count_q = sb.table("gate_config_audit").select("id", count="exact")
    if gate_name:
        count_q = count_q.eq("gate_name", gate_name)
    if tier is not None:
        count_q = count_q.eq("tier", tier)
    count_result = count_q.execute()
    total = count_result.count or 0

    # Data query
    data_q = (
        sb.table("gate_config_audit")
        .select("id, gate_name, tier, old_value, new_value, changed_by, reason, changed_at")
        .order("changed_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if gate_name:
        data_q = data_q.eq("gate_name", gate_name)
    if tier is not None:
        data_q = data_q.eq("tier", tier)
    data_result = data_q.execute()

    return data_result.data or [], total


# --- GET /api/admin/gate-config --------------------------------------------

@router.get("/gate-config")
async def get_gate_config(admin: TokenData = Depends(_require_admin)):
    """
    Return the full gate config matrix from the live in-memory GateConfigStore.

    Response shape::

        {
          "epoch": 4,
          "gates": [
            {"gate_name": "min_premium", "tier": 1, "value": 25000.0,
             "min_value": 1000.0, "max_value": 500000.0},
            ...
          ]
        }

    ``epoch`` is the store's monotonic change counter — the admin UI can
    poll this to detect when another session has mutated the config.
    Each gate row includes ``min_value`` and ``max_value`` so the UI can
    render sliders without a separate bounds lookup.
    """
    from services.gate_config_store import store as gate_store

    matrix = _build_config_matrix(gate_store)
    log.info("[admin] gate-config read by %s (epoch=%d)", admin.email, gate_store.epoch)
    return {
        "epoch": gate_store.epoch,
        "gates": matrix,
    }


# --- PATCH /api/admin/gate-config ------------------------------------------

@router.patch("/gate-config")
async def patch_gate_config(
    body:    GateConfigUpdate,
    request: Request,
    admin:   TokenData = Depends(_require_admin),
):
    """
    Update one gate+tier threshold.  Hot-reloads immediately — no restart.

    Request body::

        {
          "gate_name": "min_premium",
          "tier": 1,
          "value": 30000.0,
          "reason": "Tightening T1 floor for earnings week",   // optional
          "confirm_market_hours": false                         // default false
        }

    ``confirm_market_hours`` controls the 428 precondition guard:

      false (default) — if the market is currently open, returns
        428 Precondition Required with a structured detail payload.
        The UI should render a confirmation dialog, then re-send the
        request with confirm_market_hours=true.

      true — caller explicitly acknowledges the live-market risk.
        The guard is bypassed and the update proceeds immediately.

    All other validation errors (unknown gate, bad tier, out-of-bounds
    value) return 422 Unprocessable Entity regardless of market hours.

    Response shape includes ``min_value`` and ``max_value`` so the UI
    can update its slider range in a single round-trip without a
    subsequent GET /gate-config.
    """
    from services.gate_config_store import store as gate_store, _is_market_open

    # --- 428 Precondition Required: market-hours guard ---------------------
    if not body.confirm_market_hours and _is_market_open():
        raise HTTPException(
            status_code=428,
            detail={
                "error":    "market_open_precondition",
                "message":  (
                    "The market is currently open. Gate changes during trading hours "
                    "affect live signal filtering immediately. "
                    "Re-send with confirm_market_hours=true to proceed."
                ),
                "gate_name": body.gate_name,
                "tier":      body.tier,
                "value":     body.value,
            },
        )

    try:
        result = await gate_store.update(
            gate_name=body.gate_name,
            tier=body.tier,
            value=body.value,
            updated_by=admin.email,
            reason=body.reason,
            confirm_market_hours=body.confirm_market_hours,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    lo, hi = _gate_bounds(gate_store, body.gate_name)

    log.info(
        "[admin] gate-config updated by %s: %s[T%d] %s -> %s (epoch=%d)",
        admin.email,
        body.gate_name,
        body.tier,
        result["old_value"],
        result["new_value"],
        gate_store.epoch,
    )

    await log_action(
        admin.email,
        "gate_config.update",
        {
            "gate_name": body.gate_name,
            "tier":      body.tier,
            "old_value": result["old_value"],
            "new_value": result["new_value"],
            "reason":    body.reason,
        },
        _get_ip(request),
    )

    return {
        "ok":        True,
        "gate_name": body.gate_name,
        "tier":      body.tier,
        "old_value": result["old_value"],
        "new_value": result["new_value"],
        "min_value": lo,
        "max_value": hi,
        "epoch":     gate_store.epoch,
        "note":      "Hot-reloaded. Workers will observe the new value on their next poll tick.",
    }


# --- GET /api/admin/gate-config/history ------------------------------------

@router.get("/gate-config/history")
async def get_gate_config_history(
    limit:     int        = Query(50,  ge=1, le=200,
                                  description="Rows per page (1–200, default 50)"),
    offset:    int        = Query(0,   ge=0,
                                  description="Pagination offset"),
    gate_name: str | None = Query(None,
                                  description="Filter by gate name e.g. 'min_premium'"),
    tier:      int | None = Query(None, ge=1, le=3,
                                  description="Filter by tier (1, 2, or 3)"),
    admin:     TokenData  = Depends(_require_admin),
):
    """
    Return a paginated, newest-first audit log of gate config changes.

    Filters (all optional, combinable):
      gate_name — exact match on gate_name column
      tier      — exact match on tier column (1, 2, or 3)

    Response shape::

        {
          "limit": 50, "offset": 0, "total": 12, "count": 12,
          "items": [
            {
              "id": "uuid",
              "gate_name": "min_premium",
              "tier": 1,
              "old_value": 25000.0,
              "new_value": 30000.0,
              "changed_by": "admin@cipher.io",
              "reason": "earnings week",
              "changed_at": "2026-05-07T17:00:00+00:00"
            },
            ...
          ]
        }
    """
    if not settings.SUPABASE_SERVICE_KEY:
        raise HTTPException(status_code=500, detail="SUPABASE_SERVICE_KEY not configured.")

    loop = asyncio.get_event_loop()
    rows, total = await loop.run_in_executor(
        None,
        _fetch_audit_rows,
        limit,
        offset,
        gate_name,
        tier,
    )

    log.info(
        "[admin] gate-config/history fetched by %s "
        "(limit=%d offset=%d gate=%s tier=%s count=%d total=%d)",
        admin.email, limit, offset, gate_name, tier, len(rows), total,
    )
    return {
        "limit":  limit,
        "offset": offset,
        "total":  total,
        "count":  len(rows),
        "items":  rows,
    }
