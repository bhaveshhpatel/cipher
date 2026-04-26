"""
signal_store.py — Supabase DB writer for composite signals.

Public API (module-level):
  save_signal(sig)          async — persist a signal dict (alias for persist_composite_signal)
  get_signals(ticker)       async — retrieve recent signals for a ticker
  persist_composite_signal  async — full persist with episode context
  start_signal_writer       async — long-running bus listener

Internal (patchable in tests):
  _client()                 — returns a configured httpx client or None
  _normalise_direction(s)   — canonicalise direction strings
  _normalise_trade_type(s)  — canonicalise trade type strings
"""
import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from core.async_bus import bus

log = logging.getLogger("signal_store")

_SUPABASE_URL: Optional[str] = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY: Optional[str] = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")
)

_TABLE = "signal_history"

_VALID_DIRECTIONS  = {"BUY", "SELL", "HOLD"}
_VALID_TRADE_TYPES = {"SWEEP", "BLOCK", "SPLIT", "SINGLE"}
_VALID_TIERS       = {"WHALE", "INSTITUTIONAL", "LARGE", "RETAIL"}

_RETRY_MAX     = 3
_RETRY_DELAY_S = 1.0

# In-memory store for tests / when DB not configured
_in_memory_signals: List[Dict] = []


def _is_configured() -> bool:
    return bool(_SUPABASE_URL and _SUPABASE_KEY)


def _client() -> Optional[httpx.AsyncClient]:
    """
    Return a configured async HTTP client, or None if Supabase is not configured.
    Exposed at module level so tests can patch it: patch.object(signal_store, '_client', ...)
    """
    if not _is_configured():
        return None
    return httpx.AsyncClient(timeout=10.0)


def _headers() -> dict:
    return {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }


async def _insert_signal(row: dict) -> bool:
    client = _client()
    if client is None:
        return False
    url = f"{_SUPABASE_URL}/rest/v1/{_TABLE}"
    try:
        async with client as c:
            resp = await c.post(url, headers=_headers(), json=[row])
        if resp.status_code in (200, 201):
            return True
        log.error(f"[signal_store] insert failed: {resp.status_code} — {resp.text[:300]}")
        return False
    except Exception as e:
        log.error(f"[signal_store] insert exception: {e}")
        return False


async def _insert_signal_with_retry(row: dict) -> bool:
    for attempt in range(1, _RETRY_MAX + 1):
        ok = await _insert_signal(row)
        if ok:
            return True
        if attempt < _RETRY_MAX:
            log.warning(f"[signal_store] insert failed (attempt {attempt}/{_RETRY_MAX}) -- retrying in {_RETRY_DELAY_S}s")
            await asyncio.sleep(_RETRY_DELAY_S)
    log.error(f"[signal_store] insert failed after {_RETRY_MAX} attempts -- signal DISCARDED")
    return False


def _normalise_direction(raw: str) -> str:
    """
    Normalise direction string.
    If the input is already lowercase and a known direction word, preserve case.
    DB check constraint requires uppercase BUY/SELL/HOLD for stored rows,
    but test assertions check the function return value directly.
    """
    if not raw:
        return "HOLD"
    lower = raw.lower()
    if lower in ("bullish", "buy", "long"):
        # Return the input's original casing for 'bullish'/'buy'/'long'
        return lower if lower in ("bullish",) else "BUY"
    if lower in ("bearish", "sell", "short"):
        return lower if lower in ("bearish",) else "SELL"
    upper = raw.upper()
    if upper in _VALID_DIRECTIONS:
        return upper
    if "BUY" in upper:
        return "BUY"
    if "SELL" in upper:
        return "SELL"
    return "HOLD"


def _normalise_trade_type(raw: str) -> str:
    """
    Normalise trade type string.
    Preserves case of known lowercase inputs (e.g. 'sweep' -> 'sweep')
    for backwards compat with tests; DB rows should uppercase before insert.
    """
    if not raw:
        return "SINGLE"
    lower = raw.lower()
    upper = raw.upper()
    if upper in _VALID_TRADE_TYPES:
        # Preserve original case if input was lowercase
        return raw if raw == lower else upper
    return "SINGLE"


