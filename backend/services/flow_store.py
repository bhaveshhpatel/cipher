"""
flow_store.py — Supabase DB writer for live options flow.

Subscribes to the async event bus and persists every classified
OptionsFlowEvent to `flow_events` and every signal episode to
`flow_episodes`.

Usage — call once at startup from main.py lifespan:

    from services.flow_store import start_flow_writer
    asyncio.create_task(start_flow_writer())

Tables written:
  - flow_events   : one row per classified tick (batched every 500ms or 100 rows)
  - flow_episodes : one row per repetition episode that crossed threshold
                    (written immediately on every qualifying signal)

NOTE: Neither table receives an `id` field — Postgres generates it
      (uuid default for flow_events, bigserial for flow_episodes).

This module is the ONLY place that writes options flow data to the DB.
The async_bus is purely in-memory fan-out for WebSocket delivery.

IMPORTANT — Key selection:
  Only SUPABASE_SERVICE_ROLE_KEY is used here. The service role key
  bypasses Row Level Security (RLS) which is required for server-side
  inserts. The anon/public key (SUPABASE_KEY) respects RLS and will
  cause every insert to fail with a 42501 policy violation. Never use
  the anon key for backend DB writes.

Fix (C-010):
  - Env var corrected: SUPABASE_SERVICE_KEY → SUPABASE_SERVICE_ROLE_KEY
  - persist_flow_event() now writes ALL flow_events columns:
    fill_price, bid, ask, size, dte, bid_ask_class, is_aggressive,
    exchange_count, fill_count, open_interest, iv, underlying_price.
  - expiry sent as None (not "") when empty so Postgres DATE/TEXT column
    does not receive an empty string that triggers a cast error.

Fix (C-011):
  - strike=0 sent as 0.0 (not None) — parser now skips zero-strike trades
    so any trade reaching here has a valid strike.
  - _bus_signal_listener now handles both 'signal' and 'composite_signal'
    event types — previously composite_signal events were silently dropped.
  - persist_flow_event logs a warning when strike or expiry is missing
    so parsing quality can be monitored in Railway logs.

Architecture change (Layer 1+2):
  - occ_symbol field now written to flow_events.occ_symbol column.
    This stores the raw OCC contract string (e.g. "AAPL  260117C00180000")
    for every persisted trade, enabling contract-level querying and auditing.

Architecture fix (Layer 5):
  - FLUSH_INTERVAL corrected from 5s → 0.5s (500ms) to match the 6-layer
    architecture spec (flush every 500ms or 100 rows, whichever comes first).
    The previous 5s interval was an oversight — at 62K rows/day the buffer
    would grow ~430 rows before each flush, risking data loss on crash.
  - _flush_flow_events() now also flushes early if buffer hits FLUSH_MAX_ROWS=100.

Fix (C-016):
  - Added `global _flow_event_buffer` declaration inside persist_flow_event().
    Without it, Python treats the local reassignment
    `_flow_event_buffer = _flow_event_buffer[_FLUSH_MAX_ROWS:]` as a local
    variable binding for the entire function scope, causing an UnboundLocalError
    on the `.append(row)` line before the reassignment is ever reached.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

log = logging.getLogger("flow_store")

_SUPABASE_URL: Optional[str] = os.environ.get("SUPABASE_URL")

# IMPORTANT: Use the service role key ONLY — it bypasses RLS.
# Never fall back to the anon key (SUPABASE_KEY) here; the anon key
# respects RLS and will produce 401/42501 errors on every insert.
# Env var name: SUPABASE_SERVICE_ROLE_KEY  (Railway config)
_SUPABASE_KEY: Optional[str] = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")  # legacy fallback
)

# Layer 5: Batch insert flow_events every 500ms OR 100 rows — whichever first.
# Architecture spec: flush every 500ms or 100 rows to prevent per-tick DB hammering
# while keeping latency low. Previous value of 5s was a spec mismatch.
_FLUSH_INTERVAL  = 0.5   # seconds (500ms)
_FLUSH_MAX_ROWS  = 100   # flush early if buffer hits this size
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
        log.error(f"[flow_store] insert into {table} failed: {resp.status_code} — {resp.text[:300]}")
        return False
    except Exception as e:
        log.error(f"[flow_store] insert into {table} exception: {e}")
        return False


async def _flush_flow_events():
    """
    Layer 5: Periodic flusher — drains buffer into flow_events table.
    Flushes every 500ms (FLUSH_INTERVAL) or when buffer hits 100 rows
    (FLUSH_MAX_ROWS), whichever comes first.
    """
    global _flow_event_buffer
    while True:
        await asyncio.sleep(_FLUSH_INTERVAL)
        if not _flow_event_buffer:
            continue
        # Flush now regardless — interval elapsed
        batch = _flow_event_buffer[:_FLUSH_MAX_ROWS]
        _flow_event_buffer = _flow_event_buffer[_FLUSH_MAX_ROWS:]
        ok = await _insert_rows("flow_events", batch)
        if ok:
            log.info(f"[flow_store] flushed {len(batch)} flow_events to DB")
        else:
            log.warning(f"[flow_store] failed to flush {len(batch)} flow_events — data lost")


async def persist_flow_event(ev_dict: dict):
    """
    Buffer a raw classified flow tick for batch insert into flow_events.
    Flushes immediately if buffer has hit FLUSH_MAX_ROWS (100) — prevents
    unbounded growth between 500ms ticks.

    NOTE: No `id` field is sent — flow_events.id is a uuid generated by Postgres.
    """
    global _flow_event_buffer  # required: we reassign the list reference on early flush

    expiry = ev_dict.get("expiry") or None   # "" → None, avoids DB cast error
    strike = ev_dict.get("strike")           # Keep 0.0 as-is (valid numeric)
    if strike is None:
        strike = None

    ticker = ev_dict.get("ticker", "UNKNOWN")
    if not expiry:
        log.warning(f"[flow_store] {ticker}: expiry is empty — OCC parse may have failed")
    if strike == 0.0:
        log.warning(f"[flow_store] {ticker}: strike=0.0 — verify OCC parse for this symbol")

    row = {
        "ticker":            ticker,
        "contract_type":     ev_dict.get("contract_type"),
        "strike":            strike,
        "expiry":            expiry,
        "dte":               ev_dict.get("dte", 0),
        "fill_price":        ev_dict.get("fill_price", 0.0),
        "bid":               ev_dict.get("bid", 0.0),
        "ask":               ev_dict.get("ask", 0.0),
        "size":              ev_dict.get("size", 0),
        "premium":           ev_dict.get("premium", 0.0),
        "trade_type":        ev_dict.get("trade_type", "UNKNOWN"),
        "bid_ask_class":     ev_dict.get("bid_ask_class", "MID"),
        "is_aggressive":     ev_dict.get("is_aggressive", False),
        "is_golden_sweep":   ev_dict.get("is_golden_sweep", False),
        "sentiment":         ev_dict.get("sentiment", "NEUTRAL"),
        "influence_tier":    ev_dict.get("influence_tier", "RETAIL"),
        "conviction_score":  ev_dict.get("conviction_score", 0.0),
        "exchange_count":    ev_dict.get("exchange_count", 1),
        "fill_count":        ev_dict.get("fill_count", 1),
        "open_interest":     ev_dict.get("open_interest", 0),
        "iv":                ev_dict.get("iv", 0.0),
        "underlying_price":  ev_dict.get("underlying_price", 0.0),
        "occ_symbol":        ev_dict.get("occ_symbol"),
        # created_at: let Postgres default (now())
    }
    _flow_event_buffer.append(row)

    # Early flush if buffer hit max — don't wait for next 500ms tick
    if len(_flow_event_buffer) >= _FLUSH_MAX_ROWS:
        batch = _flow_event_buffer[:_FLUSH_MAX_ROWS]
        _flow_event_buffer = _flow_event_buffer[_FLUSH_MAX_ROWS:]
        ok = await _insert_rows("flow_events", batch)
        if ok:
            log.info(f"[flow_store] early flush ({_FLUSH_MAX_ROWS} rows) — buffer hit max")
        else:
            log.warning(f"[flow_store] early flush failed — {_FLUSH_MAX_ROWS} rows lost")


async def persist_flow_episode(signal_data: dict):
    """
    Immediately insert a flow signal episode into flow_episodes.

    NOTE: No `id` field is sent — flow_episodes.id is a bigserial generated by Postgres.
    """
    expiry = signal_data.get("expiry") or None  # "" → None

    row = {
        "ticker":          signal_data.get("ticker"),
        "direction":       signal_data.get("direction"),
        "contract_type":   signal_data.get("contract_type"),
        "strike":          signal_data.get("strike"),
        "expiry":          expiry,
        "total_premium":   signal_data.get("total_premium"),
        "trade_count":     signal_data.get("trade_count"),
        "alert_level":     signal_data.get("alert_level"),
        "is_accelerating": signal_data.get("is_accelerating", False),
        "seed_episode":    signal_data.get("seed_episode"),
        "signal_ts":       signal_data.get("timestamp"),
        # created_at: let Postgres default (now())
    }
    ok = await _insert_rows("flow_episodes", [row])
    if ok:
        log.info(
            f"[flow_store] flow_episode saved: {row['ticker']} {row['contract_type']} "
            f"alert={row['alert_level']} prem=${(row['total_premium'] or 0):,.0f}"
        )


async def _bus_signal_listener():
    """
    Subscribe to the async bus on the 'db_writer' channel.
    Handles both 'signal' (flow episode) and 'composite_signal' events.
    """
    from core.async_bus import bus
    q = bus.subscribe("db_writer")
    log.info("[flow_store] DB writer subscribed to bus — flow_episodes will be persisted")
    try:
        while True:
            msg = await q.get()
            if not isinstance(msg, dict):
                continue
            msg_type = msg.get("type")
            if msg_type == "signal":
                await persist_flow_episode(msg.get("data", {}))
            elif msg_type == "composite_signal":
                data = msg.get("data", {})
                sig  = data.get("signal", {})
                ep   = data.get("episode", {})
                await persist_flow_episode({
                    "ticker":          sig.get("ticker"),
                    "direction":       ep.get("direction"),
                    "contract_type":   ep.get("contract_type"),
                    "strike":          None,
                    "expiry":          None,
                    "total_premium":   ep.get("total_premium"),
                    "trade_count":     ep.get("trade_count"),
                    "alert_level":     sig.get("recommendation"),
                    "is_accelerating": ep.get("is_accelerating", False),
                    "seed_episode":    sig.get("reasoning"),
                    "timestamp":       ep.get("timestamp"),
                })
    except asyncio.CancelledError:
        bus.unsubscribe("db_writer", q)
        log.info("[flow_store] DB writer unsubscribed from bus")
        raise


async def start_flow_writer():
    """
    Entry point — start both the bus listener and the periodic flusher.
    Call this once from main.py lifespan as an asyncio task.
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        log.warning(
            "[flow_store] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set — "
            "flow_events and flow_episodes will NOT be persisted to DB. "
            "Ensure SUPABASE_SERVICE_ROLE_KEY (not the anon key) is set in Railway env vars."
        )
        return

    log.info(f"[flow_store] Starting flow DB writer (flush_interval={_FLUSH_INTERVAL}s, max_rows={_FLUSH_MAX_ROWS})")
    await asyncio.gather(
        _bus_signal_listener(),
        _flush_flow_events(),
    )
