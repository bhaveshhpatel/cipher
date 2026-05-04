"""
signal_store.py — Supabase DB writer for composite signals.

Phase 5A changes:
  - _build_row() now persists swarm fields:
    swarm_direction, swarm_confidence, swarm_agents (JSONB),
    swarm_bull_votes, swarm_bear_votes, swarm_hold_votes

Fix 3 (2026-04-26):
  - _signal_memory is now a collections.deque(maxlen=1000) to prevent
    unbounded memory growth in long-running Railway processes.

Fix 5 (2026-04-26):
  - persist_composite_signal() now falls back to _signal_memory when
    Supabase is not configured (matches save_signal() behaviour).

Fix 6 (2026-05-04):
  - _build_row() now normalises `sentiment` through _normalise_sentiment()
    before inserting, preventing 23514 CHECK violations when upstream emits
    lowercase/mixed-case values (e.g. "bullish", "Bearish") or unexpected
    values like "STRONG_BULLISH".
  - _build_row() now validates `alert_level` through _normalise_alert_level()
    before inserting, preventing 23514 CHECK violations if sig["alert_level"]
    carries a value outside the live DB constraint (e.g. "NORMAL").
  - Root cause confirmed by schema inspection of cipher-database (2026-05-04):
      signal_feed_log_sentiment_check:   BULLISH | BEARISH | NEUTRAL
      signal_feed_log_alert_level_check: CONVICTION | STRONG_SIGNAL | ALERT | WATCH

Public API (for tests):
  save_signal(signal: dict | object) -> bool
  get_signals(ticker: str | None, limit: int) -> list[dict]         [async]
  get_recent_signals(ticker: str | None, limit: int) -> list[dict]  [async alias]
  _client() -> Supabase-SDK-like | dict | None  (patchable)
  _clear_signal_memory() -> None               (test isolation helper)

Normalisation helpers:
  _normalise_direction(raw) -> 'bullish' | 'bearish' | 'neutral'
  _normalise_trade_type(raw) -> 'sweep' | 'block' | 'split' | 'single'
  _normalise_sentiment(raw) -> 'BULLISH' | 'BEARISH' | 'NEUTRAL'
  _normalise_alert_level(raw) -> 'CONVICTION' | 'STRONG_SIGNAL' | 'ALERT' | 'WATCH'

CI / no-network behaviour:
  When Supabase is configured but the host is unreachable (DNS -2 in CI),
  save_signal() falls back to _signal_memory after all retries fail so
  that get_signals() can still return the saved data within the same process.
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

# These sets mirror the live DB CHECK constraints exactly.
# Update here if and only if the corresponding migration alters the constraint.
_VALID_DIRECTIONS   = {"BUY", "SELL", "HOLD"}
_VALID_TRADE_TYPES  = {"SWEEP", "BLOCK", "SPLIT", "SINGLE"}
_VALID_TIERS        = {"WHALE", "INSTITUTIONAL", "LARGE", "RETAIL"}
_VALID_SENTIMENTS   = {"BULLISH", "BEARISH", "NEUTRAL"}           # signal_feed_log_sentiment_check
_VALID_ALERT_LEVELS = {"CONVICTION", "STRONG_SIGNAL", "ALERT", "WATCH"}  # signal_feed_log_alert_level_check

_RETRY_MAX     = 3
_RETRY_DELAY_S = 1.0

# In-memory signal store — bounded deque prevents unbounded memory growth.
# maxlen=1000 keeps ~16 mins of 1/s signals; oldest entries auto-evict.
_signal_memory: deque = deque(maxlen=1000)
# Dedup set — keyed by signal 'id' to prevent duplicates in _signal_memory
_signal_ids_seen: set = set()


def _clear_signal_memory() -> None:
    """Reset in-memory signal store and dedup set. Used by test fixtures for isolation."""
    global _signal_memory, _signal_ids_seen
    _signal_memory = deque(maxlen=1000)
    _signal_ids_seen = set()


def _is_configured() -> bool:
    return bool(_SUPABASE_URL and _SUPABASE_KEY)


def _client():  # noqa: ANN201 — patchable by tests
    """
    Returns None when unconfigured.
    Returns a Supabase SDK client when credentials are present.
    When patched in tests to return a mock with .table(), that mock is used
    directly by save_signal / get_recent_signals for insert/select.
    """
    if not _is_configured():
        return None
    return {"url": _SUPABASE_URL, "key": _SUPABASE_KEY}


def _headers() -> dict:
    return {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }


def _client_is_sdk_mock(client) -> bool:
    """True when the client has a .table() method (Supabase SDK or test mock)."""
    return client is not None and hasattr(client, "table")


async def _insert_via_sdk(client, row: dict) -> bool:
    """Insert a row using the Supabase SDK (or a compatible test mock)."""
    try:
        t = client.table(_TABLE)
        t.insert(row).execute()
        return True
    except Exception as e:
        log.error("[signal_store] SDK insert exception: %s", e)
        return False


async def _insert_signal(row: dict) -> bool:
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return False
    url = f"{_SUPABASE_URL}/rest/v1/{_TABLE}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=_headers(), json=[row])
        if resp.status_code in (200, 201):
            return True
        log.error("[signal_store] insert failed: %s -- %s", resp.status_code, resp.text[:300])
        return False
    except Exception as e:
        log.error("[signal_store] insert exception: %s", e)
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


def _normalise_sentiment(raw: str) -> str:
    """
    Normalise raw sentiment to a value accepted by signal_feed_log_sentiment_check.
    Handles lowercase, mixed-case, and aliased values from upstream emitters.
    Unknown values fall back to 'NEUTRAL'.
    """
    if not raw:
        return "NEUTRAL"
    upper = raw.upper()
    if upper in _VALID_SENTIMENTS:
        return upper
    # Common upstream aliases
    if upper in ("BULL", "BULLISH_STRONG", "STRONG_BULLISH", "BUY"):
        return "BULLISH"
    if upper in ("BEAR", "BEARISH_STRONG", "STRONG_BEARISH", "SELL"):
        return "BEARISH"
    log.warning("[signal_store] unknown sentiment value %r -- defaulting to NEUTRAL", raw)
    return "NEUTRAL"


def _normalise_alert_level(raw: str) -> str:
    """
    Validate alert_level against signal_feed_log_alert_level_check.
    If the value is not in the live constraint, fall back to 'WATCH'.
    This guards against upstream passing stale values like 'NORMAL'
    (the column default pre-migration) or any other out-of-range string.
    """
    if not raw:
        return "WATCH"
    upper = raw.upper()
    if upper in _VALID_ALERT_LEVELS:
        return upper
    log.warning("[signal_store] unknown alert_level value %r -- defaulting to WATCH", raw)
    return "WATCH"


def _db_direction(raw: str) -> str:
    normalised = _normalise_direction(raw)
    mapping = {"bullish": "BUY", "bearish": "SELL"}
    return mapping.get(normalised, "HOLD")


def _db_trade_type(raw: str) -> str:
    return _normalise_trade_type(raw).upper()


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


def _build_row(sig, ep: Optional[dict] = None) -> dict:
    sig = _coerce_to_dict(sig)
    episode = ep or {}

    score = sig.get("composite_score") or 0.0

    # Derive alert_level from score first, then validate whatever came from sig
    # through _normalise_alert_level to guard against out-of-constraint values.
    if sig.get("alert_level"):
        alert_level = _normalise_alert_level(sig["alert_level"])
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

    # Fix 6: route through _normalise_sentiment — never pass raw sig["sentiment"]
    # directly to the DB row. Raw passthrough was the source of 23514 violations.
    if sig.get("sentiment"):
        sentiment = _normalise_sentiment(sig["sentiment"])
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


def _store_in_memory(sig_dict: dict) -> None:
    """Append to _signal_memory respecting dedup by 'id'."""
    sig_id = sig_dict.get("id")
    if sig_id is None or sig_id not in _signal_ids_seen:
        _signal_memory.append(sig_dict)
        if sig_id is not None:
            _signal_ids_seen.add(sig_id)


async def save_signal(signal) -> bool:
    sig_dict = _coerce_to_dict(signal)
    client = _client()

    if _client_is_sdk_mock(client):
        ok = await _insert_via_sdk(client, sig_dict)
        if ok:
            _store_in_memory(sig_dict)
        return ok

    if not _is_configured():
        _store_in_memory(sig_dict)
        return True

    row = _build_row(signal)
    ok  = await _insert_signal_with_retry(row)
    _store_in_memory(sig_dict)
    return ok


async def get_signals(ticker: Optional[str] = None, limit: int = 50) -> list:
    client = _client()
    if _client_is_sdk_mock(client):
        try:
            t      = client.table(_TABLE)
            result = t.select("*").order("id", desc=True).limit(limit).execute()
            rows   = getattr(result, "data", None) or []
            if ticker:
                rows = [r for r in rows if r.get("ticker") == ticker]
            return rows
        except Exception:
            pass

    results = list(_signal_memory)
    if ticker:
        results = [s for s in results if s.get("ticker") == ticker]
    return results[-limit:]


async def get_recent_signals(ticker: Optional[str] = None, limit: int = 50) -> list:
    """Alias for get_signals — backward-compat."""
    return await get_signals(ticker=ticker, limit=limit)


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
        premium_val  = row["premium"]
        premium_fmt  = "${:,.0f}".format(premium_val) if premium_val else "$0"
        golden_sweep = row["is_golden_sweep"]
        golden_tag   = " \u26a1 GOLDEN SWEEP" if golden_sweep else ""
        swarm_dir    = row["swarm_direction"] or "--"
        bull         = row["swarm_bull_votes"]
        bear         = row["swarm_bear_votes"]
        hold         = row["swarm_hold_votes"]
        log.info(
            "[signal_store] DB INSERT OK | "
            "%s | %s | dir=%s | score=%.3f | flow=%.3f | alert=%s | "
            "sentiment=%s | tier=%s | type=%s | premium=%s | "
            "swarm=%s (%sB/%sBe/%sH)%s",
            row["ticker"], row["recommendation"], row["direction"],
            row["composite_score"], row["flow_score"], row["alert_level"],
            row["sentiment"], row["influence_tier"], row["trade_type"],
            premium_fmt, swarm_dir, bull, bear, hold, golden_tag,
        )
    else:
        log.warning(
            "[signal_store] INSERT FAILED -- signal for %s was NOT saved to DB",
            row.get("ticker"),
        )


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
