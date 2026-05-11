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
  - flow_episodes : one row per same-session episode (upsert — ING-009)
                    Written directly from _process_trade() in tradier_stream.py
                    BEFORE the SIG-DEBOUNCE check. This decouples episode
                    persistence from the debounce gate (which is only for
                    WebSocket / signal_history anti-spam).

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
  7. ING-009 (2026-05-06): persist_flow_episode() is now an upsert.
     _lookup_open_episode() queries flow_episodes for an open same-session
     episode matching (ticker, direction, contract_type, strike, expiry)
     within _EPISODE_MERGE_WINDOW_S. On match: PATCH (trade_count +=1,
     total_premium +=, signal_ts = new). On no match: INSERT (existing path).
     _stats gains "created_episodes" and "merged_episodes" counters.
     Both counters initialised at module level and visible in /health/stream.
  8. ING-009-RACE (2026-05-08): concurrent coroutines for the same contract
     arriving within the same batch flush window both called
     _lookup_open_episode(), both received None (neither INSERT had committed
     yet), and both issued INSERT — producing 2-3 orphan episode rows instead
     of 1. Fix: per-contract asyncio.Lock (_episode_locks dict keyed by merge
     key string) serialises the lookup→insert/patch path. An in-process
     _episode_in_flight cache allows the second waiter to PATCH the just-
     inserted row without a DB round-trip.
  9. ING-008 (2026-05-08): persist_flow_event() now captures vol/OI snapshot
     from chain_store.get_contract_vol_oi(occ_symbol) and writes
     contract_volume_snapshot and contract_oi to the flow_events row.
     persist_flow_episode() captures vol/OI at INSERT time (contract_oi_at_open)
     and at PATCH time (contract_volume_at_close), then pre-computes
     volume_oi_ratio = volume / oi (NULL-safe, zero-OI-safe).
     Vol/OI is enrichment only — a cache miss (None) never drops flow.
  10. REARCH-010 (2026-05-09): removed is_golden_sweep, influence_tier, and
      conviction_score from persist_flow_event() row dict — all three columns
      were dropped from flow_events in migration 024. Keeping them caused a
      PostgREST 400 on every event insert.
  11. REARCH-003 (2026-05-10): full event quality tag suite written to
      flow_events. Seven fields total:
        is_ask_side, bid_ask_class, vol_oi_signal, normalized_premium
          — computed by module-level pure helpers (_classify_bid_ask,
            _compute_vol_oi_signal, _compute_normalized_premium).
        dte_bucket, notional_tier, event_cipher_score
          — computed by IngestionProcessor.enrich_tags() (static method,
            processor.py), called after the tag block above.
            enrich_tags() reads is_ask_side/vol_oi_signal already in the row
            dict. Local import avoids circular dependency.
            Failure is non-fatal — NULLs written, same policy as ING-008.
      All seven fields are derived from values already in ev_dict or the
      chain_store vol/OI cache — zero new I/O, zero new blocking calls.
      Migrations 026 (ADD COLUMN) and 027 (backfill) accompany this change.
  12. REARCH-003-FIX (2026-05-10): _classify_bid_ask used fp >= a * 0.98 for
      the ASK boundary. With fill=4.90, bid=4.80, ask=5.00 the threshold is
      4.90, so a fill exactly at mid was classified ASK instead of MID.
      Fix: strict boundaries — fill >= ask → ASK, fill <= bid → BID, else MID.
      No fuzz multipliers. is_ask_side remains True iff bid_ask_class == 'ASK'.
  13. TEST-FIX (2026-05-10): three test failures resolved:
      a. ImportError: FlowStore — add FlowStore in-memory class with
         add_flow, get_flows, get_flows_by_symbol, get_stats, clear, size.
      b. AttributeError: _insert_rows_with_episode_id — extract the episode
         INSERT httpx call into a named coroutine that tests can patch.
         persist_flow_episode INSERT branch calls _insert_rows_with_episode_id(
         table, row, key, premium, oi_at_open). Row includes alert_level,
         is_accelerating, seed_episode from signal_data.
      c. TypeError: Queue does not support async context manager protocol —
         AsyncEventBus.subscribe() returns asyncio.Queue, not an async CM.
         _bus_signal_listener now uses bus.subscribe()/bus.unsubscribe() with
         try/finally instead of `async with bus.subscribe(...) as queue`.

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

ING-009 episode merge semantics:
  flow_episodes = one aggregated episode per contract per same-session window.
  flow_events   = every qualifying classified tick (unchanged — insert-only).
  A subsequent qualifying print for an open episode within
  _EPISODE_MERGE_WINDOW_S updates the existing row rather than inserting a
  new one. This ensures ING-007's get_contract_prior_days() query operates
  on correctly aggregated episode rows, not per-print duplicates.

