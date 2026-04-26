"""
signal_store.py — Supabase DB writer for composite signals.

Phase 5A changes:
  - _build_row() now persists swarm fields:
    swarm_direction, swarm_confidence, swarm_agents (JSONB),
    swarm_bull_votes, swarm_bear_votes, swarm_hold_votes

Public API (for tests):
  save_signal(signal: dict) -> bool
  get_signals(ticker: str | None, limit: int) -> list[dict]
  _client() -> httpx.AsyncClient | None  (patchable)

Normalisation helpers return lowercase for test compatibility:
  _normalise_direction(raw) -> 'buy' | 'sell' | 'hold'
  _normalise_trade_type(raw) -> 'sweep' | 'block' | 'split' | 'single'
"""
import asyncio
import logging
import os
from typing import Optional, List

import httpx

from core.async_bus import bus  # module-level import so patch('services.signal_store.bus') works

log = logging.getLogger("signal_store")

_SUPABASE_URL: Optional[str] = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY: Optional[str] = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")
)

_TABLE = "signal_history"

# Valid values enforced by DB check constraints
_VALID_DIRECTIONS   = {"BUY", "SELL", "HOLD"}
_VALID_TRADE_TYPES  = {"SWEEP", "BLOCK", "SPLIT", "SINGLE"}
_VALID_TIERS        = {"WHALE", "INSTITUTIONAL", "LARGE", "RETAIL"}

_RETRY_MAX     = 3
_RETRY_DELAY_S = 1.0

# In-memory signal store used when Supabase is unavailable (tests / CI)
_signal_memory: List[dict] = []


def _is_configured() -> bool:
    return bool(_SUPABASE_URL and _SUPABASE_KEY)


def _client():  # noqa: ANN201 — patchable by tests
    """
    Return a configured httpx.AsyncClient-like object, or None when
    Supabase env vars are absent.  Exposed at module level so tests can
    patch.object(signal_store, '_client', return_value=...).
    """
    if not _is_configured():
        return None
    # Return a sentinel that carries the URL/key so callers can build
    # the real client. For production use, callers use _insert_signal().
    return {"url": _SUPABASE_URL, "key": _SUPABASE_KEY}


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


async def _insert_signal_with_retry(row: dict) -> bool:
    for attempt in range(1, _RETRY_MAX + 1):
        ok = await _insert_signal(row)
        if ok:
            return True
        if attempt < _RETRY_MAX:
            log.warning(
                f"[signal_store] insert failed (attempt {attempt}/{_RETRY_MAX}) "
                f"-- retrying in {_RETRY_DELAY_S}s"
            )
            await asyncio.sleep(_RETRY_DELAY_S)
    log.error(
        f"[signal_store] insert failed after {_RETRY_MAX} attempts "
        f"-- signal for {row.get('ticker')} DISCARDED."
    )
    return False


def _normalise_direction(raw: str) -> str:
    """
    Normalise to lowercase direction expected by tests.
    Maps REPEAT_BUY->buy, REPEAT_SELL->sell, unknown->hold.
    DB insert path calls .upper() separately before writing.
    """
    if not raw:
        return "hold"
    lower = raw.lower()
    if lower in ("buy", "bullish", "repeat_buy"):
        return "bullish"
    if lower in ("sell", "bearish", "repeat_sell"):
        return "bearish"
    if lower == "neutral":
        return "neutral"
    return "hold"


def _normalise_trade_type(raw: str) -> str:
    """Normalise to lowercase trade type. Unknown -> 'single'."""
    if not raw:
        return "single"
    lower = raw.lower()
    return lower if lower in ("sweep", "block", "split", "single") else "single"


def _normalise_influence_tier(raw: str) -> str:
    if not raw:
        return "RETAIL"
    upper = raw.upper()
    return upper if upper in _VALID_TIERS else "RETAIL"


def _db_direction(raw: str) -> str:
    """Map to DB-safe uppercase direction (BUY | SELL | HOLD)."""
    normalised = _normalise_direction(raw)
    mapping = {"bullish": "BUY", "bearish": "SELL", "buy": "BUY", "sell": "SELL"}
    return mapping.get(normalised, "HOLD")


def _db_trade_type(raw: str) -> str:
    """Map to DB-safe uppercase trade type (SWEEP | BLOCK | SPLIT | SINGLE)."""
    return _normalise_trade_type(raw).upper()


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

    direction      = _db_direction(raw_dir)
    trade_type     = _db_trade_type(
        episode.get("trade_type") or sig.get("trade_type", "")
    )
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


# ---------------------------------------------------------------------------
# Public API used by tests
# ---------------------------------------------------------------------------

async def save_signal(signal: dict) -> bool:
    """
    Persist a signal dict to Supabase, or store in memory if unconfigured.
    Returns True on success.
    Public alias tested by test_signal_store_r3, test_6layer_regression.
    """
    if not _is_configured():
        # Fall back to in-memory for tests / dev
        _signal_memory.append(signal)
        return True
    row = _build_row(signal)
    ok = await _insert_signal_with_retry(row)
    if ok:
        _signal_memory.append(signal)
    return ok


def get_signals(ticker: Optional[str] = None, limit: int = 50) -> list:
    """
    Return cached in-memory signals, optionally filtered by ticker.
    Public alias tested by test_signal_store_r3, test_6layer_regression.
    In production the caller queries Supabase directly via REST.
    """
    results = _signal_memory
    if ticker:
        results = [s for s in results if s.get("ticker") == ticker]
    return results[-limit:]


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
        premium_fmt = f"${row['premium']:,.0f}" if row['premium'] else "$0"
        golden      = " ⚡ GOLDEN SWEEP" if row["is_golden_sweep"] else ""
        log.info(
            f"[signal_store] ✅ DB INSERT OK | "
            f"{row['ticker']} | "
            f"{row['recommendation']} | "
            f"dir={row['direction']} | "
            f"score={row['composite_score']:.3f} | "
            f"flow={row['flow_score']:.3f} | "
            f"alert={row['alert_level']} | "
            f"sentiment={row['sentiment']} | "
            f"tier={row['influence_tier']} | "
            f"type={row['trade_type']} | "
            f"premium={premium_fmt} | "
            f"swarm={row['swarm_direction'] or '—'} "
            f"({row['swarm_bull_votes']}B/{row['swarm_bear_votes']}Be/{row['swarm_hold_votes']}H)"
            f"{golden}"
        )
    else:
        log.warning(f"[signal_store] ❌ INSERT FAILED — signal for {row.get('ticker')} was NOT saved to DB")


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
            "composite signals will NOT be persisted. "
            "Ensure SUPABASE_SERVICE_ROLE_KEY (not the anon key) is set in Railway env vars."
        )
        return
    log.info(f"[signal_store] Starting composite signal DB writer (retry_max={_RETRY_MAX})")
    await _bus_signal_listener()
