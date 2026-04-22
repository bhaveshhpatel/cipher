from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel
from core.auth import get_current_user, TokenData
from signals.composite_signal_engine import CompositeSignal
from signals.repetition_accumulator import RepetitionAccumulator, RepetitionEpisode
from signals.backtest_validator import get_backtest_score
from services.tradier_stream import get_stats
import random

router = APIRouter(prefix="/api/signals", tags=["signals"])

class CompositeOut(BaseModel):
    ticker:          str
    recommendation:  str
    composite_score: float
    flow_score:      float
    backtest_score:  float
    reasoning:       str

class StatsOut(BaseModel):
    active_symbols: int
    ticks:          int
    classified:     int
    signals:        int
    errors:         int

class StatsResponse(BaseModel):
    stats: StatsOut

def _mock_composite(ticker: str) -> CompositeOut:
    """Demo composite signal when no live data is available."""
    rng       = random.Random(hash(ticker) % 88888)
    flow_s    = round(rng.uniform(0.35, 0.92), 3)
    bt_s      = get_backtest_score(ticker, rng.choice(["CALL","PUT"]), rng.choice([14,30,60]), "INSTITUTIONAL")
    comp      = round(flow_s * 0.6 + bt_s * 0.4, 3)
    rec       = "BUY" if comp >= 0.65 else ("SELL" if comp <= 0.35 else "HOLD")
    return CompositeOut(
        ticker          = ticker,
        recommendation  = rec,
        composite_score = comp,
        flow_score      = flow_s,
        backtest_score  = bt_s,
        reasoning       = (
            f"Composite analysis for {ticker}: "
            f"flow score {flow_s:.0%}, backtest win-rate {bt_s:.0%}. "
            f"Combined score {comp:.0%} suggests {rec}."
        ),
    )

@router.get("/composite/{ticker}", response_model=CompositeOut)
async def get_composite(
    ticker: str = Path(..., min_length=1, max_length=10),
    _: TokenData = Depends(get_current_user),
):
    return _mock_composite(ticker.upper())

@router.get("/stream/stats", response_model=StatsResponse)
async def stream_stats(_: TokenData = Depends(get_current_user)):
    return StatsResponse(stats=StatsOut(**get_stats()))

# Alias so /api/stream/stats works (registered separately in main.py)
