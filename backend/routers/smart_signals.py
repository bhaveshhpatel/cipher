from fastapi import APIRouter, Depends, Path, Query, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from core.auth import get_current_user, TokenData
from signals.backtest_validator import get_backtest_score
from services.tradier_stream import get_stats
import random

router = APIRouter(prefix="/api/signals", tags=["signals"])


class CompositeOut(BaseModel):
    ticker:                 str
    recommendation:         str
    composite_score:        float
    flow_score:             float
    backtest_score:         float
    volume_premium_factor:  float = 0.5  # Phase 3: included in response
    reasoning:              str


class SignalsListResponse(BaseModel):
    signals:    List[CompositeOut]
    page:       int
    page_size:  int
    total:      int


class StatsOut(BaseModel):
    active_symbols: int
    ticks:          int
    classified:     int
    signals:        int
    errors:         int


class StatsResponse(BaseModel):
    stats: StatsOut


# Valid enum values for filter params
_VALID_DIRECTIONS = {"bullish", "bearish", "neutral"}
_VALID_TIERS      = {"whale", "institutional", "large", "retail"}


def _mock_composite(ticker: str) -> CompositeOut:
    """Demo composite signal when no live data is available."""
    rng    = random.Random(hash(ticker) % 88888)
    flow_s = round(rng.uniform(0.35, 0.92), 3)
    bt_s   = get_backtest_score(
        ticker,
        rng.choice(["CALL", "PUT"]),
        rng.choice([14, 30, 60]),
        "INSTITUTIONAL",
    )
    vwp_f  = round(rng.uniform(0.3, 0.85), 3)
    # Phase 3 weights
    comp   = round(flow_s * 0.55 + bt_s * 0.35 + vwp_f * 0.10, 3)
    rec    = "BUY" if comp >= 0.65 else ("SELL" if comp <= 0.35 else "HOLD")
    return CompositeOut(
        ticker                = ticker,
        recommendation        = rec,
        composite_score       = comp,
        flow_score            = flow_s,
        backtest_score        = bt_s,
        volume_premium_factor = vwp_f,
        reasoning=(
            f"Composite analysis for {ticker}: "
            f"flow score {flow_s:.0%}, backtest win-rate {bt_s:.0%}, "
            f"volume-premium factor {vwp_f:.0%}. "
            f"Combined score {comp:.0%} suggests {rec}."
        ),
    )


@router.get("/composite/{ticker}", response_model=CompositeOut)
async def get_composite(
    ticker: str = Path(..., min_length=1, max_length=10),
    _: TokenData = Depends(get_current_user),
):
    return _mock_composite(ticker.upper())


@router.get("/list", response_model=SignalsListResponse)
async def list_signals(
    # Pagination
    page:       int   = Query(default=1,  ge=1,           description="Page number (1-indexed)"),
    page_size:  int   = Query(default=20, ge=1,  le=100,  description="Results per page"),
    # Filters
    direction:      Optional[str]   = Query(default=None, description="bullish | bearish | neutral"),
    tier:           Optional[str]   = Query(default=None, description="whale | institutional | large | retail"),
    min_conviction: float           = Query(default=0.0,  ge=0.0, le=1.0, description="Minimum composite_score"),
    _: TokenData = Depends(get_current_user),
):
    # Validate enum filters
    if direction and direction.lower() not in _VALID_DIRECTIONS:
        raise HTTPException(status_code=422, detail=f"direction must be one of: {sorted(_VALID_DIRECTIONS)}")
    if tier and tier.lower() not in _VALID_TIERS:
        raise HTTPException(status_code=422, detail=f"tier must be one of: {sorted(_VALID_TIERS)}")

    # Mock dataset — replace with live accumulator query when wired
    mock_tickers = [
        "AAPL", "TSLA", "NVDA", "SPY", "QQQ", "AMZN", "MSFT", "META",
        "GOOGL", "AMD", "NFLX", "COIN", "PLTR", "MSTR", "SOFI", "RIVN",
        "SMCI", "ARM", "UBER", "SHOP",
    ]
    all_signals = [_mock_composite(t) for t in mock_tickers]

    # Apply filters
    filtered = all_signals
    if direction:
        dir_map = {"bullish": "BUY", "bearish": "SELL", "neutral": "HOLD"}
        target_rec = dir_map[direction.lower()]
        filtered = [s for s in filtered if s.recommendation == target_rec]
    if tier:
        # tier filter is metadata — mock uses fixed INSTITUTIONAL; pass through for now
        pass
    if min_conviction > 0.0:
        filtered = [s for s in filtered if s.composite_score >= min_conviction]

    # Pagination
    total  = len(filtered)
    start  = (page - 1) * page_size
    end    = start + page_size
    paged  = filtered[start:end]

    return SignalsListResponse(
        signals   = paged,
        page      = page,
        page_size = page_size,
        total     = total,
    )


@router.get("/stream/stats", response_model=StatsResponse)
async def stream_stats(_: TokenData = Depends(get_current_user)):
    return StatsResponse(stats=StatsOut(**get_stats()))

# Alias so /api/stream/stats works (registered separately in main.py)
