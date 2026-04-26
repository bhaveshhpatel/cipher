"""
flow.py — Live options flow scan endpoint.

BUG FIX (2026-04-24):
  Fixed to query flow_episodes (the populated table, 82k+ rows).

BUG FIX (2026-04-26):
  Malformed rows (missing expiry) are now skipped so
  test_flow_scan_malformed_row_is_skipped passes.
  A row is considered malformed when expiry is empty/null — the ticker
  alone cannot produce a valid FlowEventOut (expiry is a required field).
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
_SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")

_DIRECTION_TO_SENTIMENT = {
    "REPEAT_BUY":    "BULLISH",
    "REPEAT_SELL":   "BEARISH",
    "BULLISH":       "BULLISH",
    "BEARISH":       "BEARISH",
    "NEUTRAL":       "NEUTRAL",
    "HOLD":          "NEUTRAL",
}

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


def _is_malformed(r: dict) -> bool:
    """
    Return True if the row is too incomplete to be useful.
    A row is malformed when expiry is empty/null — expiry is a required
    field in FlowEventOut and cannot be defaulted.
    Note: an empty ticker alone does not trigger this check; the row is
    still attempted and will use the fallback empty string.
    """
    expiry = (r.get("expiry") or "").strip()
    return not expiry


async def _query_flow_episodes(
    ticker: Optional[str],
    limit:  int,
    offset: int,
) -> tuple[list[dict], int]:
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

        if resp.status_code not in (200, 206):
            log.error(f"[flow] Supabase query failed: {resp.status_code} — {resp.text[:300]}")
            return [], 0

        rows = resp.json()

        if not isinstance(rows, list):
            log.error(f"[flow] Unexpected Supabase response type: {type(rows)} — {str(rows)[:200]}")
            return [], 0

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
    ticker_clean = ticker.upper().strip() if ticker else None

    rows, total = await _query_flow_episodes(ticker_clean, limit, offset)

    events = []
    for r in rows:
        if _is_malformed(r):
            log.warning(f"[flow] skipping malformed row (no expiry): {r}")
            continue
        try:
            raw_direction  = r.get("direction", "NEUTRAL") or "NEUTRAL"
            raw_alert      = r.get("alert_level", "LOW") or "LOW"

            sentiment      = _DIRECTION_TO_SENTIMENT.get(raw_direction.upper(), "NEUTRAL")
            influence_tier = _ALERT_TO_TIER.get(raw_alert.upper(), "RETAIL")
            is_accel       = bool(r.get("is_accelerating", False))

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
