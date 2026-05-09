"""
smart_signals.py — Composite signal endpoints.

Phase 4 changes:
  - /list: wired to live signal_history DB table (falls back to mock if DB empty/unavailable)
  - /composite/{ticker}: queries signal_history for most recent signal for ticker;
    falls back to deterministic mock if no DB record exists
  - /stream/stats: unchanged

B3-001: StatsOut(**get_stats()) replaced with explicit .get() extraction
  to avoid ValidationError when get_stats() returns extra keys (deduped,
  reconnects, mode, last_tick_at, last_reconnect_at, uptime_seconds, dedup
  counters). StatsOut shape kept at 5 fields to preserve test contract.

B3-002: SUPABASE_KEY replaced with SUPABASE_SERVICE_ROLE_KEY preference
  so DB queries bypass RLS and actually return rows.

Rearch-010 (2026-05-09): Removed influence_tier, flow_score, backtest_score,
  and volume_premium_factor from all DB select params, CompositeOut model,
  _row_to_composite(), and _mock_composite(). All four columns were dropped
  from signal_history in migration 024. composite_score + reasoning are the
  retained signal surfaces.
"""
from fastapi import APIRouter, Depends, Path, Query, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from core.auth import get_current_user, TokenData
from services.tradier_stream import get_stats
import os
import logging
import httpx
import random

log = logging.getLogger("routers.smart_signals")
router = APIRouter(prefix="/api/signals", tags=["signals"])

_SUPABASE_URL = os.environ.get("SUPABASE_URL")
# B3-002: prefer service role key (bypasses RLS) — same precedence as flow_store.py
_SUPABASE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")
    or os.environ.get("SUPABASE_KEY")
)

_VALID_DIRECTIONS = {"bullish", "bearish", "neutral"}
_DIR_TO_REC       = {"bullish": "BUY", "bearish": "SELL", "neutral": "HOLD"}


class CompositeOut(BaseModel):
    ticker:          str
    recommendation:  str
    composite_score: float
    reasoning:       str


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
    min_conviction: float = 0.0,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Query signal_history for the /list endpoint."""
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return [], 0
    url = f"{_SUPABASE_URL}/rest/v1/signal_history"
    params: dict = {
        "select": "ticker,recommendation,composite_score,reasoning",
        "order":  "created_at.desc",
        "limit":  str(limit),
        "offset": str(offset),
    }
    if recommendation:
        params["recommendation"] = f"eq.{recommendation}"
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
        "select": "ticker,recommendation,composite_score,reasoning",
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
    rng   = random.Random(hash(ticker) % 88888)
    score = round(rng.uniform(0.35, 0.92), 3)
    rec   = "BUY" if score >= 0.65 else ("SELL" if score <= 0.35 else "HOLD")
    return CompositeOut(
        ticker          = ticker,
        recommendation  = rec,
        composite_score = score,
        reasoning=(
            f"Composite analysis for {ticker}: "
            f"combined score {score:.0%} suggests {rec}."
        ),
    )


def _row_to_composite(r: dict) -> CompositeOut:
    return CompositeOut(
        ticker          = r["ticker"],
        recommendation  = r["recommendation"],
        composite_score = float(r["composite_score"]),
        reasoning       = r.get("reasoning") or "",
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

    rec_filter = _DIR_TO_REC.get(direction.lower()) if direction else None
    offset     = (page - 1) * page_size

    rows, total = await _fetch_from_db(
        recommendation = rec_filter,
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
    # B3-001: explicit .get() extraction — never passes unknown keys to StatsOut
    s = get_stats()
    return StatsResponse(stats=StatsOut(
        active_symbols = s.get("active_symbols", 0),
        ticks          = s.get("ticks", 0),
        classified     = s.get("classified", 0),
        signals        = s.get("signals", 0),
        errors         = s.get("errors", 0),
    ))