ING-009-RACE episode lock semantics:
  _episode_locks: Dict[str, asyncio.Lock]
    One lock per merge key ("{ticker}|{dir}|{ctype}|{strike}|{expiry}").
    Created on first use via _get_episode_lock(key). Never deleted within
    a session — the dict is bounded by the tracked-symbol universe
    (O(contracts seen per session), not O(events)).

  _episode_in_flight: Dict[str, dict]
    Stores {"id": int, "trade_count": int, "total_premium": float} for
    the most recently inserted/patched episode for each key.
    The first coroutine to acquire the lock finds nothing in-flight and
    checks the DB. If it INSERTs, it populates _episode_in_flight[key] via
    _set_episode_in_flight(). Subsequent waiters that acquire the lock see
    the in-flight entry and go directly to PATCH — no DB GET needed.
    After each PATCH, total_premium in the in-flight entry is updated so
    the next waiter accumulates correctly.
    Cleared on session reset via reset_episode_state().

ING-008 Vol/OI capture semantics:
  flow_events.contract_volume_snapshot
  flow_events.contract_oi
    Captured inline in persist_flow_event() via get_contract_vol_oi(occ_symbol).
    Written into the buffered row dict before it enters _flow_event_buffer.
    NULL on cache miss — never a drop condition.

  flow_episodes.contract_oi_at_open
    Captured at INSERT time (episode creation) only.
    Represents OI for the contract at the time the episode opened.

  flow_episodes.contract_volume_at_close
    Captured at every PATCH (episode merge) — always the latest value
    at the time of the most recent qualifying print.

  flow_episodes.volume_oi_ratio
    Pre-computed at persist time as contract_volume_at_close / contract_oi_at_open.
    NULL when either component is None or contract_oi_at_open == 0.
    No live computation required in signal engines — read directly.

REARCH-003 event quality tag semantics:
  flow_events.is_ask_side
    True when fill_price >= ask (strict ask boundary, no fuzz).
    False when ask is None or 0 (conservative fallback).
    Computed by _classify_bid_ask() — same function that determines bid_ask_class.

  flow_events.bid_ask_class
    Already persisted. Now computed via shared _classify_bid_ask() helper
    to ensure is_ask_side and bid_ask_class are always consistent.
    Values: 'ASK' | 'BID' | 'MID'.
    ASK: fill >= ask. BID: fill <= bid. MID: strictly between bid and ask.

  flow_events.vol_oi_signal
    'HIGH'    when contract_volume_snapshot / contract_oi >= _VOL_OI_HIGH_THRESHOLD (0.5)
    'NORMAL'  when ratio < threshold
    'UNKNOWN' when either input is None or contract_oi == 0 (cache miss / zero-OI-safe)
    Computed by _compute_vol_oi_signal().

  flow_events.normalized_premium
    premium / underlying_price, rounded to 4 decimal places.
    NULL when underlying_price is None or 0 (div-by-zero safe).
    Computed by _compute_normalized_premium().

  flow_events.dte_bucket
    DTE bracket: '0DTE' | '1-4' | '5-60' | '61-90' | '90+'
    Computed by IngestionProcessor.enrich_tags() via _compute_dte_bucket().

  flow_events.notional_tier
    Premium size bucket: 'WATCH' | 'NOTEWORTHY' | 'BLOCK' | 'GOLDEN'
    Thresholds: WATCH <$50k, NOTEWORTHY $50k–$99k, BLOCK $100k–$499k, GOLDEN $500k+
    Computed by IngestionProcessor.enrich_tags() via _compute_notional_tier().

  flow_events.event_cipher_score
    Sum of 4 binary Steamroom dimensions (0–4). Dim 5 is REARCH-004 scope.
    Dim 1: is_ask_side
    Dim 2: vol_oi_signal == 'HIGH'
    Dim 3: notional_tier in ('BLOCK', 'GOLDEN')
    Dim 4: dte_bucket in ('1-4', '5-60')
    Computed by IngestionProcessor.enrich_tags() via _compute_cipher_score().
    None inputs treated as zero-score defaults — never raises.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

import utils.contract_day_cache as _contract_day_cache  # ING-007: module ref so patch('utils.contract_day_cache.get_lookback') intercepts at call time

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
# REARCH-003: vol/OI signal threshold
#   Module-level constant so tests can override without patching internals:
#     import services.flow_store as fs; fs._VOL_OI_HIGH_THRESHOLD = 0.3
# ---------------------------------------------------------------------------
_VOL_OI_HIGH_THRESHOLD: float = 0.5

# ---------------------------------------------------------------------------
# ING-009: same-session episode merge window
#   Two qualifying prints for the same contract within this window are merged
#   into a single flow_episodes row (PATCH) rather than inserting a new row.
#   30 minutes (1800s) is the WSJ same-session accumulation window.
# ---------------------------------------------------------------------------
_EPISODE_MERGE_WINDOW_S: int = 1800

# ---------------------------------------------------------------------------
# ING-009-RACE: per-contract locks and in-flight cache
#   Prevents duplicate INSERT when two coroutines for the same contract key
#   arrive concurrently within the same batch flush window.
#   Both dicts are keyed by the merge key string:
#     "{ticker}|{direction}|{contract_type}|{strike}|{expiry}"
# ---------------------------------------------------------------------------
_episode_locks: Dict[str, asyncio.Lock] = {}
_episode_in_flight: Dict[str, dict] = {}  # key -> {"id", "trade_count", "total_premium"}


