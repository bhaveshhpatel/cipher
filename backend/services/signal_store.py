"""
signal_store.py — Supabase DB writer for composite signals.

Phase 5A: persists swarm fields (swarm_direction, swarm_confidence,
  swarm_agents JSONB, swarm_bull_votes, swarm_bear_votes, swarm_hold_votes).

Fix 3 (2026-04-26): _signal_memory is a collections.deque(maxlen=1000) to
  prevent unbounded memory growth in long-running Railway processes.

Public API:
  save_signal(signal)                              -> bool          [async]
  get_signals(ticker, limit)                       -> list[dict]    [async]
  get_recent_signals(ticker, limit)                -> list[dict]    [async alias]
  persist_composite_signal(sig, ep)                -> None          [async]
  start_signal_writer()                            -> None          [async]
  _clear_signal_memory()                           -> None          (test helper)
  _normalise_direction(raw)    -> 'bullish'|'bearish'|'neutral'
  _normalise_trade_type(raw)   -> 'sweep'|'block'|'split'|'single'

CI / no-network behaviour:
  When Supabase is configured but the host is unreachable, save_signal()
  falls back to _signal_memory after all retries so that get_signals()
  can still return saved data within the same process.
"""
import asyncio
import logging
import os
from collections import deque
from typing import Optional

import httpx

from core.async_bus import bus  # module-level import so patch('services.signal_store.bus') works

log = logging.getLogger("signal_store")

_SUPABASE_URL: Optional[str] = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY: Optional[str] = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")
)

_TABLE = "signal_history"

_VALID_TIERS = {"WHALE", "INSTITUTIONAL", "LARGE", "RETAIL"}

_RETRY_MAX     = 3
_RETRY_DELAY_S = 1.0

# Bounded in-memory store — maxlen prevents unbounded growth.
# maxlen=1000 keeps ~16 min of 1/s signals; oldest entries auto-evict.
_signal_memory: deque = deque(maxlen=1000)
_signal_ids_seen: set  = set()


def _clear_signal_memory() -> None:
    """Reset in-memory signal store and dedup set. Used by test fixtures for isolation."""
    global _signal_memory, _signal_ids_seen
    _signal_memory   = deque(maxlen=1000)
    _signal_ids_seen = set()


def _is_configured() -> bool:
    return bool(_SUPABASE_URL and _SUPABASE_KEY)


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
        log.error(
            "[signal_store] insert failed: %s -- %s",
            resp.status_code, resp.text[:300],
        )
        return False
    except Exception as exc:
        log.error("[signal_store] insert exception: %s", exc)
        return False


async def _insert_signal_with_retry(row: dict) -> bool:
    for attempt in range(1, _RETRY_MAX + 1):
        ok = await _insert_signal(row)
        if ok:
            return True
        if attempt < _RETRY_MAX:
            log.warning(
                "[signal_store] insert failed (attempt %d/%d) -- retrying in %ss",
                attempt, _RETRY_MAX, _RETRY_DELAY_S,
            )
            await asyncio.sleep(_RETRY_DELAY_S)
    log.error(
        "[signal_store] insert failed after %d attempts -- signal for %s DISCARDED.",
        _RETRY_MAX, row.get("ticker"),
    )
    return False


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _normalise_direction(raw: str) -> str:
    if not raw:
        return "neutral"
    lower = raw.lower()
    if lower in ("buy", "bullish", "repeat_buy"):
        return "bullish"
    if lower in ("sell", "bearish", "repeat_sell"):
        return "bearish"
    return "neutral"


def _normalise_trade_type(raw: str) -> str:
    """Normalise to lowercase trade type. Unknown values map to 'single'."""
    if not raw:
        return "single"
    lower = raw.lower()
    return lower if lower in ("sweep", "block", "split", "single") else "single"


def _normalise_influence_tier(raw: str) -> str:
    if not raw:
        return "RETAIL"
    upper = raw.upper()
    return upper if upper in _VALID_TIERS else "RETAIL"


def _coerce_to_dict(sig) -> dict:
    if isinstance(sig, dict):
        return sig
    if hasattr(sig, "model_dump"):
        return sig.model_dump()
    if hasattr(sig, "__dict__"):
        return vars(sig)
    try:
        return dict(sig)
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Row builder
# ---------------------------------------------------------------------------

