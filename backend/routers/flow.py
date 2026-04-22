from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from typing import List
from core.auth import get_current_user, TokenData
import random
from datetime import date, timedelta

router = APIRouter(prefix="/api/flow", tags=["flow"])

class FlowEventOut(BaseModel):
    ticker:           str
    contract_type:    str
    strike:           float
    expiry:           str
    premium:          float
    trade_type:       str
    sentiment:        str
    influence_tier:   str
    conviction_score: float
    is_golden_sweep:  bool
    timestamp:        str

class FlowResponse(BaseModel):
    ticker: str
    events: List[FlowEventOut]

def _mock_events(ticker: str, n: int = 20) -> List[FlowEventOut]:
    rng    = random.Random(hash(ticker) % 99999)
    ctypes = ["CALL", "PUT"]
    tiers  = ["WHALE","INSTITUTIONAL","LARGE","RETAIL"]
    sents  = ["BULLISH","BEARISH","NEUTRAL"]
    ttypes = ["SWEEP","BLOCK","SPLIT","SINGLE"]
    today  = date.today()
    events = []
    for _ in range(n):
        ctype  = rng.choice(ctypes)
        strike = round(rng.uniform(50, 600) / 5) * 5
        dte    = rng.choice([7, 14, 30, 45, 60, 90])
        expiry = (today + timedelta(days=dte)).strftime("%Y-%m-%d")
        prem   = rng.randint(50_000, 5_000_000)
        tier   = rng.choices(tiers, weights=[5, 20, 35, 40])[0]
        events.append(FlowEventOut(
            ticker           = ticker,
            contract_type    = ctype,
            strike           = strike,
            expiry           = expiry,
            premium          = prem,
            trade_type       = rng.choice(ttypes),
            sentiment        = "BULLISH" if ctype=="CALL" else rng.choice(sents),
            influence_tier   = tier,
            conviction_score = round(rng.uniform(0.3, 0.95), 2),
            is_golden_sweep  = prem >= 500_000 and rng.random() < 0.15,
            timestamp        = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        ))
    return sorted(events, key=lambda e: e.premium, reverse=True)

@router.get("/scan", response_model=FlowResponse)
async def scan_flow(
    ticker: str = Query(..., min_length=1, max_length=10),
    limit:  int = Query(50, ge=1, le=200),
    _: TokenData = Depends(get_current_user),
):
    ticker = ticker.upper().strip()
    # In production: query Supabase for recent events or call Tradier chain
    events = _mock_events(ticker, min(limit, 50))
    return FlowResponse(ticker=ticker, events=events)
