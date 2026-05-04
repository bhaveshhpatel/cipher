"""
flow_store.py — Supabase DB writer for live options flow.

Subscribes to the async event bus and persists every classified
OptionsFlowEvent to `flow_events` and every signal episode to
`flow_episodes`.

Usage — call once at startup from main.py lifespan:

    from services.flow_store import start_flow_writer
    asyncio.create_task(start_flow_writer())

    # ING-007: also start the lookback queue worker:
    from services.flow_store import start_lookback_worker
    asyncio.create_task(start_lookback_worker())

Tables written:
  - flow_events   : one row per classified tick (batched every 500ms or 100 rows)
  - flow_episodes : one row per Signal Gate crossing, written directly from
                    _process_trade() in tradier_stream.py BEFORE the SIG-DEBOUNCE
                    check. This decouples episode persistence from the debounce
                    gate (which is only for WebSocket / signal_history anti-spam).

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

C-003 — Sweep Retroactive Upgrade:
  upgrade_to_sweep_in_db(occ_symbol, fill_price, size) issues a targeted
  PATCH (UPDATE) to flow_events setting trade_type='SWEEP' for rows that
  were written as 'BTO' before the sweep threshold was confirmed. Called
  from _process_trade() via asyncio.create_task() so it does not block
  the stream hot path.

ING-007 — Background Lookback Queue:
  _lookback_queue: asyncio.Queue (maxsize=5000) — receives ContractKey
  tuples from _process_trade() in tradier_stream.py. The queue worker
  (_lookback_worker) calls contract_day_cache.get_lookback() and writes
  results back onto the RepetitionEpisode in _accumulator.

  First-episode cold-cache behaviour:
    The first episode for a contract in a session enqueues its ContractKey
    before the cache entry exists. Until the queue worker completes the
    DB fetch, ep.prior_days_active = 0 and ep.is_multi_day_repeat = False.
    This is acceptable — is_multi_day_repeat is enrichment, not a gate
    (SA-Q1, 2026-05-04). The field self-corrects on subsequent ingest_tick
    calls once the cache is populated.

  Overflow behaviour:
    If the queue is full (500 unique contracts firing in rapid succession
    during a volatile session), put_nowait() raises asyncio.QueueFull.
    The caller in _process_trade() catches this and increments
    _stats["lookback_queue_overflow"]. The episode still produces and
    emits correctly — lookback enrichment is silently skipped for that tick.

Bug fixes applied:
  1. ALERT-LEVEL (2026-04-29): _bus_signal_listener was reading
     sig.get("alert_level") but the published signal dict uses key "alert".
     Fixed to try both keys with fallback to "WATCH".
  2. SWEEP-SQL (2026-04-29): upgrade_to_sweep_in_db was embedding the raw
     SQL expression "now()-interval 30 seconds" as a literal string in the
     PostgREST filter URL. PostgREST does not evaluate SQL expressions in
     filter values. Fixed to pre-compute a UTC ISO timestamp in Python.
  3. EPISODE-FIX (2026-04-30): flow_episodes were only written when the
     composite_signal bus event fired (after Signal Gate AND SIG-DEBOUNCE).
     This caused flow_episodes row count to equal signal_history row count.
     Additionally, _bus_signal_listener always wrote strike=None/expiry=None
     because composite_msg never included those fields.
     Fix: persist_flow_episode() is now called directly from _process_trade()
     after Signal Gate, before SIG-DEBOUNCE. _bus_signal_listener no longer
     writes flow_episodes — it is retained but acts as a no-op consumer of
     the db_writer channel for future use.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from core.async_bus import bus  # module-level import so patch('services.flow_store.bus') works

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

# ---------------------------------------------------------------------------
# ING-007: Background lookback queue
# ContractKey = (ticker, contract_type, strike, expiry) — 4-tuple (PBE, 2026-05-04)
# maxsize=5000 — overflow is caught and counted; never propagated (PBE-Q3)
# ---------------------------------------------------------------------------
_lookback_queue: asyncio.Queue = asyncio.Queue(maxsize=5000)

# Module-level stats — all keys initialised here so /health/stream never
# raises KeyError on cold start (Rule 6.4, STORY-STEPS_ING.md)
_stats: Dict[str, int] = {
    "lookback_queue_overflow": 0,
}


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


# ---------------------------------------------------------------------------
# ING-007: Lookback queue worker
# ---------------------------------------------------------------------------

async def _lookback_worker(accumulator) -> None:
    """
    ING-007: Background worker that drains _lookback_queue and enriches
    RepetitionEpisode objects with prior_days_active / prior_days_aggressive.

    Receives ContractKey tuples enqueued by _process_trade() in tradier_stream.py.
    Calls contract_day_cache.get_lookback() for each key, then writes results
    back onto the live RepetitionEpisode in the accumulator (if still present).

    accumulator: RepetitionAccumulator instance from tradier_stream module.
    This avoids a circular import — the caller (tradier_stream.py) passes
    its module-level accumulator reference at startup.

    The worker runs indefinitely until cancelled. It never raises — all
    exceptions are caught and logged so the worker cannot kill the event loop.
    """
    from utils.contract_day_cache import get_lookback, ContractKey  # local import avoids circular

    log.info("[flow_store] ING-007 lookback queue worker started")
    while True:
        try:
            key: ContractKey = await _lookback_queue.get()
            ticker, contract_type, strike, expiry = key

            # Resolve DTE-tier floor from the live episode if still present.
            episode_key = f"{ticker}:{contract_type}:{strike}:{expiry}"
            ep = accumulator._episodes.get(episode_key)
            min_premium = 0.0
            if ep is not None:
                min_premium = accumulator._get_episode_min_premium(ep)

            result = await get_lookback(key, min_premium)

            # Write back onto the episode if it is still active.
            if ep is not None:
                ep.prior_days_active     = result.prior_days_active
                ep.prior_days_aggressive = result.prior_days_aggressive
                ep.is_multi_day_repeat   = (
                    result.prior_days_active >= accumulator._multi_day_min_days
                )
                log.debug(
                    f"[flow_store] lookback enriched {ticker} {contract_type} "
                    f"{strike} {expiry}: active={result.prior_days_active} "
                    f"aggressive={result.prior_days_aggressive} "
                    f"multi_day={ep.is_multi_day_repeat}"
                )
            _lookback_queue.task_done()

        except asyncio.CancelledError:
            log.info("[flow_store] lookback worker cancelled")
            raise
        except Exception as exc:  # pragma: no cover
            log.warning(f"[flow_store] lookback worker exception (non-fatal): {exc}")
            try:
                _lookback_queue.task_done()
            except Exception:
                pass


async def start_lookback_worker(accumulator) -> None:
    """
    ING-007: Start the background lookback queue worker.
    Call once at startup from main.py lifespan alongside start_flow_writer().

        asyncio.create_task(start_lookback_worker(accumulator))

    accumulator: the RepetitionAccumulator instance from tradier_stream module.
    """
    await _lookback_worker(accumulator)


def enqueue_lookback(contract_key) -> None:
    """
    ING-007: Enqueue a ContractKey for async lookback enrichment.

    Called from _process_trade() in tradier_stream.py on the hot path.
    Uses put_nowait() — never blocks. On QueueFull, increments
    _stats["lookback_queue_overflow"] and returns silently. The episode
    still produces and emits correctly; lookback enrichment is skipped
    for this tick only.
    """
    try:
        _lookback_queue.put_nowait(contract_key)
    except asyncio.QueueFull:
        _stats["lookback_queue_overflow"] += 1
        log.debug("[flow_store] lookback_queue full — overflow +1")


def get_lookback_stats() -> Dict[str, int]:
    """Return a copy of the lookback stats dict for /health/stream."""
    return dict(_stats)


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


async def upgrade_to_sweep_in_db(occ_symbol: str, fill_price: float, size: int) -> bool:
    """
    C-003: Retroactively upgrade flow_events rows to trade_type='SWEEP'.

    Fix (2026-04-29): The previous implementation embedded the raw SQL
    expression "now()-interval 30 seconds" directly in the PostgREST
    filter URL. PostgREST treats filter values as literals, not SQL, so
    the expression was passed verbatim and Postgres rejected it with
    error code 22007 (invalid_datetime_format). Fixed by pre-computing
    the cutoff timestamp in Python and formatting it as ISO 8601.
    """
    if not _is_configured():
        log.debug("[flow_store] upgrade_to_sweep_in_db: not configured, skipping")
        return False

    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()

    url = (
        f"{_SUPABASE_URL}/rest/v1/flow_events"
        f"?occ_symbol=eq.{quote(occ_symbol)}"
        f"&fill_price=eq.{fill_price}"
        f"&size=eq.{size}"
        f"&trade_type=neq.SWEEP"
        f"&created_at=gte.{quote(cutoff)}"
    )
    headers = {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }
    payload = {"trade_type": "SWEEP"}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.patch(url, headers=headers, json=payload)
        if resp.status_code in (200, 204):
            log.info(
                f"[flow_store] sweep upgrade applied: {occ_symbol} "
                f"fill={fill_price} size={size}"
            )
            return True
        log.warning(
            f"[flow_store] sweep upgrade failed: {resp.status_code} -- {resp.text[:200]}"
        )
        return False
    except Exception as e:
        log.error(f"[flow_store] upgrade_to_sweep_in_db exception: {e}")
        return False


async def persist_flow_episode(signal_data: dict):
    """
    Write one row to flow_episodes.

    Called directly from _process_trade() in tradier_stream.py after the
    Signal Gate check and BEFORE the SIG-DEBOUNCE check. This ensures
    flow_episodes records every qualifying Signal Gate crossing, not just
    the subset that also clears the debounce gate.

    strike and expiry are populated from sig_ep fields (not from the
    composite_msg bus payload which never included them).
    """
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
            f"strike={row['strike']} expiry={row['expiry']} "
            f"alert={row['alert_level']} prem=${(row['total_premium'] or 0):,.0f}"
        )


async def _bus_signal_listener():
    """
    EPISODE-FIX (2026-04-30): flow_episodes are now written directly from
    _process_trade() before the SIG-DEBOUNCE gate. This listener is retained
    to consume the db_writer channel (preventing queue build-up) but no longer
    writes any rows itself.
    """
    q = bus.subscribe("db_writer")
    log.info("[flow_store] DB writer subscribed to bus (flow_episodes written directly from stream, not here)")
    try:
        while True:
            await q.get()  # drain the channel — no-op consumer
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


# ---------------------------------------------------------------------------
# FlowStore — in-memory store for unit / regression tests.
# ---------------------------------------------------------------------------

class FlowStore:
    """In-memory store for options flow events."""

    def __init__(self) -> None:
        self._flows: List[Any] = []
        self._stats: Dict[str, Any] = {"total": 0}

    def add_flow(self, flow: Any) -> None:
        self._flows.append(flow)
        self._stats["total"] = len(self._flows)

    def get_flows(self) -> List[Any]:
        return list(self._flows)

    def get_flows_by_symbol(self, symbol: str) -> List[Any]:
        return [
            f for f in self._flows
            if (f.get("symbol") if isinstance(f, dict) else getattr(f, "symbol", None)) == symbol
        ]

    def get_stats(self) -> Dict[str, Any]:
        return dict(self._stats)

    def clear(self) -> None:
        self._flows.clear()
        self._stats = {"total": 0}

    def size(self) -> int:
        return len(self._flows)


_store = FlowStore()


async def add_flow(flow: dict) -> None:
    _store.add_flow(flow)


async def get_flows(ticker: str) -> List[dict]:
    return [
        f for f in _store.get_flows()
        if (f.get("ticker") if isinstance(f, dict) else getattr(f, "ticker", None)) == ticker
    ]


async def clear_flows() -> None:
    _store.clear()
