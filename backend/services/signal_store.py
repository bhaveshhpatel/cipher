"""
signal_store.py — Supabase DB writer for composite signals.

Rearch-006 (2026-05-12):
  - Imported get_engine from signal_engine.
  - _bus_signal_listener now calls get_engine().evaluate_episode(ep) before
    any DB write.  If result.passed is False, the episode is logged at INFO
    and discarded — no signal_history row is written.
  - _build_row() alert_level is now sourced from the EpisodeEvalResult
    returned by the engine rather than being score-derived heuristically.
    The engine is the single source of truth for alert level from this point.
  - persist_composite_signal() gains an optional EpisodeEvalResult parameter
    so callers that already have the eval result can pass it in and avoid a
    second engine evaluation.
  - Fix E-18/E-19: GATE FAIL / GATE PASS log lines used %,.0f which is
    f-string/str.format syntax and raises ValueError in %-style logging.
    Premium is now pre-formatted as an f-string and passed as %s, matching
    the existing pattern in persist_composite_signal().

Rearch-010 (2026-05-09) — schema purge pass 2:
  - Removed flow_score from _build_row() (column dropped in migration 024;
    was a write-only orphan — never read by any SELECT in any router or service).
    composite_score is now the sole score surface per REARCH-010.
  - Removed flow=%.3f log interpolation from persist_composite_signal() log
    line to match (would have raised KeyError post-migration).
  - Removed backtest_score from _build_row() (column retired, not in signal_history).
  - Removed volume_premium_factor from _build_row() (column dropped in migration 024).
  - Updated _VALID_ALERT_LEVELS to REARCH vocab: WATCH | NOTEWORTHY | BLOCK | GOLDEN
    (migration 024 replaces old CONVICTION|WHALE|INSTITUTIONAL|LARGE|RETAIL constraint).
  - Updated _VALID_DIRECTIONS to REARCH vocab: BULLISH | BEARISH | NEUTRAL
    (migration 024 replaces old BUY|SELL|HOLD constraint on signal_history.direction).
  - Updated _normalise_alert_level() — accepts new vocab; legacy bridge maps
    old tier names (CONVICTION/WHALE -> BLOCK, INSTITUTIONAL/LARGE -> NOTEWORTHY,
    RETAIL -> WATCH) so in-flight signals from un-redeployed callers don't 400.
  - Updated score branches in _build_row() to emit WATCH/NOTEWORTHY/BLOCK/GOLDEN.
  - Removed _db_direction() — direction column now stores BULLISH/BEARISH/NEUTRAL
    directly. _normalise_direction() output is uppercased and written as-is.

Rearch-010 (2026-05-09) — schema purge pass 1:
  - Removed swarm fields from _build_row(): swarm_direction, swarm_confidence,
    swarm_agents, swarm_bull_votes, swarm_bear_votes, swarm_hold_votes
    (columns dropped in migration 024).
  - Removed influence_tier from _build_row() (column dropped in migration 024).
  - Removed is_golden_sweep from _build_row() (column dropped in migration 024).
  - Removed _normalise_influence_tier() helper (no longer called).
  - Stripped swarm interpolation from persist_composite_signal log line.

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
    carries a value outside the live DB constraint.

Fix 7 (2026-05-04):
  - Aligned _VALID_ALERT_LEVELS, _build_row() score branches, and
    _normalise_alert_level() to the corrected DB constraint vocabulary.
    (Superseded by Rearch-010 pass 2 above — vocab updated again to REARCH set.)

QA-3 (ING-007 pre-merge 2026-05-06):
  - is_multi_day_repeat is present in the bus signal payload
    (data.signal.is_multi_day_repeat) as of ING-007. _build_row() does NOT
    read or forward this key to signal_history because the column does not
    exist in signal_history yet. This is intentional — the column migration
    is gated on ING-009 merge. _build_row() only reads explicit keys via
    sig.get()/ep.get(), so unknown keys are silently ignored and the
    Supabase REST insert succeeds cleanly. No write error occurs.
    When ING-009 lands and the column migration runs, add
    "is_multi_day_repeat": ep.get("is_multi_day_repeat", False)
    to the _build_row() return dict.

Public API (for tests):
  save_signal(signal: dict | object) -> bool
  get_signals(ticker: str | None, limit: int) -> list[dict]         [async]
  get_recent_signals(ticker: str | None, limit: int) -> list[dict]  [async alias]
  _client() -> Supabase-SDK-like | dict | None  (patchable)
  _clear_signal_memory() -> None               (test isolation helper)

Normalisation helpers:
  _normalise_direction(raw) -> 'BULLISH' | 'BEARISH' | 'NEUTRAL'
  _normalise_trade_type(raw) -> 'sweep' | 'block' | 'split' | 'single'
  _normalise_sentiment(raw) -> 'BULLISH' | 'BEARISH' | 'NEUTRAL'
  _normalise_alert_level(raw) -> 'WATCH' | 'NOTEWORTHY' | 'BLOCK' | 'GOLDEN'

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
from services.signal_engine import EpisodeEvalResult, get_engine

log = logging.getLogger("signal_store")

_SUPABASE_URL: Optional[str] = os.environ.get("SUPABASE_URL")
_SUPABASE_KEY: Optional[str] = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")
)

_TABLE = "signal_history"

# These sets mirror the live DB CHECK constraints after migration 024.
# Update here if and only if the corresponding migration alters the constraint.
_VALID_DIRECTIONS   = {"BULLISH", "BEARISH", "NEUTRAL"}                              # rearch-010: was BUY|SELL|HOLD
_VALID_TRADE_TYPES  = {"SWEEP", "BLOCK", "SPLIT", "SINGLE"}
_VALID_SENTIMENTS   = {"BULLISH", "BEARISH", "NEUTRAL"}                              # signal_history_sentiment_check
_VALID_ALERT_LEVELS = {"WATCH", "NOTEWORTHY", "BLOCK", "GOLDEN"}                    # rearch-010: was CONVICTION|WHALE|INSTITUTIONAL|LARGE|RETAIL

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
    """
    Normalise direction to REARCH vocab: BULLISH | BEARISH | NEUTRAL.
    Handles legacy BUY/SELL/HOLD values from in-flight callers pre-redeploy.
    """
    if not raw:
        return "NEUTRAL"
    upper = raw.upper()
    if upper in ("BULLISH", "BUY", "REPEAT_BUY"):
        return "BULLISH"
    if upper in ("BEARISH", "SELL", "REPEAT_SELL"):
        return "BEARISH"
    return "NEUTRAL"


def _normalise_trade_type(raw: str) -> str:
    """Normalise to lowercase trade type. Unknown -> 'single'."""
    if not raw:
        return "single"
    lower = raw.lower()
    return lower if lower in ("sweep", "block", "split", "single") else "single"


def _normalise_sentiment(raw: str) -> str:
    """
    Normalise raw sentiment to a value accepted by signal_history_sentiment_check.
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
    Validate alert_level against REARCH constraint (migration 024):
      WATCH | NOTEWORTHY | BLOCK | GOLDEN

    Legacy bridge: maps pre-REARCH tier names emitted by un-redeployed callers
    to the nearest REARCH equivalent so in-flight signals don't 400.
      CONVICTION / WHALE         -> BLOCK
      INSTITUTIONAL / LARGE      -> NOTEWORTHY
      RETAIL                     -> WATCH
      STRONG_SIGNAL / ALERT      -> NOTEWORTHY  (Fix 7 legacy vocab)
      WATCH (old Fix 7 fallback) -> WATCH        (coincidentally correct)
      NORMAL                     -> WATCH

    Unknown values fall back to 'WATCH'.
    """
    if not raw:
        return "WATCH"
    upper = raw.upper()
    if upper in _VALID_ALERT_LEVELS:
        return upper
    # Legacy bridge — pre-REARCH tier vocab
    _LEGACY_MAP = {
        "CONVICTION":    "BLOCK",
        "WHALE":         "BLOCK",
        "INSTITUTIONAL": "NOTEWORTHY",
        "LARGE":         "NOTEWORTHY",
        "RETAIL":        "WATCH",
        "STRONG_SIGNAL": "NOTEWORTHY",
        "ALERT":         "NOTEWORTHY",
        "NORMAL":        "WATCH",
    }
    if upper in _LEGACY_MAP:
        log.warning(
            "[signal_store] legacy alert_level value %r mapped to %r (rearch-010 bridge)",
            raw, _LEGACY_MAP[upper],
        )
        return _LEGACY_MAP[upper]
    log.warning("[signal_store] unknown alert_level value %r -- defaulting to WATCH", raw)
    return "WATCH"


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


def _build_row(
    sig,
    ep: Optional[dict] = None,
    eval_result: Optional[EpisodeEvalResult] = None,
) -> dict:
    """
    Build the signal_history INSERT dict from a signal and episode.

    REARCH-006: alert_level is now sourced from EpisodeEvalResult when
    provided.  This makes the signal engine the single authority for alert
    level — no more score-derived heuristics in the store layer.

    Falls back to score-derived heuristic when eval_result is None so that
    save_signal() (used by legacy callers and tests that bypass the bus) still
    works without requiring an EpisodeEvalResult.
    """
    sig = _coerce_to_dict(sig)
    episode = ep or {}

    score = sig.get("composite_score") or 0.0

    # REARCH-006: prefer engine output; fall back to score heuristic for
    # legacy/direct callers that don't go through the bus listener.
    if eval_result is not None:
        alert_level = eval_result.alert_level
    elif sig.get("alert_level"):
        alert_level = _normalise_alert_level(sig["alert_level"])
    elif score >= 0.85:
        alert_level = "BLOCK"
    elif score >= 0.70:
        alert_level = "BLOCK"
    elif score >= 0.55:
        alert_level = "NOTEWORTHY"
    else:
        alert_level = "WATCH"

    ctype   = episode.get("contract_type") or sig.get("contract_type", "")
    raw_dir = episode.get("direction", sig.get("direction", ""))

    # Fix 6: route through _normalise_sentiment — never pass raw sig["sentiment"] directly.
    if sig.get("sentiment"):
        sentiment = _normalise_sentiment(sig["sentiment"])
    elif "BUY" in raw_dir.upper() or "BULLISH" in raw_dir.upper() or ctype.upper() == "CALL":
        sentiment = "BULLISH"
    elif "SELL" in raw_dir.upper() or "BEARISH" in raw_dir.upper() or ctype.upper() == "PUT":
        sentiment = "BEARISH"
    else:
        sentiment = "NEUTRAL"

    # Rearch-010: direction column now stores BULLISH/BEARISH/NEUTRAL directly.
    # _normalise_direction() returns uppercase REARCH vocab; no secondary mapping needed.
    direction  = _normalise_direction(raw_dir)
    trade_type = _db_trade_type(
        episode.get("trade_type") or sig.get("trade_type", "")
    )

    # QA-3 note: is_multi_day_repeat (ING-007) is intentionally NOT included here.
    # The signal_history column does not exist until the ING-009-gated migration runs.
    # TODO(ING-009): add "is_multi_day_repeat": ep.get("is_multi_day_repeat", False)
    # to this return dict after the column migration lands.
    #
    # Rearch-010 removed columns (DO NOT re-add without a corresponding migration):
    #   backtest_score        — column retired, never existed in signal_history schema
    #   volume_premium_factor — dropped in migration 024
    #   flow_score            — dropped in migration 024 Section 6; write-only orphan
    #                           (written here, read by nothing). composite_score is
    #                           the sole score surface per REARCH-010.
    return {
        "ticker":          sig.get("ticker"),
        "recommendation":  sig.get("recommendation"),
        "composite_score": sig.get("composite_score"),
        "reasoning":       sig.get("reasoning"),
        "alert_level":     alert_level,
        "direction":       direction,
        "sentiment":       sentiment,
        "premium":         episode.get("total_premium") or sig.get("total_premium") or 0,
        "trade_type":      trade_type,
        "contract_type":   ctype or None,
        "total_premium":   episode.get("total_premium"),
        "trade_count":     episode.get("trade_count"),
        "is_accelerating": episode.get("is_accelerating", False),
        "signal_ts":       episode.get("timestamp"),
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


async def persist_composite_signal(
    sig: dict,
    ep: Optional[dict] = None,
    eval_result: Optional[EpisodeEvalResult] = None,
) -> None:
    """
    Persist a composite signal to signal_history.

    REARCH-006: accepts an optional EpisodeEvalResult so that
    _bus_signal_listener can pass the engine output through to _build_row()
    without re-evaluating.  When eval_result is None the alert_level falls
    back to the score-derived heuristic for backward compat.
    """
    if not _is_configured():
        log.warning(
            "[signal_store] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set "
            "-- composite signal for %s stored in memory (not persisted to DB).",
            sig.get("ticker", "UNKNOWN"),
        )
        row = _build_row(sig, ep, eval_result=eval_result)
        _store_in_memory(row)
        return

    row = _build_row(sig, ep, eval_result=eval_result)
    ok  = await _insert_signal_with_retry(row)
    if ok:
        premium_val = row["premium"]
        premium_fmt = "${:,.0f}".format(premium_val) if premium_val else "$0"
        log.info(
            "[signal_store] DB INSERT OK | "
            "%s | %s | dir=%s | score=%.3f | alert=%s | "
            "sentiment=%s | type=%s | premium=%s",
            row["ticker"], row["recommendation"], row["direction"],
            row["composite_score"] or 0.0, row["alert_level"],
            row["sentiment"], row["trade_type"], premium_fmt,
        )
    else:
        log.warning(
            "[signal_store] INSERT FAILED -- signal for %s was NOT saved to DB",
            row.get("ticker"),
        )


async def _bus_signal_listener() -> None:
    """
    Consume composite_signal events from the async bus.

    REARCH-006: Every episode is evaluated by SignalEngine before the DB
    write.  Episodes that fail any conviction dimension are logged and
    discarded — no signal_history row is written.

    Gate decision log format (INFO):
      [signal_store] GATE PASS  AAPL | alert=BLOCK | premium=$650,000 | trades=3
      [signal_store] GATE FAIL  AAPL | dims=[D2_ASK_SIDE, D5_REPETITION] | premium=$55,000
    """
    q = bus.subscribe("signal_writer")
    log.info("[signal_store] signal writer subscribed to bus")
    engine = get_engine()
    try:
        while True:
            msg = await q.get()
            if isinstance(msg, dict) and msg.get("type") == "composite_signal":
                data = msg.get("data", {})
                sig  = data.get("signal", {})
                ep   = data.get("episode", {})
                if not sig:
                    continue

                # REARCH-006: run conviction gate before any DB write
                result = engine.evaluate_episode(ep)

                if not result.passed:
                    # Pre-format premium — %,.0f is f-string syntax, invalid in %-style logging
                    premium_fmt = f"${result.premium:,.0f}"
                    log.info(
                        "[signal_store] GATE FAIL  %s | dims=%s | premium=%s",
                        result.ticker or sig.get("ticker", "UNKNOWN"),
                        result.failing_dimensions,
                        premium_fmt,
                    )
                    continue

                # Pre-format premium — %,.0f is f-string syntax, invalid in %-style logging
                premium_fmt = f"${result.premium:,.0f}"
                log.info(
                    "[signal_store] GATE PASS  %s | alert=%s | premium=%s | trades=%d",
                    result.ticker or sig.get("ticker", "UNKNOWN"),
                    result.alert_level,
                    premium_fmt,
                    int(ep.get("trade_count") or 0),
                )

                await persist_composite_signal(sig, ep, eval_result=result)
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
