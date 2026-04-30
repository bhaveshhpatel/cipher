"""
flow.py — Live options flow endpoints.

Endpoints:
  GET /api/flow/scan     — existing flow scanner (queries flow_episodes)
  GET /api/flow/events   — raw per-trade rows from flow_events table
  GET /api/flow/episodes — aggregated episode rows from flow_episodes

BUG FIX (2026-04-24): Fixed to query flow_episodes (the populated table).
BUG FIX (2026-04-26): Malformed rows (missing expiry) skipped in /scan.
FEAT  (2026-04-28): Added /events + /episodes endpoints (Chunk 1).
BUG FIX (2026-04-29): Renamed tier -> influence_tier in flow_events select + filter.
BUG FIX (2026-04-29): Replace nonexistent 'timestamp' col with 'created_at' in
                      flow_events query (Supabase 42703). Expand select to include
                      conviction_score, dte, trade_type, iv, underlying_price, occ_symbol.
BUG FIX (2026-04-30): FlowEventOut.expiry/strike made Optional — flow_episodes rows are
                      aggregated episodes, not individual contracts. strike/expiry are
                      legitimately null for multi-contract or synthetic episodes. Removed
                      _is_malformed() guard from /scan so these rows are no longer dropped.
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


# ---------------------------------------------------------------------------
# Existing /scan models
# ---------------------------------------------------------------------------

class FlowEventOut(BaseModel):
    ticker:           str
    contract_type:    str
    # strike and expiry are nullable on aggregated episode rows (multi-contract
    # episodes, synthetic flow). Do NOT filter these rows out.
    strike:           Optional[float] = None
    expiry:           Optional[str]   = None
    premium:          float
    trade_type:       str
    sentiment:        str
    influence_tier:   str
    conviction_score: float
    is_golden_sweep:  bool
    timestamp:        Optional[str]   = None


class FlowResponse(BaseModel):
    ticker:  Optional[str]
    events:  List[FlowEventOut]
    total:   int
    limit:   int
    offset:  int


# ---------------------------------------------------------------------------
# /events models — raw per-trade rows from flow_events table
# ---------------------------------------------------------------------------

class FlowEventRaw(BaseModel):
    id:               Optional[str]   = None
    ticker:           str
    strike:           Optional[float] = None
    expiry:           Optional[str]   = None
    dte:              Optional[int]   = None
    contract_type:    str
    trade_type:       Optional[str]   = None
    sentiment:        str
    premium:          float
    size:             int
    bid:              Optional[float] = None
    ask:              Optional[float] = None
    fill_price:       Optional[float] = None
    influence_tier:   Optional[str]   = None
    conviction_score: Optional[float] = None
    is_aggressive:    bool
    is_golden_sweep:  bool
    iv:               Optional[float] = None
    underlying_price: Optional[float] = None
    occ_symbol:       Optional[str]   = None
    timestamp:        Optional[str]   = None  # mapped from created_at


class FlowEventsResponse(BaseModel):
    events:     List[FlowEventRaw]
    total:      int
    limit:      int
    filters:    dict


# ---------------------------------------------------------------------------
# /episodes models — aggregated episode rows from flow_episodes table
# ---------------------------------------------------------------------------

class FlowEpisodeOut(BaseModel):
    id:                     Optional[str]   = None
    ticker:                 str
    direction:              str
    contract_type:          str
    alert_level:            str
    trade_count:            int
    total_premium:          float
    last_signaled_premium:  float
    duration_seconds:       Optional[int]   = None
    started_at:             Optional[str]   = None
    updated_at:             Optional[str]   = None


class FlowEpisodesResponse(BaseModel):
    episodes:  List[FlowEpisodeOut]
    total:     int
    limit:     int
    offset:    int
    ticker:    Optional[str]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _headers() -> dict:
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")
    return {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Accept":        "application/json",
    }


def _is_malformed(r: dict) -> bool:
    """
    Return True if the row is too incomplete to be useful.
    Only used for per-contract rows (flow_events). Do NOT apply to
    flow_episodes rows — strike/expiry are nullable there by design.
    """
    expiry = (r.get("expiry") or "").strip()
    return not expiry


# ---------------------------------------------------------------------------
# Internal query helpers
# ---------------------------------------------------------------------------

async def _query_flow_episodes(
    ticker: Optional[str],
    limit:  int,
    offset: int,
) -> tuple[list[dict], int]:
    url_base = os.environ.get("SUPABASE_URL")
    if not url_base or not (os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")):
        log.warning("[flow] SUPABASE_URL or SUPABASE_SERVICE_KEY not set — returning empty flow scan")
        return [], 0

    url = f"{url_base}/rest/v1/flow_episodes"
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


async def _query_flow_events(
    ticker:         Optional[str],
    sentiment:      Optional[str],
    contract_type:  Optional[str],
    tier:           Optional[str],
    aggressive:     Optional[bool],
    golden_sweep:   Optional[bool],
    limit:          int,
    offset:         int,
) -> tuple[list[dict], int]:
    """Query flow_events table with optional filters."""
    url_base = os.environ.get("SUPABASE_URL")
    if not url_base or not (os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")):
        log.warning("[flow/events] SUPABASE_URL or key not set — returning empty")
        return [], 0

    url = f"{url_base}/rest/v1/flow_events"
    params: dict = {
        # 'timestamp' does not exist — the column is 'created_at'
        "select": "id,ticker,strike,expiry,dte,contract_type,trade_type,sentiment,"
                  "premium,size,bid,ask,fill_price,influence_tier,conviction_score,"
                  "is_aggressive,is_golden_sweep,iv,underlying_price,occ_symbol,created_at",
        "order":  "created_at.desc",
        "limit":  str(limit),
        "offset": str(offset),
    }

    if ticker:
        params["ticker"] = f"eq.{ticker.upper()}"
    if sentiment:
        params["sentiment"] = f"eq.{sentiment.upper()}"
    if contract_type:
        params["contract_type"] = f"eq.{contract_type.upper()}"
    if tier:
        params["influence_tier"] = f"eq.{tier.upper()}"
    if aggressive is not None:
        params["is_aggressive"] = f"eq.{str(aggressive).lower()}"
    if golden_sweep is not None:
        params["is_golden_sweep"] = f"eq.{str(golden_sweep).lower()}"

    headers = {**_headers(), "Prefer": "count=exact"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers, params=params)

        if resp.status_code not in (200, 206):
            log.error(f"[flow/events] Supabase error: {resp.status_code} — {resp.text[:300]}")
            return [], 0

        rows = resp.json()
        if not isinstance(rows, list):
            log.error(f"[flow/events] Unexpected response type: {type(rows)}")
            return [], 0

        content_range = resp.headers.get("content-range", "")
        total = len(rows)
        if "/" in content_range:
            try:
                total = int(content_range.split("/")[1])
            except ValueError:
                pass

        log.info(f"[flow/events] rows={len(rows)} total={total}")
        return rows, total

    except Exception as e:
        log.error(f"[flow/events] query exception: {e}")
        return [], 0


async def _query_episodes_v2(
    ticker:        Optional[str],
    direction:     Optional[str],
    contract_type: Optional[str],
    alert_level:   Optional[str],
    limit:         int,
    offset:        int,
) -> tuple[list[dict], int]:
    """Query flow_episodes with full filter support for /episodes endpoint."""
    url_base = os.environ.get("SUPABASE_URL")
    if not url_base or not (os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")):
        log.warning("[flow/episodes] SUPABASE_URL or key not set — returning empty")
        return [], 0

    url = f"{url_base}/rest/v1/flow_episodes"
    params: dict = {
        "select": "id,ticker,direction,contract_type,alert_level,trade_count,"
                  "total_premium,last_signaled_premium,duration_seconds,"
                  "started_at,updated_at",
        "order":  "updated_at.desc",
        "limit":  str(limit),
        "offset": str(offset),
    }

    if ticker:
        params["ticker"] = f"eq.{ticker.upper()}"
    if direction:
        params["direction"] = f"eq.{direction.upper()}"
    if contract_type:
        params["contract_type"] = f"eq.{contract_type.upper()}"
    if alert_level:
        params["alert_level"] = f"eq.{alert_level.upper()}"

    headers = {**_headers(), "Prefer": "count=exact"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers, params=params)

        if resp.status_code not in (200, 206):
            log.error(f"[flow/episodes] Supabase error: {resp.status_code} — {resp.text[:300]}")
            return [], 0

        rows = resp.json()
        if not isinstance(rows, list):
            log.error(f"[flow/episodes] Unexpected response type: {type(rows)}")
            return [], 0

        content_range = resp.headers.get("content-range", "")
        total = len(rows)
        if "/" in content_range:
            try:
                total = int(content_range.split("/")[1])
            except ValueError:
                pass

        log.info(f"[flow/episodes] rows={len(rows)} total={total}")
        return rows, total

    except Exception as e:
        log.error(f"[flow/episodes] query exception: {e}")
        return [], 0


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

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
        # NOTE: do NOT call _is_malformed() here — flow_episodes rows are
        # aggregated episodes where strike/expiry are nullable by design.
        # _is_malformed() is only appropriate for per-contract flow_events rows.
        try:
            raw_direction  = r.get("direction", "NEUTRAL") or "NEUTRAL"
            raw_alert      = r.get("alert_level", "LOW") or "LOW"

            sentiment      = _DIRECTION_TO_SENTIMENT.get(raw_direction.upper(), "NEUTRAL")
            influence_tier = _ALERT_TO_TIER.get(raw_alert.upper(), "RETAIL")
            is_accel       = bool(r.get("is_accelerating", False))

            conviction_map = {"CRITICAL": 0.92, "HIGH": 0.75, "MEDIUM": 0.55, "LOW": 0.35}
            conviction = conviction_map.get(raw_alert.upper(), 0.5)

            strike_raw = r.get("strike")
            events.append(FlowEventOut(
                ticker           = r.get("ticker", ""),
                contract_type    = r.get("contract_type") or "CALL",
                strike           = float(strike_raw) if strike_raw is not None else None,
                expiry           = r.get("expiry") or None,
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


@router.get("/events", response_model=FlowEventsResponse)
async def get_flow_events(
    ticker:        Optional[str]  = Query(default=None, min_length=1, max_length=10),
    sentiment:     Optional[str]  = Query(default=None, description="BULLISH | BEARISH | NEUTRAL"),
    contract_type: Optional[str]  = Query(default=None, description="CALL | PUT"),
    tier:          Optional[str]  = Query(default=None, description="T1 | T2 | T3"),
    aggressive:    Optional[bool] = Query(default=None),
    golden_sweep:  Optional[bool] = Query(default=None),
    limit:         int            = Query(default=50, ge=1, le=500),
    offset:        int            = Query(default=0,  ge=0),
    _: TokenData = Depends(get_current_user),
):
    ticker_clean = ticker.upper().strip() if ticker else None

    rows, total = await _query_flow_events(
        ticker=ticker_clean,
        sentiment=sentiment,
        contract_type=contract_type,
        tier=tier,
        aggressive=aggressive,
        golden_sweep=golden_sweep,
        limit=limit,
        offset=offset,
    )

    events: list[FlowEventRaw] = []
    for r in rows:
        try:
            events.append(FlowEventRaw(
                id               = str(r.get("id") or "") or None,
                ticker           = r.get("ticker") or "",
                strike           = float(r["strike"]) if r.get("strike") is not None else None,
                expiry           = r.get("expiry") or None,
                dte              = int(r["dte"]) if r.get("dte") is not None else None,
                contract_type    = r.get("contract_type") or "CALL",
                trade_type       = r.get("trade_type") or None,
                sentiment        = r.get("sentiment") or "NEUTRAL",
                premium          = float(r.get("premium") or 0),
                size             = int(r.get("size") or 0),
                bid              = float(r["bid"]) if r.get("bid") is not None else None,
                ask              = float(r["ask"]) if r.get("ask") is not None else None,
                fill_price       = float(r["fill_price"]) if r.get("fill_price") is not None else None,
                influence_tier   = r.get("influence_tier") or None,
                conviction_score = float(r["conviction_score"]) if r.get("conviction_score") is not None else None,
                is_aggressive    = bool(r.get("is_aggressive", False)),
                is_golden_sweep  = bool(r.get("is_golden_sweep", False)),
                iv               = float(r["iv"]) if r.get("iv") is not None else None,
                underlying_price = float(r["underlying_price"]) if r.get("underlying_price") is not None else None,
                occ_symbol       = r.get("occ_symbol") or None,
                timestamp        = r.get("created_at") or None,
            ))
        except Exception as e:
            log.warning(f"[flow/events] row parse error: {e} — row={r}")
            continue

    active_filters = {
        k: v for k, v in {
            "ticker": ticker_clean,
            "sentiment": sentiment,
            "contract_type": contract_type,
            "tier": tier,
            "aggressive": aggressive,
            "golden_sweep": golden_sweep,
        }.items() if v is not None
    }

    return FlowEventsResponse(
        events=events,
        total=total,
        limit=limit,
        filters=active_filters,
    )


@router.get("/episodes", response_model=FlowEpisodesResponse)
async def get_flow_episodes(
    ticker:        Optional[str] = Query(default=None, min_length=1, max_length=10),
    direction:     Optional[str] = Query(default=None, description="BULLISH | BEARISH | NEUTRAL"),
    contract_type: Optional[str] = Query(default=None, description="CALL | PUT"),
    alert_level:   Optional[str] = Query(default=None, description="WATCH | ALERT | STRONG | HOLD"),
    limit:         int           = Query(default=50, ge=1, le=500),
    offset:        int           = Query(default=0,  ge=0),
    _: TokenData = Depends(get_current_user),
):
    ticker_clean = ticker.upper().strip() if ticker else None

    rows, total = await _query_episodes_v2(
        ticker=ticker_clean,
        direction=direction,
        contract_type=contract_type,
        alert_level=alert_level,
        limit=limit,
        offset=offset,
    )

    episodes: list[FlowEpisodeOut] = []
    for r in rows:
        try:
            episodes.append(FlowEpisodeOut(
                id                    = str(r.get("id") or "") or None,
                ticker                = r.get("ticker") or "",
                direction             = r.get("direction") or "NEUTRAL",
                contract_type         = r.get("contract_type") or "CALL",
                alert_level           = r.get("alert_level") or "WATCH",
                trade_count           = int(r.get("trade_count") or 0),
                total_premium         = float(r.get("total_premium") or 0),
                last_signaled_premium = float(r.get("last_signaled_premium") or 0),
                duration_seconds      = int(r["duration_seconds"]) if r.get("duration_seconds") is not None else None,
                started_at            = r.get("started_at") or None,
                updated_at            = r.get("updated_at") or None,
            ))
        except Exception as e:
            log.warning(f"[flow/episodes] row parse error: {e} — row={r}")
            continue

    return FlowEpisodesResponse(
        episodes=episodes,
        total=total,
        limit=limit,
        offset=offset,
        ticker=ticker_clean,
    )
