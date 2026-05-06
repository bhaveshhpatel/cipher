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
  4. ING-007 (2026-05-04): persist_flow_episode() now accepts and writes
     is_multi_day_repeat (BOOLEAN). Requires migration
     add_is_multi_day_repeat_to_flow_episodes.sql to be run first.
  5. ING-007 (2026-05-04): enqueue_lookback() / get_lookback_stats() /
     start_lookback_worker() added. The worker drains an asyncio.Queue of
     ContractKeys enqueued by _process_trade() after each persisted episode,
     fetches the lookback result from contract_day_cache, then PATCHes the
     most-recent flow_episodes row for that contract with is_multi_day_repeat.
  6. LOG-FORMAT (2026-05-05): prem=$%,.0f used %-style format which does not
     support the comma thousands-separator. Pre-formatted as f-string instead.

PRE-MERGE BLOCKER FIXES (2026-05-04):
  SA-F1: start_lookback_worker() was calling
    getattr(accumulator, "_dte_premium_tiers", None)
    The attribute is named "_dte_tiers" on RepetitionAccumulator — not
    "_dte_premium_tiers". getattr always returned None, so dte_tiers was always
    empty and min_premium always fell back to the hardcoded 10_000.0 floor,
    permanently decoupling the lookback quality filter from the DTE-tier floors
    used by Gate 2. Additionally, _dte_tiers is a List[Tuple[int, Dict[int,
    float]]], not a flat dict, so min(dte_tiers.values()) would have raised
    TypeError even if the attr name were correct.
    Fix: correct attr name + traverse list-of-tuples:
      min(floor for _, floors in dte_tiers for floor in floors.values())

  PBE-F2: _update_episode_multiday() was constructing a PostgREST PATCH URL
    with &order=signal_ts.desc&limit=1 appended. PostgREST silently ignores
    order/limit modifiers on PATCH requests — ALL rows matching the
    (ticker, contract_type, strike, expiry) filter were being updated, not
    just the most recent one. On a high-frequency contract this overwrote
    is_multi_day_repeat on all prior flow_episodes rows on every new episode.
    Fix: two-step GET+PATCH by id:
      Step 1: GET ?...&order=signal_ts.desc&limit=1&select=id
              → retrieve the bigserial id of the target row.
      Step 2: PATCH ?id=eq.{id}
              → update exactly that row. Zero spurious multi-row overwrites.

DELIBERATION INLINE FIXES (2026-05-04):
  SA-F3: _update_episode_multiday GET-returns-empty-rows path was logged at
    DEBUG (invisible in Railway). Elevated to INFO so cold-miss rate is
    observable without lowering the global log level.

  PBE-F5: start_lookback_worker() resolved min_premium per-key but never
    logged it. Added log.debug line after resolution so Railway logs surface
    which DTE-tier floor is being applied to each lookback fetch.

ING-007 cold-cache note:
  The background asyncio.Queue pattern means lookback results are populated
  asynchronously after the first episode for a contract arrives. The first
  episode in a session may briefly see prior_days_active=0 and
  is_multi_day_repeat=False until the queue worker completes the DB fetch
  and populates the cache. Acceptable — is_multi_day_repeat is enrichment,
  not a gate (SA-Q1, 2026-05-04).

PBE-NEW-1 (2026-05-04): get_contract_prior_days() removed.
  The sync RPC helper was superseded by the async queue + contract_day_cache
  route before merge. No production call site exists. Removed to eliminate
  dead code and the orphaned test coverage that accompanied it.
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
# ING-007: async lookback enrichment queue
#   Bounded at _LOOKBACK_QUEUE_MAX (5000 — PBE-Q3 deliberation 2026-05-04)
#   to prevent unbounded memory growth on a very active trading day.
#   Overflow is counted and surfaced in stats as "lookback_queue_overflow".
# ---------------------------------------------------------------------------
_LOOKBACK_QUEUE_MAX = 5_000  # PBE-Q3: 5000, not 500
_lookback_queue: asyncio.Queue = asyncio.Queue(maxsize=_LOOKBACK_QUEUE_MAX)
_lookback_stats: Dict[str, int] = {
    "lookback_queued":         0,
    "lookback_queue_overflow": 0,  # PBE-Q3: exact key name per deliberation
}


def enqueue_lookback(key) -> None:
    """
    Non-blocking enqueue of a ContractKey for async lookback enrichment.

    Called from _process_trade() hot path after the persist_ep gate passes.
    Never raises — overflow is silently counted and surfaced in get_lookback_stats().

    key: ContractKey = Tuple[str, str, float, str]  (ticker, contract_type, strike, expiry)
    """
    try:
        _lookback_queue.put_nowait(key)
        _lookback_stats["lookback_queued"] += 1
    except asyncio.QueueFull:
        _lookback_stats["lookback_queue_overflow"] += 1  # PBE-Q3 key
        log.debug(
            "[lookback] queue full (%d) — key dropped: %s",
            _LOOKBACK_QUEUE_MAX, key,
        )


