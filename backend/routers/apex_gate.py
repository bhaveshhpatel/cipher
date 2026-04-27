"""
routers/apex_gate.py — Runtime toggle for the Apex aggression gate.

GET  /api/apex/gate-config  → returns current mode + stats snapshot
PATCH /api/apex/gate-config → sets hard_reject: true|false at runtime

Requires admin JWT (same auth dependency as other admin routes).
Changes take effect immediately — no restart needed.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import require_admin
from signals import signal_gate

router = APIRouter(prefix="/api/apex", tags=["apex-gate"])


class GateConfigResponse(BaseModel):
    hard_reject: bool
    source: str          # "override" | "env"
    max_aggression_penalty: float
    flat_aggression_penalty: float
    stats: dict


class GateConfigPatch(BaseModel):
    hard_reject: bool


def _source() -> str:
    return "override" if signal_gate._aggression_hard_reject_override is not None else "env"


@router.get("/gate-config", response_model=GateConfigResponse)
async def get_gate_config(_: str = Depends(require_admin)):
    """Return current aggression gate configuration and rejection stats."""
    return GateConfigResponse(
        hard_reject=signal_gate.get_aggression_hard_reject(),
        source=_source(),
        max_aggression_penalty=signal_gate.MAX_AGGRESSION_PENALTY,
        flat_aggression_penalty=signal_gate.FLAT_AGGRESSION_PENALTY,
        stats=signal_gate.stats(),
    )


@router.patch("/gate-config", response_model=GateConfigResponse)
async def patch_gate_config(
    body: GateConfigPatch,
    _: str = Depends(require_admin),
):
    """Toggle the aggression gate between hard-reject and soft-reject at runtime."""
    if not isinstance(body.hard_reject, bool):
        raise HTTPException(status_code=422, detail="hard_reject must be a boolean")
    signal_gate.set_aggression_hard_reject(body.hard_reject)
    return GateConfigResponse(
        hard_reject=signal_gate.get_aggression_hard_reject(),
        source=_source(),
        max_aggression_penalty=signal_gate.MAX_AGGRESSION_PENALTY,
        flat_aggression_penalty=signal_gate.FLAT_AGGRESSION_PENALTY,
        stats=signal_gate.stats(),
    )
