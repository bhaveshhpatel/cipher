"""
flow_store.py — Supabase DB writer for live options flow.

Subscribes to the async event bus and persists every classified
OptionsFlowEvent to `flow_events` and every signal episode to
`composite_signals`.

Usage — call once at startup from main.py lifespan:

    from services.flow_store import start_flow_writer
    asyncio.create_task(start_flow_writer())

Tables written:
  - flow_events       : one row per classified tick
  - composite_signals : one row per repetition episode that crossed threshold

This module is the ONLY place that writes options flow data to the DB.
The async_bus is purely in-memory fan-out for WebSocket delivery.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger("flow_store")

_SUPABASE_URL: Optional[str] = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY: Optional[str] = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")

# Batch insert flow_events every N seconds to avoid per-tick DB hammering
_FLUSH_INTERVAL = 5  # seconds
_flow_event_buffer: list[dict] = []


def _headers() -> dict:
    return {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }


async def _insert_rows(table: str, rows: list[dict]) -> bool:
    """POST rows to a Supabase REST table. Returns True on success."""
    if not rows or not _SUPABASE_URL or not _SUPABASE_KEY:
        return False
    url = f"{_SUPABASE_URL}/rest/v1/{table}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=_headers(), json=rows)
        if resp.status_code in (200, 201):
            return True
        log.error("[flow_store] insert into %s failed: %d — %s", table, resp.status_code, resp.text[:300])
        return False
    except Exception as e:
        log.error("[flow_store] insert into %s exception: %s", table, e)
        return False


async def _flush_flow_events():
    """Periodic flusher — drains buffer into flow_events table."""
    global _flow_event_buffer
    while True:
        await asyncio.sleep(_FLUSH_INTERVAL)
        if not _flow_event_buffer:
            continue
        batch = _flow_event_buffer.copy()
        _flow_event_buffer.clear()
        ok = await _insert_rows("flow_events", batch)
        if ok:
            log.info("[flow_store] flushed %d flow_events to DB", len(batch))
        else:
            log.warning("[flow_store] failed to flush %d flow_events — data lost", len(batch))


async def persist_flow_event(ev_dict: dict):
    """
    Buffer a raw classified flow tick for batch insert into flow_events.

    Expected ev_dict keys (from parse_tradier_trade output):
      ticker, contract_type, strike, expiry, premium,
      trade_type, sentiment, influence_tier, conviction_score,
      is_golden_sweep, timestamp
    """
    row = {
        "ticker":           ev_dict.get("ticker"),
        "contract_type":    ev_dict.get("contract_type"),
        "strike":           ev_dict.get("strike"),
        "expiry":           ev_dict.get("expiry"),
        "premium":          ev_dict.get("premium"),
        "trade_type":       ev_dict.get("trade_type", "UNKNOWN"),
        "sentiment":        ev_dict.get("sentiment", "UNKNOWN"),
        "influence_tier":   ev_dict.get("influence_tier", "UNKNOWN"),
        "conviction_score": ev_dict.get("conviction_score", 0.0),
        "is_golden_sweep":  ev_dict.get("is_golden_sweep", False),
        "created_at":       datetime.now(timezone.utc).isoformat(),
    }
    _flow_event_buffer.append(row)


async def persist_composite_signal(signal_data: dict):
    """
    Immediately insert a composite signal episode into composite_signals.

    signal_data is the 'data' dict from the bus signal payload:
      ticker, direction, contract_type, strike, expiry,
      total_premium, trade_count, alert_level, is_accelerating,
      seed_episode, timestamp
    """
    row = {
        "ticker":          signal_data.get("ticker"),
        "direction":       signal_data.get("direction"),
        "contract_type":   signal_data.get("contract_type"),
        "strike":          signal_data.get("strike"),
        "expiry":          signal_data.get("expiry"),
        "total_premium":   signal_data.get("total_premium"),
        "trade_count":     signal_data.get("trade_count"),
        "alert_level":     signal_data.get("alert_level"),
        "is_accelerating": signal_data.get("is_accelerating", False),
        "seed_episode":    signal_data.get("seed_episode"),
        "signal_ts":       signal_data.get("timestamp"),
        "created_at":      datetime.now(timezone.utc).isoformat(),
    }
    ok = await _insert_rows("composite_signals", [row])
    if ok:
        log.info(
            "[flow_store] composite_signal saved: %s %s alert=%s prem=$%,.0f",
            row["ticker"], row["contract_type"], row["alert_level"], row["total_premium"] or 0,
        )


async def _bus_signal_listener():
    """
    Subscribe to the async bus on the 'db_writer' channel.
    On every signal published by tradier_stream, persist the composite signal.
    """
    from core.async_bus import bus
    q = bus.subscribe("db_writer")
    log.info("[flow_store] DB writer subscribed to bus — composite_signals will be persisted")
    try:
        while True:
            signal = await q.get()
            if isinstance(signal, dict) and signal.get("type") == "signal":
                await persist_composite_signal(signal.get("data", {}))
    except asyncio.CancelledError:
        bus.unsubscribe("db_writer", q)
        log.info("[flow_store] DB writer unsubscribed from bus")
        raise


async def start_flow_writer():
    """
    Entry point — start both the bus listener and the periodic flusher.
    Call this once from main.py lifespan as an asyncio task.

        stream_task   = asyncio.create_task(stream_options_flow(symbols))
        db_write_task = asyncio.create_task(start_flow_writer())   # <-- add this
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        log.warning(
            "[flow_store] SUPABASE_URL or SUPABASE_KEY not set — "
            "flow_events and composite_signals will NOT be persisted to DB"
        )
        return

    log.info("[flow_store] Starting flow DB writer (flush_interval=%ds)", _FLUSH_INTERVAL)
    await asyncio.gather(
        _bus_signal_listener(),
        _flush_flow_events(),
    )
