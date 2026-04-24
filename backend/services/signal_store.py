"""
signal_store.py — Supabase DB writer for composite signals.

Subscribes to the async event bus on the 'signal_writer' channel and
persists every CompositeSignal to the `signal_history` table.

Usage — call once at startup from main.py lifespan:

    from services.signal_store import start_signal_writer
    asyncio.create_task(start_signal_writer())

Table written:
  - signal_history : one row per CompositeSignal emitted by
                     composite_signal_engine.build_composite()

NOTE: id is BIGSERIAL — never send it. Postgres generates it.

IMPORTANT — Key selection:
  Uses SUPABASE_SERVICE_ROLE_KEY exclusively. The anon key (SUPABASE_KEY)
  respects RLS and will cause every insert to fail with 42501.
  Same rule as flow_store.py — never introduce an anon-key fallback here.

Bus channel: 'signal_writer'
  The tradier_stream / composite_signal_engine publishes signals on the bus
  with type='composite_signal' and data=CompositeSignal.__dict__.
  This module subscribes on the 'signal_writer' channel.
"""
import asyncio
import logging
import os
from typing import Optional

import httpx

log = logging.getLogger("signal_store")

_SUPABASE_URL: Optional[str] = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY: Optional[str] = os.environ.get("SUPABASE_SERVICE_KEY")

_TABLE = "signal_history"


def _headers() -> dict:
    return {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }


async def _insert_signal(row: dict) -> bool:
    """POST a single composite signal row to signal_history."""
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return False
    url = f"{_SUPABASE_URL}/rest/v1/{_TABLE}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=_headers(), json=[row])
        if resp.status_code in (200, 201):
            return True
        log.error(
            f"[signal_store] insert failed: {resp.status_code} — {resp.text[:300]}"
        )
        return False
    except Exception as e:
        log.error(f"[signal_store] insert exception: {e}")
        return False


def _build_row(sig: dict, ep: Optional[dict] = None) -> dict:
    """
    Build a signal_history DB row from a CompositeSignal dict
    plus optional RepetitionEpisode metadata.

    The bus publishes composite signals as:
      {
        "type": "composite_signal",
        "data": {
          "signal":  CompositeSignal.__dict__,
          "episode": RepetitionEpisode summary dict
        }
      }
    """
    episode = ep or {}
    return {
        "ticker":                sig.get("ticker"),
        "recommendation":        sig.get("recommendation"),
        "composite_score":       sig.get("composite_score"),
        "flow_score":            sig.get("flow_score"),
        "backtest_score":        sig.get("backtest_score"),
        "volume_premium_factor": sig.get("volume_premium_factor", 0.5),
        "reasoning":             sig.get("reasoning"),
        # Episode metadata (denormalized)
        "contract_type":         episode.get("contract_type"),
        "direction":             episode.get("direction"),
        "influence_tier":        episode.get("influence_tier"),
        "total_premium":         episode.get("total_premium"),
        "trade_count":           episode.get("trade_count"),
        "is_accelerating":       episode.get("is_accelerating", False),
        "signal_ts":             episode.get("timestamp"),
        # created_at: let Postgres default (now())
    }


async def persist_composite_signal(sig: dict, ep: Optional[dict] = None) -> None:
    """Insert one CompositeSignal to signal_history immediately."""
    row = _build_row(sig, ep)
    ok = await _insert_signal(row)
    if ok:
        log.info(
            f"[signal_store] signal saved: {row['ticker']} "
            f"{row['recommendation']} score={row['composite_score']}"
        )
    else:
        log.warning(
            f"[signal_store] failed to save signal for {row.get('ticker')} — data lost"
        )


async def _bus_signal_listener() -> None:
    """
    Subscribe to the bus on the 'signal_writer' channel.
    Expects messages with type='composite_signal':
      {
        "type": "composite_signal",
        "data": {
          "signal":  { ...CompositeSignal fields... },
          "episode": { ...episode metadata... }
        }
      }
    """
    from core.async_bus import bus
    q = bus.subscribe("signal_writer")
    log.info("[signal_store] signal writer subscribed to bus — composite signals will be persisted")
    try:
        while True:
            msg = await q.get()
            if isinstance(msg, dict) and msg.get("type") == "composite_signal":
                data = msg.get("data", {})
                sig  = data.get("signal", {})
                ep   = data.get("episode", {})
                if sig:
                    await persist_composite_signal(sig, ep)
    except asyncio.CancelledError:
        bus.unsubscribe("signal_writer", q)
        log.info("[signal_store] signal writer unsubscribed from bus")
        raise


async def start_signal_writer() -> None:
    """
    Entry point — start the bus listener for composite signals.
    Call once from main.py lifespan as an asyncio task:

        signal_write_task = asyncio.create_task(start_signal_writer())
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        log.warning(
            "[signal_store] SUPABASE_URL or SUPABASE_SERVICE_KEY not set — "
            "composite signals will NOT be persisted to signal_history. "
            "Ensure SUPABASE_SERVICE_KEY (not the anon key) is set in Railway env vars."
        )
        return

    log.info("[signal_store] Starting composite signal DB writer")
    await _bus_signal_listener()
