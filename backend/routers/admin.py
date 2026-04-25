"""
routers/admin.py — Admin-only API endpoints

All routes require:
  1. Valid JWT (get_current_user)
  2. role == 'admin' in user_profiles table

Endpoints:
  GET  /api/admin/demo/status          — get demo engine state + stats
  POST /api/admin/demo/on              — start the realistic demo engine
  POST /api/admin/demo/off             — stop the demo engine
  GET  /api/admin/ingestion/config     — get all ingestion config knobs
  PATCH /api/admin/ingestion/config    — update one ingestion config knob
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from core.auth import get_current_user, TokenData

log = logging.getLogger("admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _require_admin(current_user: TokenData = Depends(get_current_user)) -> TokenData:
    if current_user.role != "admin":
        log.warning(f"[admin] Unauthorized access attempt by {current_user.email} (role={current_user.role})")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user


@router.get("/demo/status")
async def demo_status(admin: TokenData = Depends(_require_admin)):
    from services.demo_engine import get_stats, is_running
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


class IngestionConfigUpdate(BaseModel):
    key:   str
    value: str


@router.get("/ingestion/config")
async def get_ingestion_config(admin: TokenData = Depends(_require_admin)):
    """
    Return all ingestion config rows with metadata for the admin UI.
    """
    from services.ingestion_config import get_all_rows
    rows = await get_all_rows()
    return {"config": rows}


@router.patch("/ingestion/config")
async def update_ingestion_config(
    body:  IngestionConfigUpdate,
    admin: TokenData = Depends(_require_admin),
):
    """
    Update a single ingestion config key.
    Change takes effect on the next registry build cycle (<= REGISTRY_REFRESH_MINS).
    Cache is invalidated immediately so a manual rebuild picks up the new value.
    """
    from services.ingestion_config import update_config
    ok = await update_config(body.key, body.value, updated_by=admin.email)
    if not ok:
        raise HTTPException(status_code=500, detail=f"Failed to update config key '{body.key}'")
    log.info(f"[admin] Ingestion config updated: {body.key}={body.value} by {admin.email}")
    return {"ok": True, "key": body.key, "value": body.value}
