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

C-003 — Sweep Retroactive Upgrade:
  upgrade_to_sweep_in_db(occ_symbol, fill_price, size) issues a targeted
  PATCH (UPDATE) to flow_events setting trade_type='SWEEP' for rows that
  were written as 'BTO' before the sweep threshold was confirmed. Called
  from _process_trade() via asyncio.create_task() so it does not block
  the stream hot path.
"""
import asyncio
import logging
import os
from typing import Optional

import httpx

from core.async_bus import bus  # module-level import so patch('services.flow_store.bus') works

log = logging.getLogger("flow_store")

_SUPABASE_URL: Optional[str] = os.environ.get("SUPABASE_URL")

_SUPABASE_KEY: Optional[str] = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SERVICE_KEY")
)

_FLUSH_INTERVAL = 0.5
_FLUSH_MAX_ROWS = 100

_RETRY_MAX     = 3
_RETRY_DELAY_S = 1.0

_buffer_lock: Optional[asyncio.Lock] = None
_flow_event_buffer: list[dict] = []


def _get_lock() -> asyncio.Lock:
    global _buffer_lock
    if _buffer_lock is None:
        _buffer_lock = asyncio.Lock()
    return _buffer_lock


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
        log.error(
            "[flow_store] insert into %s failed: %s -- %s",
            table, resp.status_code, resp.text[:300],
        )
        return False
    except Exception as exc:
        log.error("[flow_store] insert into %s exception: %s", table, exc)
        return False


async def _insert_rows_with_retry(table: str, rows: list[dict]) -> bool:
    for attempt in range(1, _RETRY_MAX + 1):
        ok = await _insert_rows(table, rows)
        if ok:
            return True
        if attempt < _RETRY_MAX:
            log.warning(
                "[flow_store] insert into %s failed (attempt %d/%d) -- retrying in %ss",
                table, attempt, _RETRY_MAX, _RETRY_DELAY_S,
            )
            await asyncio.sleep(_RETRY_DELAY_S)
    log.error(
        "[flow_store] insert into %s failed after %d attempts "
        "-- %d rows DISCARDED. Check Supabase connectivity.",
        table, _RETRY_MAX, len(rows),
    )
    return False


async def _flush_flow_events() -> None:
    global _flow_event_buffer
    lock = _get_lock()
    while True:
        await asyncio.sleep(_FLUSH_INTERVAL)
        async with lock:
            if not _flow_event_buffer:
                continue
            batch = _flow_event_buffer[:_FLUSH_MAX_ROWS]
            _flow_event_buffer = _flow_event_buffer[_FLUSH_MAX_ROWS:]
        ok = await _insert_rows_with_retry("flow_events", batch)
        if ok:
            log.info("[flow_store] flushed %d flow_events to DB", len(batch))


async def persist_flow_event(ev_dict: dict) -> None:
    """
    Buffer a single flow event dict for DB persistence.
    """
    global _flow_event_buffer

    if not _is_configured():
        log.warning(
            "[flow_store] persist_flow_event: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY "
            "not set — event dropped. Set env vars to enable DB persistence."
        )
        return

    ticker = ev_dict.get("ticker", "UNKNOWN")
    expiry = ev_dict.get("expiry") or None
    strike = ev_dict.get("strike")

    if not expiry:
        log.warning("[flow_store] %s: expiry is empty -- OCC parse may have failed", ticker)
    if strike == 0.0:
        log.warning("[flow_store] %s: strike=0.0 -- verify OCC parse for this symbol", ticker)

    row = {
        "ticker":             ticker,
        "contract_type":      ev_dict.get("contract_type"),
        "strike":             strike,
        "expiry":             expiry,
        "dte":                ev_dict.get("dte", 0),
        "fill_price":         ev_dict.get("fill_price", 0.0),
        "bid":                ev_dict.get("bid", 0.0),
        "ask":                ev_dict.get("ask", 0.0),
        "size":               ev_dict.get("size", 0),
        "premium":            ev_dict.get("premium", 0.0),
        "trade_type":         ev_dict.get("trade_type", "UNKNOWN"),
        "bid_ask_class":      ev_dict.get("bid_ask_class", "MID"),
        "is_aggressive":      ev_dict.get("is_aggressive", False),
        "is_golden_sweep":    ev_dict.get("is_golden_sweep", False),
        "sentiment":          ev_dict.get("sentiment", "NEUTRAL"),
        "influence_tier":     ev_dict.get("influence_tier", "RETAIL"),
        "conviction_score":   ev_dict.get("conviction_score", 0.0),
        "exchange_count":     ev_dict.get("exchange_count", 1),
        "fill_count":         ev_dict.get("fill_count", 1),
        "open_interest":      ev_dict.get("open_interest", 0),
        "iv":                 ev_dict.get("iv", 0.0),
        "underlying_price":   ev_dict.get("underlying_price", 0.0),
        "occ_symbol":         ev_dict.get("occ_symbol"),
        "is_synthetic_quote": ev_dict.get("is_synthetic_quote", False),
    }

    lock = _get_lock()
    flush_batch: Optional[list[dict]] = None

    async with lock:
        _flow_event_buffer.append(row)
        if len(_flow_event_buffer) >= _FLUSH_MAX_ROWS:
            flush_batch = _flow_event_buffer[:_FLUSH_MAX_ROWS]
            _flow_event_buffer = _flow_event_buffer[_FLUSH_MAX_ROWS:]

    if flush_batch:
        ok = await _insert_rows_with_retry("flow_events", flush_batch)
        if ok:
            log.info(
                "[flow_store] early flush (%d rows) -- buffer hit max",
                len(flush_batch),
            )


async def upgrade_to_sweep_in_db(occ_symbol: str, fill_price: float, size: int) -> bool:
    if not _is_configured():
        log.debug("[flow_store] upgrade_to_sweep_in_db: not configured, skipping")
        return False

    url = (
        f"{_SUPABASE_URL}/rest/v1/flow_events"
        f"?occ_symbol=eq.{occ_symbol}"
        f"&fill_price=eq.{fill_price}"
        f"&size=eq.{size}"
        f"&trade_type=neq.SWEEP"
        f"&created_at=gte.now()-interval%2030%20seconds"
    )
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.patch(url, headers=_headers(), json={"trade_type": "SWEEP"})
        if resp.status_code in (200, 204):
            log.info(
                "[flow_store] sweep upgrade applied: %s fill=%s size=%s",
                occ_symbol, fill_price, size,
            )
            return True
        log.warning(
            "[flow_store] sweep upgrade failed: %s -- %s",
            resp.status_code, resp.text[:200],
        )
        return False
    except Exception as exc:
        log.error("[flow_store] upgrade_to_sweep_in_db exception: %s", exc)
        return False


async def persist_flow_episode(signal_data: dict) -> None:
    row = {
        "ticker":          signal_data.get("ticker"),
        "direction":       signal_data.get("direction"),
        "contract_type":   signal_data.get("contract_type"),
        "strike":          signal_data.get("strike"),
        "expiry":          signal_data.get("expiry") or None,
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
            "[flow_store] flow_episode saved: %s %s alert=%s prem=$%s",
            row["ticker"], row["contract_type"],
            row["alert_level"],
            f"{row['total_premium'] or 0:,.0f}",
        )


async def _bus_signal_listener() -> None:
    q = bus.subscribe("db_writer")
    log.info("[flow_store] DB writer subscribed to bus -- flow_episodes written on composite_signal only")
    try:
        while True:
            msg = await q.get()
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "composite_signal":
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


async def start_flow_writer() -> None:
    if not _is_configured():
        log.warning(
            "[flow_store] SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY not set -- "
            "flow_events and flow_episodes will NOT be persisted to DB. "
            "Ensure SUPABASE_SERVICE_ROLE_KEY (not the anon key) is set in Railway env vars."
        )
        return
    log.info(
        "[flow_store] Starting flow DB writer "
        "(flush_interval=%ss, max_rows=%d, retry_max=%d)",
        _FLUSH_INTERVAL, _FLUSH_MAX_ROWS, _RETRY_MAX,
    )
    await asyncio.gather(
        _bus_signal_listener(),
        _flush_flow_events(),
    )


# ---------------------------------------------------------------------------
# In-memory helpers — used by the 6-layer regression test and demo flow path.
# ---------------------------------------------------------------------------

import collections as _collections

_mem_store: _collections.deque = _collections.deque(maxlen=5_000)


async def add_flow(flow) -> None:
    """Append a flow (dict or dataclass) to the in-memory store."""
    _mem_store.append(flow)


def get_flows_sync(ticker: Optional[str] = None) -> list:
    """
    Synchronous version of get_flows — returns a plain list.
    Used by tests that call sum(get_flows_sync(ticker)) directly.
    """
    if ticker is None:
        return list(_mem_store)
    return [
        f for f in _mem_store
        if (
            f.get("ticker") if isinstance(f, dict)
            else getattr(f, "ticker", None)
        ) == ticker
    ]


async def get_flows(ticker: Optional[str] = None) -> list:
    """
    Async helper — returns a plain list.
    Named get_flows and declared async so callers can do:
        flows = await get_flows(ticker)
    This is the module-level function used by test_6layer_regression.py.
    """
    return get_flows_sync(ticker)


async def aget_flows(ticker: str):
    """True async generator for callers that need streaming iteration."""
    for f in _mem_store:
        key = f.get("ticker") if isinstance(f, dict) else getattr(f, "ticker", None)
        if key == ticker:
            yield f


async def clear_flows() -> None:
    """Clear the in-memory store."""
    _mem_store.clear()


# ---------------------------------------------------------------------------
# FlowStore — backward-compat class for tests that do:
#   from services.flow_store import FlowStore
# ---------------------------------------------------------------------------

class FlowStore:
    """
    Instance-level wrapper around a private deque.
    Tests use FlowStore() to get an isolated store per test.
    """

    def __init__(self):
        self._store: _collections.deque = _collections.deque(maxlen=5_000)
        self._stats_dict: dict = {"total": 0}

    # ---- mutation --------------------------------------------------------

    def add_flow(self, flow) -> None:
        """Add a flow (dict or dataclass) to this store instance."""
        self._store.append(flow)
        self._stats_dict["total"] = self._stats_dict.get("total", 0) + 1

    def clear(self) -> None:
        """Reset this store instance."""
        self._store.clear()
        self._stats_dict = {"total": 0}

    # ---- queries ---------------------------------------------------------

    def get_flows(self, ticker: Optional[str] = None) -> list:
        """Return all flows, optionally filtered by ticker.

        Intentionally synchronous so tests can call:
            assert isinstance(store.get_flows(), list)
        without awaiting.
        """
        if ticker is None:
            return list(self._store)
        return [
            f for f in self._store
            if (
                f.get("ticker") if isinstance(f, dict)
                else getattr(f, "ticker", None)
            ) == ticker
        ]

    def get_flows_by_symbol(self, symbol: str) -> list:
        """Return flows matching the given symbol."""
        return [
            f for f in self._store
            if (
                f.get("ticker") if isinstance(f, dict)
                else getattr(f, "ticker", None)
            ) == symbol
        ]

    def get_stats(self) -> dict:
        """Return a dict with basic stats for this store instance."""
        return dict(self._stats_dict)

    def __len__(self) -> int:
        return len(self._store)

    # ---- async compat ----------------------------------------------------

    async def async_add_flow(self, flow) -> None:
        self.add_flow(flow)

    async def async_get_flows(self, ticker: Optional[str] = None) -> list:
        return self.get_flows(ticker=ticker)

    async def async_clear(self) -> None:
        self.clear()
