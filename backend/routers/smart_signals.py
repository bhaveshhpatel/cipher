"""
smart_signals.py — Composite signal endpoints.

Phase 4 changes:
  - /list: wired to live signal_history DB table (falls back to mock if DB empty/unavailable)
  - /composite/{ticker}: queries signal_history for most recent signal for ticker;
    falls back to deterministic mock if no DB record exists
  - /stream/stats: unchanged
"""
from fastapi import APIRouter, Depends, Path, Query, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from core.auth import get_current_user, TokenData
from signals.backtest_validator import get_backtest_score
from services.tradier_stream import get_stats
import os
import logging
import httpx
import random

log = logging.getLogger("routers.smart_signals")
router = APIRouter(prefix="/api/signals", tags=["signals"])

_SUPABASE_URL = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

_VALID_DIRECTIONS = {"bullish", "bearish", "neutral"}
_VALID_TIERS      = {"whale", "institutional", "large", "retail"}
_DIR_TO_REC       = {"bullish": "BUY", "bearish": "SELL", "neutral": "HOLD"}
_TIER_TO_DB       = {
    "whale":         "WHALE",
    "institutional": "INSTITUTIONAL",
    "large":         "LARGE",
    "retail":        "RETAIL",
}


class CompositeOut(BaseModel):
    ticker:                 str
    recommendation:         str
    composite_score:        float
    flow_score:             float
    backtest_score:         float
    volume_premium_factor:  float = 0.5
    reasoning:              str


class SignalsListResponse(BaseModel):
    signals:    List[CompositeOut]
    page:       int
    page_size:  int
    total:      int
    source:     str  # 'live' | 'mock'


class StatsOut(BaseModel):
    active_symbols: int
    ticks:          int
    classified:     int
    signals:        int
    errors:         int


class StatsResponse(BaseModel):
    stats: StatsOut


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------
def _db_headers() -> dict:
    return {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Accept":        "application/json",
        "Prefer":        "count=exact",
    }


