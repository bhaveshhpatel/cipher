"""
routers/admin.py — Admin-only API endpoints

Provides runtime control endpoints for platform administration.
All routes require:
  1. Valid JWT (get_current_user)
  2. Email matches ADMIN_EMAIL environment variable

Endpoints:
  GET  /api/admin/demo/status  — get demo engine state + stats
  POST /api/admin/demo/on      — start the realistic demo engine
  POST /api/admin/demo/off     — stop the demo engine immediately

Admin check:
  Set ADMIN_EMAIL=bhaveshhpatel@yahoo.com in Railway env vars.
  Any other logged-in user gets 403 Forbidden.
"""
import os
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from core.auth import get_current_user, TokenData

log = logging.getLogger("admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])

_ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "bhaveshhpatel@yahoo.com")


def _require_admin(current_user: TokenData = Depends(get_current_user)) -> TokenData:
    """Dependency — raises 403 if caller is not the admin."""
    if current_user.email != _ADMIN_EMAIL:
        log.warning(f"[admin] Unauthorized access attempt by {current_user.email}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user


@router.get("/demo/status")
async def demo_status(admin: TokenData = Depends(_require_admin)):
    """Get current demo engine state and stats."""
    from services.demo_engine import get_stats, is_running
    from services.tradier_stream import get_stats as stream_stats
    return {
        "demo":   get_stats(),
        "stream": stream_stats(),
        "admin":  admin.email,
    }


@router.post("/demo/on")
async def demo_on(admin: TokenData = Depends(_require_admin)):
    """Start the realistic demo engine immediately."""
    from services.demo_engine import start_demo
    result = await start_demo()
    log.info(f"[admin] Demo engine started by {admin.email}")
    return result


@router.post("/demo/off")
async def demo_off(admin: TokenData = Depends(_require_admin)):
    """Stop the demo engine immediately."""
    from services.demo_engine import stop_demo
    result = await stop_demo()
    log.info(f"[admin] Demo engine stopped by {admin.email}")
    return result
