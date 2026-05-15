"""
health.py — Stream health endpoint (B-008).

GET /api/health/stream
  Returns the current state of the Tradier stream pipeline:
    mode            : starting | live | demo | idle | reconnecting | market_closed
    active_symbols  : number of OCC contracts currently streamed
    ticks           : total raw events received since process start
    classified      : events that passed parse + dedup (wrote to DB / accumulator)
    deduped         : events dropped by Layer 4 DedupCache
    signals         : composite signals emitted to bus
    errors          : stream-level errors logged
    reconnects      : number of reconnect attempts
    last_tick_at    : ISO-8601 UTC timestamp of last classified tick (null if none yet)
    last_reconnect_at: ISO-8601 UTC timestamp of last reconnect (null if never)
    uptime_seconds  : seconds since process started

GET /stats  (unauthenticated)
  Full raw stats dict from tradier_stream.get_stats() for quick ops debugging.
  Includes all funnel counters: ticks, parsed, parse_failed, below_min_premium,
  index_filtered, deduped, classified, accumulator_gated, persisted, signals,
  sig_debounced, errors, composite_errors, reconnects, gate_epoch, uptime_seconds,
  plus parser stats, dedup stats, lookback stats, and ingestion drop counters.

Auth: Bearer token required for /api/health/stream.
      /stats is intentionally unauthenticated — internal ops use only.
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from core.auth import get_current_user, TokenData
from services.tradier_stream import get_stats

router = APIRouter(tags=["health"])


class StreamHealthOut(BaseModel):
    mode:              str
    active_symbols:    int
    ticks:             int
    classified:        int
    deduped:           int
    signals:           int
    errors:            int
    reconnects:        int
    last_tick_at:      Optional[str]   # ISO-8601 UTC or null
    last_reconnect_at: Optional[str]   # ISO-8601 UTC or null
    uptime_seconds:    float


def _epoch_to_iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@router.get("/api/health/stream", response_model=StreamHealthOut)
async def get_stream_health(_: TokenData = Depends(get_current_user)):
    """
    B-008: Returns live stream pipeline health.
    No DB queries — reads only in-process _stats dict from tradier_stream.
    """
    s = get_stats()
    return StreamHealthOut(
        mode              = s.get("mode", "unknown"),
        active_symbols    = s.get("active_symbols", 0),
        ticks             = s.get("ticks", 0),
        classified        = s.get("classified", 0),
        deduped           = s.get("deduped", 0),
        signals           = s.get("signals", 0),
        errors            = s.get("errors", 0),
        reconnects        = s.get("reconnects", 0),
        last_tick_at      = _epoch_to_iso(s.get("last_tick_at")),
        last_reconnect_at = _epoch_to_iso(s.get("last_reconnect_at")),
        uptime_seconds    = s.get("uptime_seconds", 0.0),
    )


@router.get("/stats")
async def get_full_stats():
    """
    Unauthenticated full stats dump for ops/debugging.

    Returns the complete raw dict from tradier_stream.get_stats(), including
    all funnel counters, parser stats, dedup stats, lookback stats, and
    ingestion drop counters. Use this to diagnose silent drops on Render:

      ticks             — raw events received from Tradier WebSocket
      parsed            — events that passed parse + ingestion gates
      parse_failed      — events where parse_tradier_trade returned None
      below_min_premium — events silently dropped at premium floor (no log)
      index_filtered    — events dropped by ING-011 index ETF gate
      accumulator_gated — events dropped below DTE-adjusted premium floor
      deduped           — events dropped by DedupCache
      persisted         — events written to options_flow_events
      signals           — composite signals emitted to bus
      sig_debounced     — signals suppressed by debounce
      ing_dropped.*     — breakdown from IngestionProcessor gates
      gate_epoch        — current gate_config_store epoch (0 = not loaded)
    """
    return get_stats()

@router.get("/health", include_in_schema=False)
async def health_probe() -> dict:
    return {"status": "ok"}