def _episode_key(ticker: str, direction: str, contract_type: str, strike: float, expiry: str) -> str:
    """Stable merge-key string for _episode_locks and _episode_in_flight."""
    return f"{ticker}|{direction}|{contract_type}|{strike}|{expiry}"


def _get_episode_lock(key: str) -> asyncio.Lock:
    """Return the existing lock for key, or create one atomically."""
    if key not in _episode_locks:
        _episode_locks[key] = asyncio.Lock()
    return _episode_locks[key]


def _set_episode_in_flight(key: str, row_id: int, trade_count: int, total_premium: float) -> None:
    """Cache the just-inserted/patched episode so concurrent waiters skip the DB GET."""
    _episode_in_flight[key] = {
        "id":            row_id,
        "trade_count":   trade_count,
        "total_premium": total_premium,
    }


def reset_episode_state() -> None:
    """
    Clear the in-flight cache and lock dict for session reset (market-open boundary).
    Called from the market-open handler and from tests.
    Note: locks that are currently held will be released when their coroutine
    exits normally — this is safe to call between sessions only.
    """
    _episode_in_flight.clear()
    _episode_locks.clear()


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

# ---------------------------------------------------------------------------
# ING-009: episode upsert stats
#   Both counters must be in the module-level init block so /health/stream
#   never raises KeyError on cold start before the first episode is written.
# ---------------------------------------------------------------------------
_episode_stats: Dict[str, int] = {
    "created_episodes": 0,
    "merged_episodes":  0,
}


def get_episode_stats() -> Dict[str, int]:
    """Return a copy of episode upsert stats for /health/stream surfacing."""
    return dict(_episode_stats)


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


# ---------------------------------------------------------------------------
# TEST-FIX (2026-05-10): FlowStore in-memory class
#   Provides a simple add/get/clear interface for tests and any consumer that
#   needs in-memory flow storage without hitting the DB or the async bus.
# ---------------------------------------------------------------------------

class FlowStore:
    """
    Lightweight in-memory store for options flow events.
    Used by tests and any consumer that needs a simple add/get/clear interface
    without hitting the DB or the async bus.
    """

    def __init__(self):
        self._flows: list = []

    def add_flow(self, flow: dict) -> None:
        self._flows.append(flow)

    def get_flows(self) -> list:
        return list(self._flows)

    def get_flows_by_symbol(self, symbol: str) -> list:
        result = []
        for f in self._flows:
            if isinstance(f, dict):
                if f.get("symbol") == symbol:
                    result.append(f)
            else:
                if getattr(f, "symbol", None) == symbol:
                    result.append(f)
        return result

    def get_stats(self) -> dict:
        return {"total": len(self._flows)}

    def clear(self) -> None:
        self._flows.clear()

    def size(self) -> int:
        return len(self._flows)


def _is_configured() -> bool:
    return bool(_SUPABASE_URL and _SUPABASE_KEY)


def _headers() -> dict:
    return {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }


# ---------------------------------------------------------------------------
# REARCH-003: Event quality tag helpers
#   Pure functions — no I/O, no side effects. Independently testable.
# ---------------------------------------------------------------------------

def _classify_bid_ask(
    fill_price: Optional[float],
    bid: Optional[float],
    ask: Optional[float],
) -> tuple[str, bool]:
    """
    REARCH-003: Classify a fill relative to the bid/ask spread.

    Returns (bid_ask_class, is_ask_side) where:
      bid_ask_class : 'ASK' | 'BID' | 'MID'
      is_ask_side   : True iff bid_ask_class == 'ASK'

    Boundaries (strict, no fuzz):
      fill >= ask  → 'ASK', True
      fill <= bid  → 'BID', False
      otherwise    → 'MID', False

    Edge cases:
      - ask is None or 0: returns 'MID', False (conservative — no ask price available)
      - bid is None or 0: bid boundary skipped, falls through to MID
      - fill_price is None: treated as 0.0
    """
    fp = fill_price or 0.0
    a  = ask or 0.0
    b  = bid or 0.0

    if a > 0 and fp >= a:
        return "ASK", True
    if b > 0 and fp <= b:
        return "BID", False
    return "MID", False


def _compute_vol_oi_signal(
    volume: Optional[int],
    oi: Optional[int],
    threshold: float = _VOL_OI_HIGH_THRESHOLD,
) -> str:
    """
    REARCH-003: Derive the vol/OI signal category.

    Returns:
      'HIGH'    — volume / oi >= threshold
      'NORMAL'  — volume / oi < threshold
      'UNKNOWN' — either input is None, or oi == 0

    threshold defaults to module-level _VOL_OI_HIGH_THRESHOLD (0.5).
    Tests may pass a custom threshold without patching the module.
    """
    if volume is None or oi is None or oi == 0:
        return "UNKNOWN"
    return "HIGH" if (volume / oi) >= threshold else "NORMAL"


