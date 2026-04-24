"""
flow.py — Live options flow scan endpoint.

BUG FIX (2026-04-24):
  The endpoint was querying `flow_events` which has 0 rows.
  All 82,173+ live flow records are stored in `flow_episodes`.
  Fixed to query flow_episodes and map columns to FlowEventOut.

  Column mapping:
    flow_episodes.direction      → sentiment  (REPEAT_BUY→BULLISH etc.)
    flow_episodes.total_premium  → premium
    flow_episodes.trade_count    → (informational)
    flow_episodes.alert_level    → influence_tier  (CRITICAL→WHALE etc.)
    flow_episodes.is_accelerating→ is_golden_sweep (true when accel)
    flow_episodes.signal_ts      → timestamp

Endpoints:
  GET /api/flow/scan?ticker=AAPL&limit=100&offset=0
"""
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from typing import List, Optional
from core.auth import get_current_user, TokenData

import os
import logging
import httpx

log = logging.getLogger("routers.flow")
router = APIRouter(prefix="/api/flow", tags=["flow"])

_SUPABASE_URL = os.environ.get("SUPABASE_URL")
# Use service key so RLS does not silently block SELECT on flow_episodes.
_SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")

# Map flow_episodes.direction → sentiment values the frontend expects
_DIRECTION_TO_SENTIMENT = {
    "REPEAT_BUY":    "BULLISH",
    "REPEAT_SELL":   "BEARISH",
    "BULLISH":       "BULLISH",
    "BEARISH":       "BEARISH",
    "NEUTRAL":       "NEUTRAL",
    "HOLD":          "NEUTRAL",
}

# Map flow_episodes.alert_level → influence_tier values the frontend expects
_ALERT_TO_TIER = {
    "CRITICAL": "WHALE",
    "HIGH":     "INSTITUTIONAL",
    "MEDIUM":   "LARGE",
    "LOW":      "RETAIL",
}


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


async def _query_flow_episodes(
    ticker: Optional[str],
    limit:  int,
    offset: int,
) -> tuple[list[dict], int]:
    """
    Query flow_episodes from Supabase REST API.
    This is the populated table (82k+ rows). flow_events is empty.
    Returns (rows, total_count).
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        log.warning("[flow] SUPABASE_URL or SUPABASE_SERVICE_KEY not set — returning empty flow scan")
        return [], 0

    url = f"{_SUPABASE_URL}/rest/v1/flow_episodes"
    params: dict = {
        "select": "id,ticker,direction,contract_type,strike,expiry,total_premium,"
                  "trade_count,alert_level,is_accelerating,signal_ts,created_at",
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

        log.debug(
            f"[flow] Supabase response: status={resp.status_code} "
            f"content-range={resp.headers.get('content-range', '—')} "
            f"body_preview={resp.text[:200]}"
        )

        if resp.status_code not in (200, 206):
            log.error(f"[flow] Supabase query failed: {resp.status_code} — {resp.text[:300]}")
            return [], 0

        rows = resp.json()

        if not isinstance(rows, list):
            log.error(f"[flow] Unexpected Supabase response type: {type(rows)} — {str(rows)[:200]}")
            return [], 0

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

        log.info(f"[flow] queried flow_episodes: ticker={ticker!r} rows={len(rows)} total={total}")
        return rows, total

    except Exception as e:
        log.error(f"[flow] Supabase query exception: {e}")
        return [], 0


@router.get("/scan", response_model=FlowResponse)
async def scan_flow(
    ticker: Optional[str] = Query(default=None, min_length=1, max_length=10, description="Filter by ticker symbol"),
    limit:  int           = Query(default=100,  ge=1, le=200,                description="Max rows to return"),
    offset: int           = Query(default=0,    ge=0,                        description="Pagination offset"),
    _: TokenData = Depends(get_current_user),
):
    """
    Return recent options flow episodes from the live Supabase flow_episodes table.
    Results are ordered by most recent first.
    """
    ticker_clean = ticker.upper().strip() if ticker else None

    rows, total = await _query_flow_episodes(ticker_clean, limit, offset)

    events = []
    for r in rows:
        try:
            raw_direction  = r.get("direction", "NEUTRAL") or "NEUTRAL"
            raw_alert      = r.get("alert_level", "LOW") or "LOW"

            sentiment      = _DIRECTION_TO_SENTIMENT.get(raw_direction.upper(), "NEUTRAL")
            influence_tier = _ALERT_TO_TIER.get(raw_alert.upper(), "RETAIL")
            is_accel       = bool(r.get("is_accelerating", False))

            # conviction_score: map alert_level to 0.0–1.0
            conviction_map = {"CRITICAL": 0.92, "HIGH": 0.75, "MEDIUM": 0.55, "LOW": 0.35}
            conviction = conviction_map.get(raw_alert.upper(), 0.5)

            events.append(FlowEventOut(
                ticker           = r.get("ticker", ""),
                contract_type    = r.get("contract_type") or "CALL",
                strike           = float(r.get("strike") or 0),
                expiry           = r.get("expiry") or "",
                premium          = float(r.get("total_premium") or 0),
                trade_type       = "SWEEP" if is_accel else "BLOCK",
                sentiment        = sentiment,
                influence_tier   = influence_tier,
                conviction_score = conviction,
                is_golden_sweep  = is_accel,
                timestamp        = r.get("signal_ts") or r.get("created_at"),
            ))
        except Exception as e:
            log.warning(f"[flow] row parse error: {e} — row={r}")
            continue

    return FlowResponse(
        ticker=ticker_clean,
        events=events,
        total=total,
        limit=limit,
        offset=offset,
    )
