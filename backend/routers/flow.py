"""
flow.py — Live options flow scan endpoint.

Phase 4: Unmocked. Queries flow_events from Supabase with pagination
and optional ticker filter. Falls back to empty list if DB is unavailable.

Endpoints:
  GET /api/flow/scan?ticker=AAPL&limit=50&offset=0
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from core.auth import get_current_user, TokenData

import os
import logging
import httpx

log = logging.getLogger("routers.flow")
router = APIRouter(prefix="/api/flow", tags=["flow"])

_SUPABASE_URL = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY = os.environ.get("SUPABASE_KEY")  # anon key — read-only, no RLS issue for SELECT


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
    timestamp:        Optional[str] = None


class FlowResponse(BaseModel):
    ticker:  Optional[str]
    events:  List[FlowEventOut]
    total:   int
    limit:   int
    offset:  int


def _headers() -> dict:
    return {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Accept":        "application/json",
    }


async def _query_flow_events(
    ticker: Optional[str],
    limit:  int,
    offset: int,
) -> tuple[list[dict], int]:
    """
    Query flow_events from Supabase REST API.
    Returns (rows, total_count).
    Uses anon key — SELECT is safe under RLS (public read policy or service role not needed).
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        log.warning("[flow] SUPABASE_URL or SUPABASE_KEY not set — returning empty flow scan")
        return [], 0

    url = f"{_SUPABASE_URL}/rest/v1/flow_events"
    params: dict = {
        "select": "ticker,contract_type,strike,expiry,premium,trade_type,sentiment,influence_tier,conviction_score,is_golden_sweep,created_at",
        "order":  "created_at.desc",
        "limit":  str(limit),
        "offset": str(offset),
    }
    if ticker:
        params["ticker"] = f"eq.{ticker.upper()}"

    headers = {**_headers(), "Prefer": "count=exact"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers, params=params)

        if resp.status_code not in (200, 206):
            log.error(f"[flow] Supabase query failed: {resp.status_code} — {resp.text[:300]}")
            return [], 0

        rows = resp.json()
        # Parse Content-Range header for total count: "0-49/1234"
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
        log.error(f"[flow] Supabase query exception: {e}")
        return [], 0


@router.get("/scan", response_model=FlowResponse)
async def scan_flow(
    ticker: Optional[str] = Query(default=None, min_length=1, max_length=10, description="Filter by ticker symbol"),
    limit:  int           = Query(default=50,   ge=1, le=200,                description="Max rows to return"),
    offset: int           = Query(default=0,    ge=0,                        description="Pagination offset"),
    _: TokenData = Depends(get_current_user),
):
    """
    Return recent options flow events from the live Supabase flow_events table.
    Results are ordered by most recent first.
    """
    ticker_clean = ticker.upper().strip() if ticker else None

    rows, total = await _query_flow_events(ticker_clean, limit, offset)

    events = []
    for r in rows:
        try:
            events.append(FlowEventOut(
                ticker           = r.get("ticker", ""),
                contract_type    = r.get("contract_type", ""),
                strike           = float(r.get("strike") or 0),
                expiry           = r.get("expiry", ""),
                premium          = float(r.get("premium") or 0),
                trade_type       = r.get("trade_type", "UNKNOWN"),
                sentiment        = r.get("sentiment", "UNKNOWN"),
                influence_tier   = r.get("influence_tier", "UNKNOWN"),
                conviction_score = float(r.get("conviction_score") or 0),
                is_golden_sweep  = bool(r.get("is_golden_sweep", False)),
                timestamp        = r.get("created_at"),
            ))
        except Exception as e:
            log.warning(f"[flow] row parse error: {e} — row={r}")
            continue

    return FlowResponse(
        ticker = ticker_clean,
        events = events,
        total  = total,
        limit  = limit,
        offset = offset,
    )
