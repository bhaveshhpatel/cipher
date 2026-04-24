"""
routers/admin.py — Admin-only API endpoints

All routes require:
  1. Valid JWT (get_current_user)
  2. role == 'admin' in user_profiles table

Endpoints:
  GET  /api/admin/demo/status  — get demo engine state + stats
  POST /api/admin/demo/on      — start the realistic demo engine
  POST /api/admin/demo/off     — stop the demo engine immediately
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from core.auth import get_current_user, TokenData

log = logging.getLogger("admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _require_admin(current_user: TokenData = Depends(get_current_user)) -> TokenData:
    """Dependency — raises 403 if caller does not have role='admin'."""
    if current_user.role != "admin":
        log.warning(f"[admin] Unauthorized access attempt by {current_user.email} (role={current_user.role})")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user


@router.get("/demo/status")
async def demo_status(admin: TokenData = Depends(_require_admin)):
    """Get current demo engine state and stats."""
    from services.demo_engine import get_stats, is_running
    return {
        "demo":  get_stats(),
        "admin": admin.email,
        "role":  admin.role,
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