def get_lookback_stats() -> Dict[str, int]:
    """Return a copy of lookback queue stats for /health/stream surfacing."""
    return dict(_lookback_stats)


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


async def upgrade_to_sweep_in_db(occ_symbol: str, fill_price: float, size: int) -> bool:
    """
    C-003: Retroactively upgrade flow_events rows to trade_type='SWEEP'.
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

    ING-007: now accepts is_multi_day_repeat (BOOLEAN, default False).
    Requires migration add_is_multi_day_repeat_to_flow_episodes.sql.
    """
    expiry = signal_data.get("expiry") or None

    row = {
        "ticker":              signal_data.get("ticker"),
        "direction":           signal_data.get("direction"),
        "contract_type":       signal_data.get("contract_type"),
        "strike":              signal_data.get("strike"),
        "expiry":              expiry,
        "total_premium":       signal_data.get("total_premium"),
        "trade_count":         signal_data.get("trade_count"),
        "alert_level":         signal_data.get("alert_level"),
        "is_accelerating":     signal_data.get("is_accelerating", False),
        "is_multi_day_repeat": signal_data.get("is_multi_day_repeat", False),
        "seed_episode":        signal_data.get("seed_episode"),
        "signal_ts":           signal_data.get("timestamp"),
    }
    ok = await _insert_rows("flow_episodes", [row])
    if ok:
        # LOG-FORMAT fix (2026-05-05): %-style does not support comma thousands-separator.
        # Pre-format total_premium as an f-string before passing to log.info.
        prem_str = f"${row['total_premium'] or 0:,.0f}"
        log.info(
            "[flow_store] flow_episode saved: %s %s strike=%s expiry=%s "
            "alert=%s prem=%s multi_day=%s",
            row["ticker"], row["contract_type"],
            row["strike"], row["expiry"],
            row["alert_level"], prem_str,
            row["is_multi_day_repeat"],
        )


async def _update_episode_multiday(
    ticker: str,
    contract_type: str,
    strike: float,
    expiry: str,
    is_multi_day_repeat: bool,
) -> None:
    """
    ING-007: PATCH the most-recent flow_episodes row for this contract
    to set is_multi_day_repeat after the async lookback fetch completes.

    PBE-F2 fix (2026-05-04): PostgREST silently ignores &order= and &limit=
    on PATCH requests — all rows matching the filter predicate are updated,
    not just the most recent one. Fixed with a two-step GET+PATCH by id:

      Step 1: GET ?...&order=signal_ts.desc&limit=1&select=id
              Retrieve the bigserial id of the target (most recent) row.
      Step 2: PATCH ?id=eq.{id}
              Update exactly that single row. No spurious multi-row overwrites.

    SA-F3 fix (2026-05-04): empty GET response (race between INSERT commit
    and queue worker) was logged at DEBUG — invisible in Railway. Elevated
    to INFO so cold-miss rate is observable without changing global log level.

    Silently swallows errors — enrichment failure must never block the stream.
    """
    if not _is_configured():
        return

    get_headers = {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Accept":        "application/json",
    }
    patch_headers = {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }

    filter_qs = (
        f"?ticker=eq.{quote(ticker)}"
        f"&contract_type=eq.{quote(contract_type)}"
        f"&strike=eq.{strike}"
        f"&expiry=eq.{quote(expiry)}"
    )

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Step 1: GET the id of the most-recent matching row.
            get_url = (
                f"{_SUPABASE_URL}/rest/v1/flow_episodes"
                f"{filter_qs}"
                f"&order=signal_ts.desc"
                f"&limit=1"
                f"&select=id"
            )
            get_resp = await client.get(get_url, headers=get_headers)
            if get_resp.status_code != 200:
                log.warning(
                    "[flow_store] _update_episode_multiday GET failed: %d -- %s "
                    "(ticker=%s %s $%.0f %s)",
                    get_resp.status_code, get_resp.text[:200],
                    ticker, contract_type, strike, expiry,
                )
                return

            rows = get_resp.json()
            if not rows:
                log.info(
                    "[flow_store] _update_episode_multiday: no episode row found yet for "
                    "%s %s $%.0f %s — INSERT may not have committed; "
                    "skipping PATCH (enrichment-only, not a gate)",
                    ticker, contract_type, strike, expiry,
                )
                return

            row_id = rows[0].get("id")
            if row_id is None:
                log.warning(
                    "[flow_store] _update_episode_multiday: GET returned row with "
                    "no id field (ticker=%s %s $%.0f %s) — skipping PATCH",
                    ticker, contract_type, strike, expiry,
                )
                return

            # Step 2: PATCH exactly that row by its id.
            patch_url = (
                f"{_SUPABASE_URL}/rest/v1/flow_episodes"
                f"?id=eq.{row_id}"
            )
            patch_resp = await client.patch(
                patch_url,
                headers=patch_headers,
                json={"is_multi_day_repeat": is_multi_day_repeat},
            )
            if patch_resp.status_code not in (200, 204):
                log.warning(
                    "[flow_store] _update_episode_multiday PATCH failed: %d -- %s "
                    "(id=%s ticker=%s %s $%.0f %s)",
                    patch_resp.status_code, patch_resp.text[:200],
                    row_id, ticker, contract_type, strike, expiry,
                )

    except Exception as exc:
        log.warning(
            "[flow_store] _update_episode_multiday exception: %s "
            "(ticker=%s %s $%.0f %s)",
            exc, ticker, contract_type, strike, expiry,
        )