def _compute_normalized_premium(
    premium: Optional[float],
    underlying_price: Optional[float],
) -> Optional[float]:
    """
    REARCH-003: Compute premium / underlying_price, rounded to 4dp.

    Returns None when underlying_price is None or 0 (div-by-zero safe).
    Returns None when premium is None.
    """
    if premium is None or underlying_price is None or underlying_price == 0:
        return None
    return round(premium / underlying_price, 4)


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


# ---------------------------------------------------------------------------
# TEST-FIX (2026-05-10): _insert_rows_with_episode_id
#   Extracted from the persist_flow_episode INSERT branch so tests can patch
#   this named coroutine without intercepting the generic _insert_rows path.
#
#   Signature (positional, matches test expectations):
#     args[0] = table      (str)           "flow_episodes"
#     args[1] = row        (dict)          full episode row dict
#     args[2] = key        (str)           merge key
#     args[3] = premium    (float)
#     args[4] = oi_at_open (Optional[int])
#
#   On 200/201: populates _episode_in_flight cache and increments
#   _episode_stats["created_episodes"]. Returns True.
#   On failure: logs warning. Returns False.
# ---------------------------------------------------------------------------

async def _insert_rows_with_episode_id(
    table: str,
    row: dict,
    key: str,
    premium: float,
    oi_at_open: Optional[int],
) -> bool:
    """
    INSERT a new episode row and populate the in-flight cache on success.

    Called exclusively from the INSERT branch of persist_flow_episode().
    Named so tests can patch services.flow_store._insert_rows_with_episode_id
    without affecting the generic _insert_rows used for flow_events batches.
    """
    if not _is_configured():
        return False

    ticker        = row.get("ticker", "?")
    contract_type = row.get("contract_type", "?")
    strike        = row.get("strike", 0.0)
    expiry        = row.get("expiry")

    insert_url = f"{_SUPABASE_URL}/rest/v1/{table}"
    insert_headers = {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation",
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(insert_url, headers=insert_headers, json=row)

        if resp.status_code in (200, 201):
            returned = resp.json()
            new_id = returned[0]["id"] if returned else None
            prem_str = f"${premium:,.0f}"
            log.info(
                "[flow_store] flow_episode created: %s %s strike=%s expiry=%s "
                "prem=%s (id=%s)",
                ticker, contract_type, strike, expiry, prem_str, new_id,
            )
            if new_id is not None:
                _set_episode_in_flight(key, new_id, 1, premium)
            _episode_stats["created_episodes"] += 1
            return True

        log.warning(
            "[flow_store] flow_episode INSERT failed: %d -- %s "
            "(ticker=%s %s $%.0f %s)",
            resp.status_code, resp.text[:200],
            ticker, contract_type, strike, expiry,
        )
        return False

    except Exception as exc:
        log.warning(
            "[flow_store] flow_episode INSERT exception: %s "
            "(ticker=%s %s $%.0f %s)",
            exc, ticker, contract_type, strike, expiry,
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

    ticker     = ev_dict.get("ticker", "UNKNOWN")
    occ_symbol = ev_dict.get("occ_symbol")

    if not expiry:
        log.warning(f"[flow_store] {ticker}: expiry is empty -- OCC parse may have failed")
    if strike == 0.0:
        log.warning(f"[flow_store] {ticker}: strike=0.0 -- verify OCC parse for this symbol")

    # -----------------------------------------------------------------------
    # ING-008: capture intraday vol/OI snapshot from chain_store cache.
    # O(1) dict lookup — zero API calls. NULL on cache miss; never a gate.
    # -----------------------------------------------------------------------
    contract_volume_snapshot: Optional[int] = None
    contract_oi: Optional[int] = None
    if occ_symbol:
        try:
            from services.chain_store import get_contract_vol_oi
            contract_volume_snapshot, contract_oi = get_contract_vol_oi(occ_symbol)
        except Exception:
            pass  # enrichment failure is non-fatal

    # -----------------------------------------------------------------------
    # REARCH-003 tag group 1: bid/ask classification, vol/OI signal,
    # normalized premium. All pure helpers — zero I/O.
    # -----------------------------------------------------------------------
    fill_price       = ev_dict.get("fill_price", 0.0)
    bid              = ev_dict.get("bid", 0.0)
    ask              = ev_dict.get("ask", 0.0)
    premium          = ev_dict.get("premium", 0.0)
    underlying_price = ev_dict.get("underlying_price", 0.0)

    bid_ask_class, is_ask_side = _classify_bid_ask(fill_price, bid, ask)
    vol_oi_signal              = _compute_vol_oi_signal(contract_volume_snapshot, contract_oi)
    normalized_premium         = _compute_normalized_premium(premium, underlying_price)

    row = {
        "ticker":                   ticker,
        "contract_type":            ev_dict.get("contract_type"),
        "strike":                   strike,
        "expiry":                   expiry,
        "dte":                      ev_dict.get("dte", 0),
        "fill_price":               fill_price,
        "bid":                      bid,
        "ask":                      ask,
        "size":                     ev_dict.get("size", 0),
        "premium":                  premium,
        "trade_type":               ev_dict.get("trade_type", "UNKNOWN"),
        # REARCH-003: bid_ask_class now authoritative from _classify_bid_ask()
        "bid_ask_class":            bid_ask_class,
        "is_aggressive":            ev_dict.get("is_aggressive", False),
        "sentiment":                ev_dict.get("sentiment", "NEUTRAL"),
        "exchange_count":           ev_dict.get("exchange_count", 1),
        "fill_count":               ev_dict.get("fill_count", 1),
        "open_interest":            ev_dict.get("open_interest", 0),
        "iv":                       ev_dict.get("iv", 0.0),
        "underlying_price":         underlying_price,
        "occ_symbol":               occ_symbol,
        "is_synthetic_quote":       ev_dict.get("is_synthetic_quote", False),
        # ING-008: vol/OI enrichment — NULL on cache miss, never a gate
        "contract_volume_snapshot": contract_volume_snapshot,
        "contract_oi":              contract_oi,
        # REARCH-003: tag group 1
        "is_ask_side":              is_ask_side,
        "vol_oi_signal":            vol_oi_signal,
        "normalized_premium":       normalized_premium,
    }

    # -----------------------------------------------------------------------
    # REARCH-003 tag group 2: dte_bucket, notional_tier, event_cipher_score.
    # enrich_tags() reads dte, premium, is_ask_side, vol_oi_signal from row
    # (all already set above), mutates row in place, and returns it.
    # Local import avoids circular: flow_store ← processor ← supabase_client.
    # Failure is non-fatal — same enrichment policy as ING-008 vol/OI.
    # -----------------------------------------------------------------------
    try:
        from ingestion.processor import IngestionProcessor
        IngestionProcessor.enrich_tags(row)
    except Exception as exc:
        log.warning(
            "[flow_store] enrich_tags failed — dte_bucket/notional_tier/cipher_score NULL: %s",
            exc,
        )
        row.setdefault("dte_bucket",         None)
        row.setdefault("notional_tier",      None)
        row.setdefault("event_cipher_score", None)

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


async def _lookup_open_episode(
    ticker: str,
    direction: str,
    contract_type: str,
    strike: float,
    expiry: str,
    window_s: int = _EPISODE_MERGE_WINDOW_S,
) -> Optional[dict]:
    """
    ING-009: Query flow_episodes for an open same-session episode matching
    the merge key within the given window.

    Merge key: (ticker, direction, contract_type, strike, expiry)
    Window:    signal_ts >= now() - window_s seconds

    Returns the episode row dict (with at minimum 'id', 'trade_count',
    'total_premium') if found, else None.

    On any Supabase error: logs a warning and returns None so the caller
    falls back to INSERT — no episode is ever lost due to a lookup failure.
    This satisfies E-8 in the QA test matrix.

    NOTE: Must only be called while holding _get_episode_lock(key).
    The in-flight cache check (_episode_in_flight) should be done BEFORE
    calling this function to avoid an unnecessary DB round-trip.
    """
    if not _is_configured():
        return None

    cutoff = (
        datetime.now(timezone.utc) - timedelta(seconds=window_s)
    ).isoformat()

    get_headers = {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Accept":        "application/json",
    }

    url = (
        f"{_SUPABASE_URL}/rest/v1/flow_episodes"
        f"?ticker=eq.{quote(ticker)}"
        f"&direction=eq.{quote(direction)}"
        f"&contract_type=eq.{quote(contract_type)}"
        f"&strike=eq.{strike}"
        f"&expiry=eq.{quote(expiry)}"
        f"&signal_ts=gte.{quote(cutoff)}"
        f"&order=signal_ts.desc"
        f"&limit=1"
        f"&select=id,trade_count,total_premium,contract_oi_at_open"
    )

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=get_headers)
        if resp.status_code != 200:
            log.warning(
                "[flow_store] _lookup_open_episode GET failed: %d -- %s "
                "(ticker=%s dir=%s %s $%.0f %s) — falling back to INSERT",
                resp.status_code, resp.text[:200],
                ticker, direction, contract_type, strike, expiry,
            )
            return None
        rows = resp.json()
        return rows[0] if rows else None
    except Exception as exc:
        log.warning(
            "[flow_store] _lookup_open_episode exception: %s "
            "(ticker=%s dir=%s %s $%.0f %s) — falling back to INSERT",
            exc, ticker, direction, contract_type, strike, expiry,
        )
        return None


async def _patch_episode(
    row_id: int,
    trade_count: int,
    total_premium: float,
    new_ts: str,
    ticker: str,
    contract_type: str,
    strike: float,
    expiry: str,
    contract_volume_at_close: Optional[int] = None,
    volume_oi_ratio: Optional[float] = None,
) -> bool:
    """
    Issue the PATCH to flow_episodes for an existing episode row.
    Returns True on success (200/204), False otherwise.

    ING-008: now accepts contract_volume_at_close and volume_oi_ratio;
    both are written when not None (NULL-safe — omitted from payload if None).
    """
    patch_url = f"{_SUPABASE_URL}/rest/v1/flow_episodes?id=eq.{row_id}"
    patch_headers = {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=minimal",
    }
    payload: dict = {
        "trade_count":   trade_count,
        "total_premium": total_premium,
        "signal_ts":     new_ts,
    }
    # ING-008: only include vol/OI fields if we have values (cache hit)
    if contract_volume_at_close is not None:
        payload["contract_volume_at_close"] = contract_volume_at_close
    if volume_oi_ratio is not None:
        payload["volume_oi_ratio"] = volume_oi_ratio

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.patch(patch_url, headers=patch_headers, json=payload)
        if resp.status_code in (200, 204):
            prem_str = f"${total_premium:,.0f}"
            log.info(
                "[flow_store] flow_episode merged: %s %s strike=%s expiry=%s "
                "trade_count=%d prem=%s vol=%s oi_ratio=%s (id=%s)",
                ticker, contract_type, strike, expiry,
                trade_count, prem_str,
                contract_volume_at_close, volume_oi_ratio, row_id,
            )
            return True
        log.warning(
            "[flow_store] flow_episode PATCH failed: %d -- %s "
            "(id=%s ticker=%s %s) — episode count may be inaccurate",
            resp.status_code, resp.text[:200], row_id, ticker, contract_type,
        )
        return False
    except Exception as exc:
        log.warning(
            "[flow_store] flow_episode PATCH exception: %s (id=%s ticker=%s)",
            exc, row_id, ticker,
        )
        return False


def _compute_vol_oi_ratio(
    volume: Optional[int],
    oi: Optional[int],
) -> Optional[float]:
    """
    ING-008: Pre-compute volume_oi_ratio = volume / oi.
    Returns None when either input is None or oi == 0 (div-by-zero safe).
    """
    if volume is None or oi is None or oi == 0:
        return None
    return round(volume / oi, 4)


async def persist_flow_episode(signal_data: dict):
    """
    ING-009: Upsert one episode into flow_episodes.

    On first qualifying print for a contract within the session window:
      INSERT a new episode row via _insert_rows_with_episode_id().
      Increments _episode_stats["created_episodes"].

    On subsequent qualifying print for an open episode within
    _EPISODE_MERGE_WINDOW_S:
      PATCH the existing row:
        trade_count  += 1
        total_premium += new premium
        signal_ts     = new timestamp
      Increments _episode_stats["merged_episodes"].

    ING-009-RACE: serialised per-contract via _get_episode_lock(key).
    _episode_in_flight cache allows the second concurrent waiter to PATCH
    without a DB round-trip after the first waiter's INSERT commits.

    ING-008: vol/OI captured at INSERT (contract_oi_at_open) and PATCH
    (contract_volume_at_close + volume_oi_ratio).
    """
    if not _is_configured():
        return

    ticker        = signal_data.get("ticker", "UNKNOWN")
    direction     = signal_data.get("direction", "UNKNOWN")
    contract_type = signal_data.get("contract_type", "UNKNOWN")
    strike        = signal_data.get("strike", 0.0)
    expiry        = signal_data.get("expiry") or None
    premium       = signal_data.get("premium") or signal_data.get("total_premium", 0.0)
    signal_ts     = signal_data.get("signal_ts") or signal_data.get("timestamp") or datetime.now(timezone.utc).isoformat()
    is_multi_day  = signal_data.get("is_multi_day_repeat", False)
    occ_symbol    = signal_data.get("occ_symbol")
    alert_level   = signal_data.get("alert_level", "WATCH")
    is_accelerating = signal_data.get("is_accelerating", False)
    seed_episode  = signal_data.get("seed_episode")

    # ING-008: vol/OI at episode open
    contract_oi_at_open: Optional[int] = None
    if occ_symbol:
        try:
            from services.chain_store import get_contract_vol_oi
            _, contract_oi_at_open = get_contract_vol_oi(occ_symbol)
        except Exception:
            pass

    key  = _episode_key(ticker, direction, contract_type, strike, expiry or "")
    lock = _get_episode_lock(key)

    async with lock:
        # --- Check in-flight cache first (avoids DB round-trip on race) ---
        in_flight = _episode_in_flight.get(key)
        if in_flight:
            new_trade_count  = in_flight["trade_count"] + 1
            new_total_prem   = in_flight["total_premium"] + premium
            row_id           = in_flight["id"]

            # ING-008: vol/OI at close (latest available)
            contract_volume_at_close: Optional[int] = None
            volume_oi_ratio: Optional[float] = None
            if occ_symbol:
                try:
                    from services.chain_store import get_contract_vol_oi
                    contract_volume_at_close, oi_snap = get_contract_vol_oi(occ_symbol)
                    volume_oi_ratio = _compute_vol_oi_ratio(contract_volume_at_close, contract_oi_at_open)
                except Exception:
                    pass

            ok = await _patch_episode(
                row_id=row_id,
                trade_count=new_trade_count,
                total_premium=new_total_prem,
                new_ts=signal_ts,
                ticker=ticker,
                contract_type=contract_type,
                strike=strike,
                expiry=expiry or "",
                contract_volume_at_close=contract_volume_at_close,
                volume_oi_ratio=volume_oi_ratio,
            )
            if ok:
                _set_episode_in_flight(key, row_id, new_trade_count, new_total_prem)
                _episode_stats["merged_episodes"] += 1
            return

        # --- No in-flight entry: check DB ---
        existing = await _lookup_open_episode(ticker, direction, contract_type, strike, expiry or "")

        if existing:
            row_id           = existing["id"]
            new_trade_count  = existing["trade_count"] + 1
            new_total_prem   = existing["total_premium"] + premium

            contract_volume_at_close: Optional[int] = None
            volume_oi_ratio: Optional[float] = None
            if occ_symbol:
                try:
                    from services.chain_store import get_contract_vol_oi
                    contract_volume_at_close, _ = get_contract_vol_oi(occ_symbol)
                    oi_open = existing.get("contract_oi_at_open")
                    volume_oi_ratio = _compute_vol_oi_ratio(contract_volume_at_close, oi_open)
                except Exception:
                    pass

            ok = await _patch_episode(
                row_id=row_id,
                trade_count=new_trade_count,
                total_premium=new_total_prem,
                new_ts=signal_ts,
                ticker=ticker,
                contract_type=contract_type,
                strike=strike,
                expiry=expiry or "",
                contract_volume_at_close=contract_volume_at_close,
                volume_oi_ratio=volume_oi_ratio,
            )
            if ok:
                _set_episode_in_flight(key, row_id, new_trade_count, new_total_prem)
                _episode_stats["merged_episodes"] += 1
            return

        # --- No existing episode: INSERT via _insert_rows_with_episode_id ---
        insert_row: dict = {
            "ticker":              ticker,
            "direction":           direction,
            "contract_type":       contract_type,
            "strike":              strike,
            "expiry":              expiry,
            "total_premium":       premium,
            "trade_count":         signal_data.get("trade_count", 1),
            "signal_ts":           signal_ts,
            "is_multi_day_repeat": is_multi_day,
            "alert_level":         alert_level,
            "is_accelerating":     is_accelerating,
            "seed_episode":        seed_episode,
        }
        if contract_oi_at_open is not None:
            insert_row["contract_oi_at_open"] = contract_oi_at_open
        if occ_symbol:
            insert_row["occ_symbol"] = occ_symbol

        await _insert_rows_with_episode_id(
            "flow_episodes",
            insert_row,
            key,
            premium,
            contract_oi_at_open,
        )


async def _update_episode_multiday(
    ticker: str,
    contract_type: str,
    strike: float,
    expiry: str,
    is_multi_day_repeat: bool,
) -> None:
    """
    ING-007: Two-step GET+PATCH to set is_multi_day_repeat on the most
    recent flow_episodes row for a contract (PBE-F2 fix).

    Step 1: GET the bigserial id of the target row (order=signal_ts.desc, limit=1).
    Step 2: PATCH that specific row by id.

    Logs at INFO on GET-returns-empty (SA-F3) so cold-miss rate is observable.
    """
    if not _is_configured():
        return

    get_headers = {
        "apikey":        _SUPABASE_KEY,
        "Authorization": f"Bearer {_SUPABASE_KEY}",
        "Accept":        "application/json",
    }
    get_url = (
        f"{_SUPABASE_URL}/rest/v1/flow_episodes"
        f"?ticker=eq.{quote(ticker)}"
        f"&contract_type=eq.{quote(contract_type)}"
        f"&strike=eq.{strike}"
        f"&expiry=eq.{quote(expiry)}"
        f"&order=signal_ts.desc"
        f"&limit=1"
        f"&select=id"
    )

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
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
            log.info(  # SA-F3: elevated from DEBUG so cold-miss is observable in Railway
                "[flow_store] _update_episode_multiday: no episode row found "
                "(ticker=%s %s $%.0f %s) — skipping multiday patch",
                ticker, contract_type, strike, expiry,
            )
            return

        row_id = rows[0].get("id")
        if row_id is None:
            log.warning(
                "[flow_store] _update_episode_multiday: row missing 'id' field "
                "(ticker=%s %s $%.0f %s) — skipping multiday patch",
                ticker, contract_type, strike, expiry,
            )
            return

        patch_url = f"{_SUPABASE_URL}/rest/v1/flow_episodes?id=eq.{row_id}"
        patch_headers = {
            "apikey":        _SUPABASE_KEY,
            "Authorization": f"Bearer {_SUPABASE_KEY}",
            "Content-Type":  "application/json",
            "Prefer":        "return=minimal",
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            patch_resp = await client.patch(
                patch_url,
                headers=patch_headers,
                json={"is_multi_day_repeat": is_multi_day_repeat},
            )

        if patch_resp.status_code in (200, 204):
            log.info(
                "[flow_store] flow_episode multiday updated: %s %s $%.0f %s → %s (id=%d)",
                ticker, contract_type, strike, expiry, is_multi_day_repeat, row_id,
            )
        else:
            log.warning(
                "[flow_store] flow_episode multiday PATCH failed: %d -- %s (id=%d)",
                patch_resp.status_code, patch_resp.text[:200], row_id,
            )

    except Exception as exc:
        log.warning(
            "[flow_store] _update_episode_multiday exception: %s "
            "(ticker=%s %s $%.0f %s)",
            exc, ticker, contract_type, strike, expiry,
        )


async def start_lookback_worker(accumulator=None) -> None:
    """
    ING-007: Background worker that drains _lookback_queue and patches
    flow_episodes rows with is_multi_day_repeat.

    Each item dequeued is a ContractKey = (ticker, contract_type, strike, expiry).
    The worker fetches the lookback result from contract_day_cache, applies a
    quality filter (min_premium from accumulator DTE tiers), then calls
    _update_episode_multiday() on the most recent episode row for that contract.

    SA-F1 fix: correct attr name (_dte_tiers, not _dte_premium_tiers) and
    correct traversal for List[Tuple[int, Dict[int, float]]].

    PBE-F5 fix: log the resolved min_premium per key at DEBUG.

    ING-007-PATCH (2026-05-10): get_lookback is called via module reference
    (_contract_day_cache.get_lookback) so that unittest.mock.patch(
    'utils.contract_day_cache.get_lookback') correctly intercepts the call.
    A `from ... import get_lookback` local binding would shadow the patch.
    """
    log.info("[lookback_worker] starting")

    # SA-F1: correct attr name + correct traversal
    dte_tiers = getattr(accumulator, "_dte_tiers", None) or []
    try:
        min_premium: float = min(
            floor
            for _, floors in dte_tiers
            for floor in floors.values()
        )
    except (ValueError, TypeError):
        min_premium = 10_000.0  # hardcoded floor when accumulator has no tiers

    min_days: int = getattr(accumulator, "_multi_day_min_days", 2)

    while True:
        try:
            key = await _lookback_queue.get()
            ticker, contract_type, strike, expiry = key

            # PBE-F5: log resolved floor so Railway surfaces which tier applies
            log.debug(
                "[lookback_worker] processing %s %s $%.0f %s (min_premium=%.0f)",
                ticker, contract_type, strike, expiry, min_premium,
            )

            # ING-007: fetch via module reference so patch() intercepts at call time.
            # A local `from utils.contract_day_cache import get_lookback` would
            # bind a name that bypasses any patch on the module attribute.
            try:
                result = await _contract_day_cache.get_lookback(key, min_premium)
            except Exception as exc:
                log.debug("[lookback_worker] get_lookback error for %s: %s", key, exc)
                _lookback_queue.task_done()
                continue

            if result is None:
                _lookback_queue.task_done()
                continue

            is_repeat = result.prior_days_active >= min_days

            await _update_episode_multiday(
                ticker=ticker,
                contract_type=contract_type,
                strike=strike,
                expiry=expiry,
                is_multi_day_repeat=is_repeat,
            )

        except asyncio.CancelledError:
            log.info("[lookback_worker] cancelled — shutting down")
            break
        except Exception as exc:
            log.warning("[lookback_worker] unhandled exception: %s", exc)
        finally:
            try:
                _lookback_queue.task_done()
            except ValueError:
                pass


async def start_flow_writer() -> None:
    """
    Entry point — call once from main.py lifespan.
    Starts the bus listener and the periodic flush task.
    """
    asyncio.create_task(_flush_flow_events())
    asyncio.create_task(_bus_signal_listener())
    log.info("[flow_store] flow writer started")


async def _bus_signal_listener() -> None:
    """
    Retained for future use. Acts as a no-op consumer of the db_writer
    channel after EPISODE-FIX moved persist_flow_episode() to _process_trade().

    TEST-FIX (2026-05-10): AsyncEventBus.subscribe() returns a plain
    asyncio.Queue — it does NOT implement the async context manager protocol.
    Replaced `async with bus.subscribe(...) as queue` with explicit
    bus.subscribe() / bus.unsubscribe() in a try/finally block.
    """
    queue = bus.subscribe("db_writer")
    try:
        while True:
            try:
                await queue.get()
                # no-op: episode persistence moved to _process_trade() (EPISODE-FIX)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("[flow_store] _bus_signal_listener exception: %s", exc)
    finally:
        bus.unsubscribe("db_writer", queue)


# ---------------------------------------------------------------------------
# Module-level async helpers (used by tests and WebSocket consumers)
# ---------------------------------------------------------------------------

_store = FlowStore()

_MAX_FLOWS_PER_TICKER = 200
_FLOW_TTL_S = 86_400  # 24 hours


async def add_flow(flow: dict) -> None:
    """Add a flow event to the in-memory store."""
    _store.add_flow(flow)


async def get_flows(ticker: str) -> list:
    """Return all stored flows for ticker (not expired)."""
    now = __import__("time").time()
    return [
        f for f in _store.get_flows()
        if isinstance(f, dict)
        and f.get("ticker") == ticker
        and (now - f.get("timestamp", now)) < _FLOW_TTL_S
    ]


async def clear_flows() -> None:
    """Clear all in-memory flows."""
    _store.clear()
