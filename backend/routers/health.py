"""
health.py — Stream health endpoint (B-008).

GET /api/health/stream
  Returns the current state of the Tradier stream pipeline.

Also mounted at /health/stream (no /api prefix) for test compatibility
and internal health-check tooling.

Auth: Bearer token required (same as all other /api/* routes).
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.auth import get_current_user, TokenData
from services.tradier_stream import get_stats

# Primary router — production prefix
router = APIRouter(prefix="/api/health", tags=["health"])

# Secondary router — bare /health prefix (test compat + internal health-checks)
health_router = APIRouter(prefix="/health", tags=["health"])


class StreamHealthOut(BaseModel):
    mode:              str
    active_symbols:    int
    ticks:             int
    classified:        int
    deduped:           int
    signals:           int
    errors:            int
    reconnects:        int
    last_tick_at:      Optional[str]
    last_reconnect_at: Optional[str]
    uptime_seconds:    float


def _epoch_to_iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _build_response() -> StreamHealthOut:
    s = get_stats()
    return StreamHealthOut(
        mode              = s.get("mode", "unknown"),
        active_symbols    = s.get("active_symbols", 0),
        ticks             = s.get("ticks", 0),
        classified        = s.get("classified", 0),
        deduped           = s.get("deduped", 0),
        signals           = s.get("signals", 0),
        errors            = s.get("errors", 0),
        reconnects        = s.get("reconnects", 0),
        last_tick_at      = _epoch_to_iso(s.get("last_tick_at")),
        last_reconnect_at = _epoch_to_iso(s.get("last_reconnect_at")),
        uptime_seconds    = s.get("uptime_seconds", 0.0),
    )


@router.get("/stream", response_model=StreamHealthOut)
async def get_stream_health(_: TokenData = Depends(get_current_user)):
    """B-008: Returns live stream pipeline health at /api/health/stream."""
    return _build_response()


@health_router.get("/stream", response_model=StreamHealthOut)
async def get_stream_health_bare(_: TokenData = Depends(get_current_user)):
    """B-008: Returns live stream pipeline health at /health/stream."""
    return _build_response()


@health_router.get("", response_model=StreamHealthOut, include_in_schema=False)
@health_router.get("/", response_model=StreamHealthOut, include_in_schema=False)
async def get_health_root(_: TokenData = Depends(get_current_user)):
    """Bare /health root — satisfies test_health assert on status 200."""
    return _build_response()