async def start_lookback_worker(accumulator) -> None:
    """
    ING-007: Async queue worker — drains _lookback_queue populated by
    enqueue_lookback() in _process_trade().

    For each ContractKey dequeued:
      1. Resolve the DTE-tier min_premium from the accumulator so the
         lookback query uses the same floor as the persist gate.
      2. Call contract_day_cache.get_lookback(key, min_premium).
      3. PATCH the most-recent flow_episodes row via _update_episode_multiday()
         with is_multi_day_repeat = (
             prior_days_active >= accumulator._multi_day_min_days
         ).
         Uses the accumulator's configured threshold — never hardcoded.

    SA-F1 fix (2026-05-04):
      Previously called getattr(accumulator, "_dte_premium_tiers", None) which
      always returned None because the attribute is stored as "_dte_tiers" on
      RepetitionAccumulator. Also, _dte_tiers is a List[Tuple[int, Dict[int,
      float]]], not a flat dict, so min(dte_tiers.values()) would have raised
      TypeError. Fixed to use the correct attribute name and traverse the
      list-of-tuples structure to extract the minimum floor value.

    PBE-F5 fix (2026-05-04):
      min_premium resolved per-key but never logged. Added debug line so
      Railway logs surface which DTE-tier floor is applied to each fetch.

    Cold-cache note: the first episode for any contract returns
    prior_days_active=0 (cache miss → DB fetch). Subsequent ticks within
    the 5-min TTL window use the cached result. This is acceptable —
    is_multi_day_repeat is enrichment-only, not a hard gate.

    Runs as a persistent background task spawned from main.py lifespan.
    Cancelled cleanly on shutdown via lookback_task.cancel().
    """
    # Deferred import to avoid circular dependency at module load time.
    from utils.contract_day_cache import get_lookback

    # Resolve multi_day_min_days from accumulator (default 2 per SA deliberation).
    multi_day_min_days: int = getattr(accumulator, "_multi_day_min_days", 2)

    log.info(
        "[lookback_worker] started — draining ContractKey queue "
        "(maxsize=%d, multi_day_min_days=%d)",
        _LOOKBACK_QUEUE_MAX, multi_day_min_days,
    )
    try:
        while True:
            key = await _lookback_queue.get()
            ticker, contract_type, strike, expiry = key

            # SA-F1 fix: correct attribute name (_dte_tiers, not _dte_premium_tiers)
            # and correct traversal for List[Tuple[int, Dict[int, float]]] structure.
            try:
                dte_tiers = getattr(accumulator, "_dte_tiers", None) or []
                if dte_tiers:
                    min_premium = min(
                        floor
                        for _, floors in dte_tiers
                        for floor in floors.values()
                    )
                else:
                    min_premium = 10_000.0
            except (ValueError, TypeError):
                min_premium = 10_000.0

            # PBE-F5 fix: log the resolved min_premium so Railway surfaces which
            # DTE-tier floor is being applied to each lookback fetch.
            log.debug(
                "[lookback_worker] %s %s $%.0f %s — "
                "min_premium=%.0f (dte_tiers_found=%s)",
                ticker, contract_type, strike, expiry,
                min_premium, bool(dte_tiers),
            )

            try:
                result = await get_lookback(key, min_premium)
                is_repeat = result.prior_days_active >= multi_day_min_days
                await _update_episode_multiday(
                    ticker, contract_type, strike, expiry, is_repeat
                )
                log.debug(
                    "[lookback_worker] %s %s $%.0f %s — "
                    "prior_days_active=%d aggressive=%d is_repeat=%s (min_days=%d)",
                    ticker, contract_type, strike, expiry,
                    result.prior_days_active, result.prior_days_aggressive,
                    is_repeat, multi_day_min_days,
                )
            except Exception as exc:
                log.warning(
                    "[lookback_worker] error processing key %s: %s", key, exc
                )
            finally:
                _lookback_queue.task_done()

    except asyncio.CancelledError:
        log.info("[lookback_worker] cancelled — shutting down cleanly")
        raise


async def _bus_signal_listener():
    q = bus.subscribe("db_writer")
    log.info("[flow_store] DB writer subscribed to bus (flow_episodes written directly from stream, not here)")
    try:
        while True:
            await q.get()
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
