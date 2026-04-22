from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import List
from core.auth import get_current_user, TokenData
from simulation.ensemble_runner import run_ensemble

router = APIRouter(prefix="/api/simulation", tags=["simulation"])

class FlowEventIn(BaseModel):
    ticker:           str  = ""
    contract_type:    str  = "CALL"
    strike:           float = 0
    expiry:           str  = ""
    premium:          float = 0
    trade_type:       str  = ""
    sentiment:        str  = "NEUTRAL"
    influence_tier:   str  = "RETAIL"
    conviction_score: float = 0.5
    is_golden_sweep:  bool  = False
    timestamp:        str  = ""

class SimulationRequest(BaseModel):
    ticker:      str
    flow_events: List[FlowEventIn] = []
    n_agents:    int = 6
    n_runs:      int = 1

class AgentOut(BaseModel):
    role:      str
    direction: str
    reasoning: str

class SimulationResponse(BaseModel):
    ticker:     str
    direction:  str
    confidence: float
    bull_votes: int
    bear_votes: int
    hold_votes: int
    summary:    str
    agents:     List[AgentOut]

@router.post("/run", response_model=SimulationResponse)
async def run_simulation(
    body: SimulationRequest,
    _: TokenData = Depends(get_current_user),
):
    if body.n_agents < 1 or body.n_agents > 6:
        raise HTTPException(status_code=422, detail="n_agents must be 1-6")
    if body.n_runs < 1 or body.n_runs > 5:
        raise HTTPException(status_code=422, detail="n_runs must be 1-5")

    flow_dicts = [e.model_dump() for e in body.flow_events]
    result = await run_ensemble(
        ticker      = body.ticker.upper(),
        flow_events = flow_dicts,
        n_agents    = body.n_agents,
        n_runs      = body.n_runs,
    )
    return SimulationResponse(
        ticker     = result.ticker,
        direction  = result.direction,
        confidence = result.confidence,
        bull_votes = result.bull_votes,
        bear_votes = result.bear_votes,
        hold_votes = result.hold_votes,
        summary    = result.summary,
        agents     = [AgentOut(**a) for a in result.agents],
    )