async def _fetch_from_db(
    recommendation: Optional[str] = None,
    influence_tier: Optional[str] = None,
    min_conviction: float = 0.0,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Query signal_history for the /list endpoint."""
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return [], 0
    url = f"{_SUPABASE_URL}/rest/v1/signal_history"
    params: dict = {
        "select": "ticker,recommendation,composite_score,flow_score,backtest_score,volume_premium_factor,reasoning",
        "order":  "created_at.desc",
        "limit":  str(limit),
        "offset": str(offset),
    }
    if recommendation:
        params["recommendation"] = f"eq.{recommendation}"
    if influence_tier:
        params["influence_tier"] = f"eq.{influence_tier}"
    if min_conviction > 0.0:
        params["composite_score"] = f"gte.{min_conviction}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers=_db_headers(), params=params)
        if resp.status_code not in (200, 206):
            log.warning(f"[smart_signals] DB list query failed: {resp.status_code}")
            return [], 0
        rows = resp.json()
        content_range = resp.headers.get("content-range", "")
        total = len(rows)
        if "/" in content_range:
            try:
                total = int(content_range.split("/")[1])
            except ValueError:
                pass
        return rows, total
    except Exception as e:
        log.warning(f"[smart_signals] DB list query exception: {e}")
        return [], 0


async def _fetch_ticker_from_db(ticker: str) -> Optional[dict]:
    """Fetch the most recent signal_history row for a given ticker."""
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return None
    url = f"{_SUPABASE_URL}/rest/v1/signal_history"
    params = {
        "select": "ticker,recommendation,composite_score,flow_score,backtest_score,volume_premium_factor,reasoning",
        "ticker": f"eq.{ticker}",
        "order":  "created_at.desc",
        "limit":  "1",
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url, headers=_db_headers(), params=params)
        if resp.status_code not in (200, 206):
            return None
        rows = resp.json()
        return rows[0] if rows else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Mock fallback (used when DB has no data yet)
# ---------------------------------------------------------------------------
def _mock_composite(ticker: str) -> CompositeOut:
    rng    = random.Random(hash(ticker) % 88888)
    flow_s = round(rng.uniform(0.35, 0.92), 3)
    bt_s   = get_backtest_score(
        ticker,
        rng.choice(["CALL", "PUT"]),
        rng.choice([14, 30, 60]),
        "INSTITUTIONAL",
    )
    vwp_f = round(rng.uniform(0.3, 0.85), 3)
    comp  = round(flow_s * 0.55 + bt_s * 0.35 + vwp_f * 0.10, 3)
    rec   = "BUY" if comp >= 0.65 else ("SELL" if comp <= 0.35 else "HOLD")
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


def _row_to_composite(r: dict) -> CompositeOut:
    return CompositeOut(
        ticker                = r["ticker"],
        recommendation        = r["recommendation"],
        composite_score       = float(r["composite_score"]),
        flow_score            = float(r["flow_score"]),
        backtest_score        = float(r["backtest_score"]),
        volume_premium_factor = float(r.get("volume_premium_factor") or 0.5),
        reasoning             = r.get("reasoning") or "",
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.get("/composite/{ticker}", response_model=CompositeOut)
async def get_composite(
    ticker: str = Path(..., min_length=1, max_length=10),
    _: TokenData = Depends(get_current_user),
):
    """
    Return composite signal for a ticker.
    Queries signal_history DB first; falls back to deterministic mock
    if no persisted signal exists for this ticker yet.
    """
    t = ticker.upper().strip()
    row = await _fetch_ticker_from_db(t)
    if row:
        log.debug(f"[smart_signals] /composite/{t}: served from DB")
        return _row_to_composite(row)
    log.debug(f"[smart_signals] /composite/{t}: no DB record, using mock")
    return _mock_composite(t)


@router.get("/list", response_model=SignalsListResponse)
async def list_signals(
    page:           int            = Query(default=1,   ge=1,          description="Page number (1-indexed)"),
    page_size:      int            = Query(default=20,  ge=1,  le=100, description="Results per page"),
    direction:      Optional[str]  = Query(default=None, description="bullish | bearish | neutral"),
    tier:           Optional[str]  = Query(default=None, description="whale | institutional | large | retail"),
    min_conviction: float          = Query(default=0.0, ge=0.0, le=1.0, description="Minimum composite_score"),
    _: TokenData = Depends(get_current_user),
):
    """
    Paginated list of composite signals.
    Phase 4: queries live signal_history table.
    Falls back to mock dataset if DB is empty or unavailable.
    """
    if direction and direction.lower() not in _VALID_DIRECTIONS:
        raise HTTPException(status_code=422, detail=f"direction must be one of: {sorted(_VALID_DIRECTIONS)}")
    if tier and tier.lower() not in _VALID_TIERS:
        raise HTTPException(status_code=422, detail=f"tier must be one of: {sorted(_VALID_TIERS)}")

    rec_filter  = _DIR_TO_REC.get(direction.lower()) if direction else None
    tier_filter = _TIER_TO_DB.get(tier.lower()) if tier else None
    offset      = (page - 1) * page_size

    rows, total = await _fetch_from_db(
        recommendation = rec_filter,
        influence_tier = tier_filter,
        min_conviction = min_conviction,
        limit          = page_size,
        offset         = offset,
    )

    if rows:
        signals = []
        for r in rows:
            try:
                signals.append(_row_to_composite(r))
            except Exception as e:
                log.warning(f"[smart_signals] row parse error: {e}")
                continue
        return SignalsListResponse(
            signals   = signals,
            page      = page,
            page_size = page_size,
            total     = total,
            source    = "live",
        )

    # Fallback: mock dataset when DB is empty / not yet seeded
    log.info("[smart_signals] /list: DB empty or unavailable — serving mock dataset")
    mock_tickers = [
        "AAPL", "TSLA", "NVDA", "SPY", "QQQ", "AMZN", "MSFT", "META",
        "GOOGL", "AMD", "NFLX", "COIN", "PLTR", "MSTR", "SOFI", "RIVN",
        "SMCI", "ARM", "UBER", "SHOP",
    ]
    all_signals = [_mock_composite(t) for t in mock_tickers]
    if rec_filter:
        all_signals = [s for s in all_signals if s.recommendation == rec_filter]
    if min_conviction > 0.0:
        all_signals = [s for s in all_signals if s.composite_score >= min_conviction]
    total_mock = len(all_signals)
    paged      = all_signals[offset: offset + page_size]
    return SignalsListResponse(
        signals   = paged,
        page      = page,
        page_size = page_size,
        total     = total_mock,
        source    = "mock",
    )


@router.get("/stream/stats", response_model=StatsResponse)
async def stream_stats(_: TokenData = Depends(get_current_user)):
    return StatsResponse(stats=StatsOut(**get_stats()))