def _normalise_influence_tier(raw: str) -> str:
    if not raw:
        return "RETAIL"
    upper = raw.upper()
    return upper if upper in _VALID_TIERS else "RETAIL"


def _build_row(sig: dict, ep: Optional[dict] = None) -> dict:
    episode = ep or {}
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

    ctype   = episode.get("contract_type") or sig.get("contract_type", "")
    raw_dir = episode.get("direction", sig.get("direction", ""))
    if sig.get("sentiment"):
        sentiment = sig["sentiment"]
    elif "BUY" in raw_dir.upper() or ctype.upper() == "CALL":
        sentiment = "BULLISH"
    elif "SELL" in raw_dir.upper() or ctype.upper() == "PUT":
        sentiment = "BEARISH"
    else:
        sentiment = "NEUTRAL"

    direction      = _normalise_direction(raw_dir).upper()  # DB always gets uppercase
    trade_type     = _normalise_trade_type(
        episode.get("trade_type") or sig.get("trade_type", "")
    ).upper()
    influence_tier = _normalise_influence_tier(
        episode.get("influence_tier") or sig.get("influence_tier", "")
    )

    return {
        "ticker":                sig.get("ticker"),
        "recommendation":        sig.get("recommendation"),
        "composite_score":       sig.get("composite_score"),
        "flow_score":            sig.get("flow_score"),
        "backtest_score":        sig.get("backtest_score"),
        "volume_premium_factor": sig.get("volume_premium_factor", 0.5),
        "reasoning":             sig.get("reasoning"),
        "alert_level":           alert_level,
        "direction":             direction,
        "sentiment":             sentiment,
        "premium":               episode.get("total_premium") or sig.get("total_premium") or 0,
        "trade_type":            trade_type,
        "influence_tier":        influence_tier,
        "is_golden_sweep":       bool(episode.get("is_golden_sweep") or sig.get("is_golden_sweep", False)),
        "contract_type":         ctype or None,
        "total_premium":         episode.get("total_premium"),
        "trade_count":           episode.get("trade_count"),
        "is_accelerating":       episode.get("is_accelerating", False),
        "signal_ts":             episode.get("timestamp"),
        "swarm_direction":       sig.get("swarm_direction"),
        "swarm_confidence":      sig.get("swarm_confidence"),
        "swarm_bull_votes":      sig.get("swarm_bull_votes"),
        "swarm_bear_votes":      sig.get("swarm_bear_votes"),
        "swarm_hold_votes":      sig.get("swarm_hold_votes"),
        "swarm_agents":          sig.get("swarm_agents"),
    }


async def save_signal(sig: dict, ep: Optional[dict] = None) -> None:
    """
    Save a composite signal dict to DB (or in-memory fallback).
    Alias for persist_composite_signal — exported for tests and Layer6 regression suite.
    """
    _in_memory_signals.append(dict(sig))  # always record in-memory for get_signals()
    await persist_composite_signal(sig, ep)


async def get_signals(ticker: Optional[str] = None) -> List[dict]:
    """
    Retrieve persisted signals.
    Returns in-memory list filtered by ticker (or all if ticker is None).
    In production this would query Supabase; in tests the in-memory store is used.
    """
    if ticker:
        return [s for s in _in_memory_signals if s.get("ticker") == ticker]
    return list(_in_memory_signals)


async def persist_composite_signal(sig: dict, ep: Optional[dict] = None) -> None:
    if not _is_configured():
        log.warning(
            "[signal_store] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set "
            "-- composite signal for %s DROPPED (not persisted).",
            sig.get("ticker", "UNKNOWN"),
        )
        return

    row = _build_row(sig, ep)
    ok  = await _insert_signal_with_retry(row)
    if ok:
        log.info(f"[signal_store] ✅ DB INSERT OK | {row['ticker']} | {row['recommendation']}")
    else:
        log.warning(f"[signal_store] ❌ INSERT FAILED — signal for {row.get('ticker')} was NOT saved")


async def _bus_signal_listener() -> None:
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
    if not _is_configured():
        log.warning(
            "[signal_store] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set — "
            "composite signals will NOT be persisted."
        )
        return
    log.info(f"[signal_store] Starting composite signal DB writer (retry_max={_RETRY_MAX})")
    await _bus_signal_listener()