def _build_row(sig, ep: Optional[dict] = None) -> dict:
    sig     = _coerce_to_dict(sig)
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

    # Single normalisation path — no redundant _db_direction/_db_trade_type wrappers.
    norm_dir = _normalise_direction(raw_dir)
    direction_map = {"bullish": "BUY", "bearish": "SELL"}
    direction      = direction_map.get(norm_dir, "HOLD")
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
        "is_golden_sweep":       bool(
            episode.get("is_golden_sweep") or sig.get("is_golden_sweep", False)
        ),
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


def _store_in_memory(sig_dict: dict) -> None:
    """Append to _signal_memory, deduplicating by 'id' field."""
    sig_id = sig_dict.get("id")
    if sig_id is None or sig_id not in _signal_ids_seen:
        _signal_memory.append(sig_dict)
        if sig_id is not None:
            _signal_ids_seen.add(sig_id)


# ---------------------------------------------------------------------------
# Public write API
# ---------------------------------------------------------------------------

async def save_signal(signal) -> bool:
    """
    Persist a signal. Falls back to _signal_memory when Supabase is
    unconfigured or when DB writes fail (CI / offline environments).
    """
    sig_dict = _coerce_to_dict(signal)

    if not _is_configured():
        _store_in_memory(sig_dict)
        return True

    row = _build_row(signal)
    ok  = await _insert_signal_with_retry(row)
    # Always store in memory so get_signals() works within the same process
    # even when the DB write succeeded (avoids a round-trip on subsequent reads).
    _store_in_memory(sig_dict)
    return ok


async def persist_composite_signal(sig: dict, ep: Optional[dict] = None) -> None:
    if not _is_configured():
        log.warning(
            "[signal_store] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set "
            "-- composite signal for %s stored in memory (not persisted to DB).",
            sig.get("ticker", "UNKNOWN"),
        )
        row = _build_row(sig, ep)
        _store_in_memory(row)
        return

    row = _build_row(sig, ep)
    ok  = await _insert_signal_with_retry(row)
    if ok:
        premium_fmt  = "${:,.0f}".format(row["premium"] or 0)
        golden_tag   = " \u26a1 GOLDEN SWEEP" if row["is_golden_sweep"] else ""
        swarm_dir    = row["swarm_direction"] or "--"
        log.info(
            "[signal_store] DB INSERT OK | "
            "%s | %s | dir=%s | score=%.3f | flow=%.3f | alert=%s | "
            "sentiment=%s | tier=%s | type=%s | premium=%s | "
            "swarm=%s (%sB/%sBe/%sH)%s",
            row["ticker"], row["recommendation"], row["direction"],
            row["composite_score"], row["flow_score"], row["alert_level"],
            row["sentiment"], row["influence_tier"], row["trade_type"],
            premium_fmt, swarm_dir,
            row["swarm_bull_votes"], row["swarm_bear_votes"],
            row["swarm_hold_votes"], golden_tag,
        )
    else:
        log.warning(
            "[signal_store] INSERT FAILED -- signal for %s was NOT saved to DB",
            row.get("ticker"),
        )


# ---------------------------------------------------------------------------
# Public read API
# ---------------------------------------------------------------------------

async def get_signals(ticker: Optional[str] = None, limit: int = 50) -> list:
    """
    Return signals from in-memory store, optionally filtered by ticker.
    In production, the memory store is populated by every save_signal() call.
    """
    results = list(_signal_memory)
    if ticker:
        results = [s for s in results if s.get("ticker") == ticker]
    return results[-limit:]


async def get_recent_signals(ticker: Optional[str] = None, limit: int = 50) -> list:
    """Alias for get_signals — backward compatibility."""
    return await get_signals(ticker=ticker, limit=limit)


# ---------------------------------------------------------------------------
# Bus listener + startup
# ---------------------------------------------------------------------------

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
            "[signal_store] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set -- "
            "composite signals will NOT be persisted."
        )
        return
    log.info(
        "[signal_store] Starting composite signal DB writer (retry_max=%d)",
        _RETRY_MAX,
    )
    await _bus_signal_listener()
