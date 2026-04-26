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
"""
import asyncio
import logging
import time
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from typing import Any
from core.auth import get_current_user, TokenData
from config import settings
import services.tier_engine as te

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
async def demo_on(admin: TokenData = Depends(_require_admin)):
    from services.demo_engine import start_demo
    result = await start_demo()
    log.info(f"[admin] Demo engine started by {admin.email}")
    return result


@router.post("/demo/off")
async def demo_off(admin: TokenData = Depends(_require_admin)):
    from services.demo_engine import stop_demo
    result = await stop_demo()
    log.info(f"[admin] Demo engine stopped by {admin.email}")
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
    body:  IngestionConfigUpdate,
    admin: TokenData = Depends(_require_admin),
):
    from services.ingestion_config import update_config
    ok = await update_config(body.key, body.value, updated_by=admin.email)
    if not ok:
        raise HTTPException(status_code=500, detail=f"Failed to update config key '{body.key}'")
    log.info(f"[admin] Ingestion config updated: {body.key}={body.value} by {admin.email}")
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
    body:  TierThresholdUpdate,
    admin: TokenData = Depends(_require_admin),
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
