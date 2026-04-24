"""
history.py — Signal history endpoint.

Phase 4: Queries signal_history table from Supabase with full
pagination and filter support.

Endpoints:
  GET /api/signals/history
    ?ticker=AAPL
    &direction=bullish          # bullish | bearish | neutral
    &tier=whale                 # whale | institutional | large | retail
    &min_conviction=0.65
    &limit=50
    &offset=0
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from core.auth import get_current_user, TokenData

import os
import logging
import httpx

log = logging.getLogger("routers.history")
router = APIRouter(prefix="/api/signals", tags=["signals"])

_SUPABASE_URL = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY = os.environ.get("SUPABASE_KEY")  # anon key — SELECT only

_VALID_DIRECTIONS = {"bullish", "bearish", "neutral"}
_VALID_TIERS      = {"whale", "institutional", "large", "retail"}

# Map frontend direction names to DB recommendation values
_DIR_TO_REC = {"bullish": "BUY", "bearish": "SELL", "neutral": "HOLD"}
# Map tier names to DB influence_tier values
_TIER_TO_DB = {
    "whale":         "WHALE",
    "institutional": "INSTITUTIONAL",
    "large":         "LARGE",
    "retail":        "RETAIL",
}


class SignalHistoryItem(BaseModel):
    id:                     int
    ticker:                 str
    recommendation:         str
    composite_score:        float
    flow_score:             float
    backtest_score:         float
    volume_premium_factor:  float
    reasoning:              Optional[str]  = None
    contract_type:          Optional[str]  = None
    direction:              Optional[str]  = None
    influence_tier:         Optional[str]  = None
    total_premium:          Optional[float] = None
    trade_count:            Optional[int]  = None
    is_accelerating:        bool           = False
    signal_ts:              Optional[str]  = None
    created_at:             str


class HistoryResponse(BaseModel):
    signals: List[SignalHistoryItem]
    total:   int
    limit:   int
    offset:  int


def _headers() -> dict:
    return {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Accept":        "application/json",
        "Prefer":        "count=exact",
    }


async def _query_signal_history(
    ticker:         Optional[str],
    recommendation: Optional[str],
    influence_tier: Optional[str],
    min_conviction: float,
    limit:          int,
    offset:         int,
) -> tuple[list[dict], int]:
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        log.warning("[history] SUPABASE_URL or SUPABASE_KEY not set — returning empty history")
        return [], 0

    url = f"{_SUPABASE_URL}/rest/v1/signal_history"
    params: dict = {
        "select": "id,ticker,recommendation,composite_score,flow_score,backtest_score,"
                  "volume_premium_factor,reasoning,contract_type,direction,influence_tier,"
                  "total_premium,trade_count,is_accelerating,signal_ts,created_at",
        "order":  "created_at.desc",
        "limit":  str(limit),
        "offset": str(offset),
    }

    if ticker:
        params["ticker"] = f"eq.{ticker}"
    if recommendation:
        params["recommendation"] = f"eq.{recommendation}"
    if influence_tier:
        params["influence_tier"] = f"eq.{influence_tier}"
    if min_conviction > 0.0:
        params["composite_score"] = f"gte.{min_conviction}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=_headers(), params=params)

        if resp.status_code not in (200, 206):
            log.error(f"[history] Supabase query failed: {resp.status_code} — {resp.text[:300]}")
            return [], 0

        rows = resp.json()
        content_range = resp.headers.get("content-range", "")
        total = 0
        if "/" in content_range:
            try:
                total = int(content_range.split("/")[1])
            except ValueError:
                total = len(rows)
        else:
            total = len(rows)

        return rows, total

    except Exception as e:
        log.error(f"[history] Supabase query exception: {e}")
        return [], 0


@router.get("/history", response_model=HistoryResponse)
async def get_signal_history(
    ticker:         Optional[str]   = Query(default=None, min_length=1, max_length=10, description="Filter by ticker"),
    direction:      Optional[str]   = Query(default=None, description="bullish | bearish | neutral"),
    tier:           Optional[str]   = Query(default=None, description="whale | institutional | large | retail"),
    min_conviction: float           = Query(default=0.0,  ge=0.0, le=1.0, description="Minimum composite_score"),
    limit:          int             = Query(default=50,   ge=1,   le=200,  description="Max rows to return"),
    offset:         int             = Query(default=0,    ge=0,            description="Pagination offset"),
    _: TokenData = Depends(get_current_user),
):
    """
    Return persisted composite signals from signal_history table.
    Results ordered by most recent first.
    """
    if direction and direction.lower() not in _VALID_DIRECTIONS:
        raise HTTPException(status_code=422, detail=f"direction must be one of: {sorted(_VALID_DIRECTIONS)}")
    if tier and tier.lower() not in _VALID_TIERS:
        raise HTTPException(status_code=422, detail=f"tier must be one of: {sorted(_VALID_TIERS)}")

    ticker_clean  = ticker.upper().strip() if ticker else None
    rec_filter    = _DIR_TO_REC.get(direction.lower()) if direction else None
    tier_filter   = _TIER_TO_DB.get(tier.lower()) if tier else None

    rows, total = await _query_signal_history(
        ticker         = ticker_clean,
        recommendation = rec_filter,
        influence_tier = tier_filter,
        min_conviction = min_conviction,
        limit          = limit,
        offset         = offset,
    )

    signals = []
    for r in rows:
        try:
            signals.append(SignalHistoryItem(
                id                    = r["id"],
                ticker                = r["ticker"],
                recommendation        = r["recommendation"],
                composite_score       = float(r["composite_score"]),
                flow_score            = float(r["flow_score"]),
                backtest_score        = float(r["backtest_score"]),
                volume_premium_factor = float(r.get("volume_premium_factor") or 0.5),
                reasoning             = r.get("reasoning"),
                contract_type         = r.get("contract_type"),
                direction             = r.get("direction"),
                influence_tier        = r.get("influence_tier"),
                total_premium         = float(r["total_premium"]) if r.get("total_premium") is not None else None,
                trade_count           = r.get("trade_count"),
                is_accelerating       = bool(r.get("is_accelerating", False)),
                signal_ts             = r.get("signal_ts"),
                created_at            = r["created_at"],
            ))
        except Exception as e:
            log.warning(f"[history] row parse error: {e} — row={r}")
            continue

    return HistoryResponse(
        signals = signals,
        total   = total,
        limit   = limit,
        offset  = offset,
    )
