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
                    (written immediately on every qualifying composite_signal)

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
"""
import asyncio
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger("flow_store")

_SUPABASE_URL: Optional[str] = os.environ.get("SUPABASE_URL")

# IMPORTANT: Use the service role key ONLY -- it bypasses RLS.
_SUPABASE_KEY: Optional[str] = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")  # legacy fallback
)

_FLUSH_INTERVAL  = 0.5
_FLUSH_MAX_ROWS  = 100
_flow_event_buffer: list[dict] = []

_RETRY_MAX     = 3
_RETRY_DELAY_S = 1.0


def _is_configured() -> bool:
    return bool(_SUPABASE_URL and _SUPABASE_KEY)


def _headers() -> dict:
    return {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }


async def _insert_rows(table: str, rows: list[dict]) -> bool:
    if not rows or not _SUPABASE_URL or not _SUPABASE_KEY:
        return False
    url = f"{_SUPABASE_URL}/rest/v1/{table}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=_headers(), json=rows)
        if resp.status_code in (200, 201):
            return True
        log.error(f"[flow_store] insert into {table} failed: {resp.status_code} -- {resp.text[:300]}")
        return False
    except Exception as e:
        log.error(f"[flow_store] insert into {table} exception: {e}")
        return False


async def _insert_rows_with_retry(table: str, rows: list[dict]) -> bool:
    for attempt in range(1, _RETRY_MAX + 1):
        ok = await _insert_rows(table, rows)
        if ok:
            return True
        if attempt < _RETRY_MAX:
            log.warning(
                f"[flow_store] insert into {table} failed (attempt {attempt}/{_RETRY_MAX}) "
                f"-- retrying in {_RETRY_DELAY_S}s"
            )
            await asyncio.sleep(_RETRY_DELAY_S)
    log.error(
        f"[flow_store] insert into {table} failed after {_RETRY_MAX} attempts "
        f"-- {len(rows)} rows DISCARDED. Check Supabase connectivity."
    )
    return False


async def _flush_flow_events():
    global _flow_event_buffer
    while True:
        await asyncio.sleep(_FLUSH_INTERVAL)
        if not _flow_event_buffer:
            continue
        batch = _flow_event_buffer[:_FLUSH_MAX_ROWS]
        _flow_event_buffer = _flow_event_buffer[_FLUSH_MAX_ROWS:]
        ok = await _insert_rows_with_retry("flow_events", batch)
        if ok:
            log.info(f"[flow_store] flushed {len(batch)} flow_events to DB")


async def persist_flow_event(ev_dict: dict):
    global _flow_event_buffer

    if not _is_configured():
        log.warning(
            "[flow_store] persist_flow_event: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY "
            "not set — event dropped. Set env vars to enable DB persistence."
        )
        return

    expiry = ev_dict.get("expiry") or None
    strike = ev_dict.get("strike")
    if strike is None:
        strike = None

    ticker = ev_dict.get("ticker", "UNKNOWN")
    if not expiry:
        log.warning(f"[flow_store] {ticker}: expiry is empty -- OCC parse may have failed")
    if strike == 0.0:
        log.warning(f"[flow_store] {ticker}: strike=0.0 -- verify OCC parse for this symbol")

    row = {
        "ticker":               ticker,
        "contract_type":        ev_dict.get("contract_type"),
        "strike":               strike,
        "expiry":               expiry,
        "dte":                  ev_dict.get("dte", 0),
        "fill_price":           ev_dict.get("fill_price", 0.0),
        "bid":                  ev_dict.get("bid", 0.0),
        "ask":                  ev_dict.get("ask", 0.0),
        "size":                 ev_dict.get("size", 0),
        "premium":              ev_dict.get("premium", 0.0),
        "trade_type":           ev_dict.get("trade_type", "UNKNOWN"),
        "bid_ask_class":        ev_dict.get("bid_ask_class", "MID"),
        "is_aggressive":        ev_dict.get("is_aggressive", False),
        "is_golden_sweep":      ev_dict.get("is_golden_sweep", False),
        "sentiment":            ev_dict.get("sentiment", "NEUTRAL"),
        "influence_tier":       ev_dict.get("influence_tier", "RETAIL"),
        "conviction_score":     ev_dict.get("conviction_score", 0.0),
        "exchange_count":       ev_dict.get("exchange_count", 1),
        "fill_count":           ev_dict.get("fill_count", 1),
        "open_interest":        ev_dict.get("open_interest", 0),
        "iv":                   ev_dict.get("iv", 0.0),
        "underlying_price":     ev_dict.get("underlying_price", 0.0),
        "occ_symbol":           ev_dict.get("occ_symbol"),
        "is_synthetic_quote":   ev_dict.get("is_synthetic_quote", False),
    }
    _flow_event_buffer.append(row)

    if len(_flow_event_buffer) >= _FLUSH_MAX_ROWS:
        batch = _flow_event_buffer[:_FLUSH_MAX_ROWS]
        _flow_event_buffer = _flow_event_buffer[_FLUSH_MAX_ROWS:]
        ok = await _insert_rows_with_retry("flow_events", batch)
        if ok:
            log.info(f"[flow_store] early flush ({_FLUSH_MAX_ROWS} rows) -- buffer hit max")


async def persist_flow_episode(signal_data: dict):
    expiry = signal_data.get("expiry") or None

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
    }
    ok = await _insert_rows("flow_episodes", [row])
    if ok:
        log.info(
            f"[flow_store] flow_episode saved: {row['ticker']} {row['contract_type']} "
            f"alert={row['alert_level']} prem=${(row['total_premium'] or 0):,.0f}"
        )


async def _bus_signal_listener():
    from core.async_bus import bus
    q = bus.subscribe("db_writer")
    log.info("[flow_store] DB writer subscribed to bus -- flow_episodes written on composite_signal only")
    try:
        while True:
            msg = await q.get()
            if not isinstance(msg, dict):
                continue
            msg_type = msg.get("type")
            if msg_type == "composite_signal":
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
    if not _is_configured():
        log.warning(
            "[flow_store] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set -- "
            "flow_events and flow_episodes will NOT be persisted to DB. "
            "Ensure SUPABASE_SERVICE_ROLE_KEY (not the anon key) is set in Railway env vars."
        )
        return

    log.info(f"[flow_store] Starting flow DB writer (flush_interval={_FLUSH_INTERVAL}s, max_rows={_FLUSH_MAX_ROWS}, retry_max={_RETRY_MAX})")
    await asyncio.gather(
        _bus_signal_listener(),
        _flush_flow_events(),
    )
