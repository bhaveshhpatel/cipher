"""
signal_store.py — Supabase DB writer for composite signals.

Phase 5A changes:
  - _build_row() now persists swarm fields:
    swarm_direction, swarm_confidence, swarm_agents (JSONB),
    swarm_bull_votes, swarm_bear_votes, swarm_hold_votes

Bug fix (2026-04-24 #1):
  - _build_row() was omitting NOT NULL columns—alert_level, sentiment,
    premium, trade_type, is_golden_sweep—causing Postgres error 23502.
    Now derives sensible defaults from the episode/signal data.

Bug fix (2026-04-24 #2):
  - direction column check constraint only allows BUY | SELL | HOLD.
    REPEAT_BUY → BUY, REPEAT_SELL → SELL. (Postgres error 23514)
  - trade_type check constraint only allows SWEEP | BLOCK | SPLIT | SINGLE.
    UNKNOWN and any unrecognised value are dropped (set to NULL) since the
    column is nullable — avoids the constraint while preserving data.

Subscribes to the async event bus on the 'signal_writer' channel and
persists every CompositeSignal to the `signal_history` table.

IMPORTANT — Key selection:
  Uses SUPABASE_SERVICE_ROLE_KEY exclusively. The anon key respects RLS
  and will cause every insert to fail with 42501.
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

# Valid values enforced by DB check constraints
_VALID_DIRECTIONS  = {"BUY", "SELL", "HOLD"}
_VALID_TRADE_TYPES = {"SWEEP", "BLOCK", "SPLIT", "SINGLE"}


def _headers() -> dict:
    return {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }


async def _insert_signal(row: dict) -> bool:
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return False
    url = f"{_SUPABASE_URL}/rest/v1/{_TABLE}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=_headers(), json=[row])
        if resp.status_code in (200, 201):
            return True
        log.error(f"[signal_store] insert failed: {resp.status_code} — {resp.text[:300]}")
        return False
    except Exception as e:
        log.error(f"[signal_store] insert exception: {e}")
        return False


def _normalise_direction(raw: str) -> Optional[str]:
    """
    Map raw direction values to the DB-allowed set: BUY | SELL | HOLD.

    REPEAT_BUY  → BUY
    REPEAT_SELL → SELL
    Anything already valid passes through.
    Anything unrecognised returns None (nullable column).
    """
    if not raw:
        return None
    upper = raw.upper()
    if upper in _VALID_DIRECTIONS:
        return upper
    if "BUY" in upper:
        return "BUY"
    if "SELL" in upper:
        return "SELL"
    if "HOLD" in upper:
        return "HOLD"
    return None


def _normalise_trade_type(raw: str) -> Optional[str]:
    """
    Map raw trade_type to the DB-allowed set: SWEEP | BLOCK | SPLIT | SINGLE.
    Any unrecognised value (including UNKNOWN) returns None so the nullable
    column is left empty rather than triggering a check constraint violation.
    """
    if not raw:
        return None
    upper = raw.upper()
    return upper if upper in _VALID_TRADE_TYPES else None


def _build_row(sig: dict, ep: Optional[dict] = None) -> dict:
    """
    Build a signal_history DB row from a CompositeSignal dict
    plus optional RepetitionEpisode metadata.

    Phase 5A: includes swarm verdict fields.
    Fix #1: ensures all NOT NULL columns are populated (Postgres 23502).
    Fix #2: maps direction and trade_type to DB check-constraint-safe values (Postgres 23514).
    """
    episode = ep or {}

    # ── Derive alert_level from composite_score when not explicit ───────────
    score = sig.get("composite_score") or 0.0
    if sig.get("alert_level"):
        alert_level = sig["alert_level"]
    elif score >= 0.85:
        alert_level = "CONVICTION"
    elif score >= 0.70:
        alert_level = "STRONG_SIGNAL"
    elif score >= 0.55:
        alert_level = "ALERT"
    else:
        alert_level = "WATCH"

    # ── Derive sentiment from contract_type / direction ──────────────────────
    ctype     = episode.get("contract_type") or sig.get("contract_type", "")
    raw_dir   = episode.get("direction", sig.get("direction", ""))
    if sig.get("sentiment"):
        sentiment = sig["sentiment"]
    elif "BUY" in raw_dir.upper() or ctype.upper() == "CALL":
        sentiment = "BULLISH"
    elif "SELL" in raw_dir.upper() or ctype.upper() == "PUT":
        sentiment = "BEARISH"
    else:
        sentiment = "NEUTRAL"

    # ── Normalise constrained enum columns ───────────────────────────────────
    direction  = _normalise_direction(raw_dir)
    trade_type = _normalise_trade_type(
        episode.get("trade_type") or sig.get("trade_type", "")
    )

    return {
        # ── Composite signal fields ─────────────────────────────────────────
        "ticker":                sig.get("ticker"),
        "recommendation":        sig.get("recommendation"),
        "composite_score":       sig.get("composite_score"),
        "flow_score":            sig.get("flow_score"),
        "backtest_score":        sig.get("backtest_score"),
        "volume_premium_factor": sig.get("volume_premium_factor", 0.5),
        "reasoning":             sig.get("reasoning"),
        # ── NOT NULL columns ────────────────────────────────────────────────
        "alert_level":           alert_level,
        "sentiment":             sentiment,
        "premium":               episode.get("total_premium") or sig.get("total_premium") or 0,
        "trade_type":            trade_type,           # None when unrecognised (nullable)
        "is_golden_sweep":       bool(
            episode.get("is_golden_sweep") or sig.get("is_golden_sweep", False)
        ),
        # ── Episode metadata (denormalized) ────────────────────────────────
        "contract_type":         ctype or None,
        "direction":             direction,            # BUY | SELL | HOLD | None
        "influence_tier":        episode.get("influence_tier"),
        "total_premium":         episode.get("total_premium"),
        "trade_count":           episode.get("trade_count"),
        "is_accelerating":       episode.get("is_accelerating", False),
        "signal_ts":             episode.get("timestamp"),
        # ── Phase 5A: swarm verdict fields ─────────────────────────────────
        "swarm_direction":       sig.get("swarm_direction"),
        "swarm_confidence":      sig.get("swarm_confidence"),
        "swarm_bull_votes":      sig.get("swarm_bull_votes"),
        "swarm_bear_votes":      sig.get("swarm_bear_votes"),
        "swarm_hold_votes":      sig.get("swarm_hold_votes"),
        "swarm_agents":          sig.get("swarm_agents"),  # JSONB — list of agent dicts
    }


async def persist_composite_signal(sig: dict, ep: Optional[dict] = None) -> None:
    row = _build_row(sig, ep)
    ok  = await _insert_signal(row)
    if ok:
        swarm = sig.get("swarm_direction", "—")
        log.info(
            f"[signal_store] saved: {row['ticker']} "
            f"{row['recommendation']} score={row['composite_score']} "
            f"swarm={swarm}"
        )
    else:
        log.warning(f"[signal_store] failed to save signal for {row.get('ticker')} — data lost")


async def _bus_signal_listener() -> None:
    from core.async_bus import bus
    q = bus.subscribe("signal_writer")
    log.info("[signal_store] signal writer subscribed to bus")
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
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        log.warning(
            "[signal_store] SUPABASE_URL or SUPABASE_SERVICE_KEY not set — "
            "composite signals will NOT be persisted."
        )
        return
    log.info("[signal_store] Starting composite signal DB writer")
    await _bus_signal_listener()
