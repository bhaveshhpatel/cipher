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
  11. REARCH-003 (2026-05-11): added classify_bid_ask(), compute_vol_oi_signal(),
      and quality tag fields (is_ask_side, bid_ask_class, vol_oi_signal,
      normalized_premium, normalized_oi, dte_bucket, notional_tier) to
      persist_flow_event() row dict.
  12. SA-5 (2026-05-11): three code sites independently divided vol/OI with no
      shared helper. Added _compute_vol_oi_ratio(vol, oi) -> Optional[float] as
      the single source of truth (round(vol/oi, 4), None on bad inputs).
      compute_vol_oi_signal() now delegates to _compute_vol_oi_ratio() instead
      of re-implementing the division. persist_flow_event() normalized_oi block
      replaced with _compute_vol_oi_ratio(open_interest, contract_oi), fixing
      rounding from 6dp to 4dp to match the test spec.
  13. PBE-1 (2026-05-11): public/private API type inversion corrected.
      classify_bid_ask() is now the PUBLIC function returning Tuple[str, bool]
      (bid_ask_class, is_ask_side) — callers get both values from one call.
      _classify_bid_ask() is now the PRIVATE shim.
      persist_flow_event() updated to unpack the tuple from classify_bid_ask().
  14. REARCH-003-ENUM (2026-05-11): dte_bucket and notional_tier were missing
      from persist_flow_event() row dict entirely — new rows were written with
      NULL for both columns despite the columns existing in the schema.
      Fixed by importing _compute_dte_bucket/_compute_notional_tier from
      processor and computing both inline before the row dict is built.
  15. FS-TEST-FIX (2026-05-11): three test failures addressed:
      a) FlowStore class added — tests 5-12 in test_flow_store.py.
      b) persist_flow_episode() signature changed to accept a single signal_data
         dict. Empty expiry string coerced to None.
      c) asyncio.sleep alias _async_sleep added for safe test patching.
  16. FAS-001 / FS-035 / FS-HANG (2026-05-11): three additional test fixes:
      a) FAS-001: _classify_bid_ask() now returns Tuple[str, bool] (full tuple)
         instead of str only. All 8 test_classify_bid_ask_* tests destructure
         the return as `cls, is_ask = ...` — returning str caused ValueError.
      b) FS-035: persist_flow_episode() INSERT now passes json=insert_payload
         (dict) instead of json=[insert_payload] (list). Test reads
         call_kwargs.kwargs.get("json") and asserts row["ticker"] — a list
         can't be subscripted by string key. Response unwrap handles both
         dict and list shapes from PostgREST.
      c) FS-HANG: start_flow_writer() now returns early when not configured
         (no tasks spawned). Previously, start_lookback_worker() was created
         unconditionally and blocked forever on _lookback_queue.get(),
         keeping the pytest event loop alive past teardown and causing CI
         timeout.
  17. ING-007-SIG (2026-05-11): start_lookback_worker() now accepts an optional
      accumulator argument. When provided (test path), it is used directly.
      When None (production/main.py zero-arg call), falls back to
      get_accumulator(). This restores test compatibility broken by FS-HANG
      fix which dropped the parameter entirely.
  18. ING-009-EXTRACT (2026-05-11): extracted _insert_rows_with_episode_id()
      from the inlined INSERT block inside persist_flow_episode().
      Tests in test_ing009_episode_upsert.py patch
      fs._insert_rows_with_episode_id — the function did not exist as a
      standalone module attribute, causing AttributeError on every INSERT-path
      test (E-1, E-4 through E-9, E-11 through E-13, E-15 through E-16).
      The new function:
        - POSTs with Prefer: return=representation
        - Stores the PostgREST-returned id in _episode_in_flight if present
        - Returns True on HTTP 200/201, False otherwise
      persist_flow_episode increments created_episodes after the call returns
      True (counter stays at the call site so mocking the helper still
      triggers the counter correctly — E-1 asserts created_episodes == 1
      after patching _insert_rows_with_episode_id to return True).
  19. ING-009-GUARD (2026-05-11): removed _is_configured() early-return guard
      from persist_flow_episode(). When SUPABASE_URL/KEY are unset (test env),
      the guard caused the function to return immediately — all 16 E-* tests
      saw created_episodes == 0 and merged_episodes == 0.
      The guard is redundant here: _lookup_open_episode() and
      _insert_rows_with_episode_id() each check _is_configured() internally
      and return None / False safely. The in-process counter increments and
      lock/in-flight logic must always run regardless of DB connectivity.
  20. PBE-1-SHIM-FIX (2026-05-11): _classify_bid_ask() shim was returning
      classify_bid_ask(...)[0] (a plain str). Both test_flow_and_stats.py
      (lines 125-174, 8 tests) and test_rearch003_event_quality_tags.py
      (test_private_shim_returns_tuple) unpack the result as:
          cls, is_ask = fs._classify_bid_ask(...)
      Unpacking a 3-char string 'ASK' into 2 variables gives:
          ValueError: too many values to unpack (expected 2)
      Fix: _classify_bid_ask() returns the full Tuple[str, bool] — identical
      to classify_bid_ask(). There is no test_private_shim_returns_str_only
      in the live test suite; the docstring reference was historical only.
  21. IMPORT-HOIST (2026-05-11): lazy inline imports of get_contract_vol_oi,
      _compute_dte_bucket, and _compute_notional_tier moved from inside
      persist_flow_event() / persist_flow_episode() to top-level module
      imports. Inline imports inside hot-path async functions re-run the
      module lookup on every call. Hoisting removes per-call overhead and
      makes the dependency graph explicit.
  22. REARCH-004 (2026-05-11): episode quality aggregate columns added.
      _set_episode_in_flight() gains ask_side_count: int parameter.
      _episode_in_flight cache now carries ask_side_count.
      _lookup_open_episode() select= extended to include ask_side_count.
      persist_flow_episode() INSERT path seeds ask_side_count (1 if is_ask_side
      else 0), ask_side_pct (round(ask_side_count/1, 4) = 0.0 or 1.0),
      dte_bucket, and notional_tier from the seed event.
      persist_flow_episode() PATCH paths (both in-flight and DB-lookup)
      increment ask_side_count and recompute ask_side_pct. dte_bucket and
      notional_tier are intentionally excluded from all PATCH payloads —
      they are locked at episode open (seed-event-only semantics, SA-3).
      Pre-REARCH rows with NULL ask_side_count are handled via COALESCE(NULL,0)
      in the DB-lookup PATCH path (PBE-1 NULL contract).
      _insert_rows_with_episode_id() passes ask_side_count to
      _set_episode_in_flight() so the in-flight cache carries the seeded value.

PRE-MERGE BLOCKER FIXES (2026-05-04):
  SA-F1: start_lookback_worker() was calling
    getattr(accumulator, "_dte_premium_tiers", None)
    The attribute is named "_dte_tiers" on RepetitionAccumulator.
    Fix: correct attr name + traverse list-of-tuples.

  PBE-F2: _update_episode_multiday() was constructing a PostgREST PATCH URL
    with &order=signal_ts.desc&limit=1 appended. PostgREST silently ignores
    order/limit modifiers on PATCH requests — ALL rows matching the filter
    were being updated.
    Fix: two-step GET+PATCH by id.

DELIBERATION INLINE FIXES (2026-05-04):
  SA-F3: elevated cold-miss log from DEBUG to INFO.
  PBE-F5: added log.debug for resolved min_premium in start_lookback_worker.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import httpx

from core.async_bus import bus  # module-level import so patch('services.flow_store.bus') works
from services.chain_store import get_contract_vol_oi
from ingestion.processor import _compute_dte_bucket, _compute_notional_tier

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

# Patchable alias for asyncio.sleep — tests patch 'services.flow_store._async_sleep'
# instead of the global asyncio.sleep to avoid interfering with the event loop.
_async_sleep = asyncio.sleep

# ---------------------------------------------------------------------------
# ING-009: same-session episode merge window
# ---------------------------------------------------------------------------
_EPISODE_MERGE_WINDOW_S: int = 1800

# ---------------------------------------------------------------------------
# ING-009-RACE: per-contract locks and in-flight cache
# ---------------------------------------------------------------------------
_episode_locks: Dict[str, asyncio.Lock] = {}
_episode_in_flight: Dict[str, dict] = {}

# ---------------------------------------------------------------------------
# REARCH-003: bid/ask classification thresholds
# ---------------------------------------------------------------------------
_BID_ASK_ASK_THRESHOLD: float = 0.98   # fill >= ask * 0.98 → ASK  (boundary inclusive)
_BID_ASK_BID_THRESHOLD: float = 1.02   # fill <= bid * 1.02 → BID
VOL_OI_HIGH_THRESHOLD:  float = 0.5    # vol/OI >= 0.5      → True / "HIGH"


# ---------------------------------------------------------------------------
# FlowStore — in-memory store used by tests and legacy callers
# ---------------------------------------------------------------------------

class FlowStore:
    """
    Simple in-memory store for options flow dicts.

    Used directly by tests and by the module-level add_flow / get_flows /
    clear_flows helpers below.  Not related to the Supabase persistence path
    (persist_flow_event / persist_flow_episode).
    """

    def __init__(self) -> None:
        self._flows: List[dict] = []

    def add_flow(self, flow: dict) -> None:
        """Append a flow dict to the in-memory store."""
        self._flows.append(flow)

    def get_flows(self, symbol: Optional[str] = None) -> List[dict]:
        """Return a shallow copy of all stored flows (optionally filtered by ticker)."""
        if symbol is not None:
            return [
                f for f in self._flows
                if (f.get("ticker") == symbol or f.get("symbol") == symbol)
            ]
        return list(self._flows)

    def get_flows_by_symbol(self, symbol: str) -> List[dict]:
        """Return flows matching *symbol* via dict key or object attribute."""
        result = []
        for f in self._flows:
            if isinstance(f, dict):
                if f.get("symbol") == symbol or f.get("ticker") == symbol:
                    result.append(f)
            else:
                if getattr(f, "symbol", None) == symbol:
                    result.append(f)
        return result

    def get_stats(self) -> dict:
        """Return aggregate stats dict."""
        return {"total": len(self._flows)}

    def clear(self) -> None:
        """Remove all stored flows."""
        self._flows.clear()

    def size(self) -> int:
        """Return the number of stored flows."""
        return len(self._flows)


# Module-level FlowStore instance used by the async helpers below.
_store = FlowStore()


# ---------------------------------------------------------------------------
# Module-level async helpers (thin wrappers around _store)
# ---------------------------------------------------------------------------

async def add_flow(flow: dict) -> None:
    """Append *flow* to the module-level in-memory store."""
    _store.add_flow(flow)


async def get_flows(ticker: str) -> List[dict]:
    """Return flows for *ticker* from the module-level in-memory store."""
    return _store.get_flows(ticker)


async def clear_flows() -> None:
    """Clear all flows from the module-level in-memory store."""
    _store.clear()


# ---------------------------------------------------------------------------
# REARCH-003: public API
# ---------------------------------------------------------------------------

def classify_bid_ask(
    fill_price: float,
    bid: float,
    ask: float,
) -> Tuple[str, bool]:
    """
    REARCH-003 PUBLIC API — classify a fill and return (bid_ask_class, is_ask_side).

    PBE-1: Returns a tuple so callers get both the class label and the boolean
    flag in a single call — no need to re-derive is_ask_side from the string
    at every call site.

    Thresholds (Steamroom deliberation 2026-05-11):
      fill >= ask * 0.98  → ('ASK', True)   (buyer at or above near-ask band)
      fill <= bid * 1.02  → ('BID', False)  (seller hit at or near bid)
      otherwise           → ('MID', False)

    Guard ordering (DO NOT reorder):
      1. Zero/bad-quote guard  (ask <= 0 or bid <= 0) must come first.
      2. Crossed-market guard  (bid > ask) must follow Guard 1.
      3. ASK/BID threshold checks last, on clean valid quotes only.

    Returns: Tuple[bid_ask_class: str, is_ask_side: bool]
    """
    # Guard 1: synthetic/bad quote (zero or missing bid/ask)
    if not ask or ask <= 0 or not bid or bid <= 0:
        return ("MID", False)
    # Guard 2: crossed market
    if bid > ask:
        return ("MID", False)
    # Guard 3: threshold classification on clean quotes
    if fill_price >= ask * _BID_ASK_ASK_THRESHOLD:
        return ("ASK", True)
    if fill_price <= bid * _BID_ASK_BID_THRESHOLD:
        return ("BID", False)
    return ("MID", False)


def _compute_vol_oi_ratio(
    vol: Optional[int],
    oi: Optional[int],
) -> Optional[float]:
    """
    REARCH-003 / SA-5: Single source of truth for vol/OI division.

    Returns round(vol / oi, 4) or None when inputs are unavailable.
    None is returned when vol is None, oi is None, or oi == 0.
    """
    if vol is None or oi is None or oi == 0:
        return None
    return round(vol / oi, 4)


def compute_vol_oi_signal(
    vol: Optional[int],
    oi: Optional[int],
) -> Optional[bool]:
    """
    REARCH-003: Return True when intraday vol/OI ratio meets the high-activity
    threshold, False when below it, None when data is unavailable.

    Return type is Optional[bool] — maps directly to flow_events.vol_oi_signal
    BOOLEAN DEFAULT NULL (migration 026).
    """
    ratio = _compute_vol_oi_ratio(vol, oi)
    if ratio is None:
        return None
    return ratio >= VOL_OI_HIGH_THRESHOLD


# ---------------------------------------------------------------------------
# REARCH-003: private aliases used by tests
# ---------------------------------------------------------------------------

def _classify_bid_ask(
    fill_price: float,
    bid: Optional[float],
    ask: Optional[float],
) -> Tuple[str, bool]:
    """
    PRIVATE SHIM — returns Tuple[str, bool] (bid_ask_class, is_ask_side).

    PBE-1-SHIM-FIX: Both test suites unpack the result as:
        cls, is_ask = fs._classify_bid_ask(...)

    Specifically:
      - test_flow_and_stats.py (8 tests, lines 125-174)
      - test_rearch003_event_quality_tags.py (test_private_shim_returns_tuple)

    Returning a plain str caused ValueError: too many values to unpack because
    Python unpacks a 3-char string 'ASK' into ('A', 'S', 'K') — 3 values into
    2 variables fails.

    This shim delegates to classify_bid_ask() and returns the full tuple.
    Coerces None bid/ask to 0 so the bad-quote guard fires on missing quotes.

    Do NOT call from production paths — use classify_bid_ask() directly.
    """
    return classify_bid_ask(fill_price, bid or 0, ask or 0)


def _compute_vol_oi_signal(
    vol: Optional[int],
    oi: Optional[int],
    threshold: float = VOL_OI_HIGH_THRESHOLD,
) -> str:
    """
    Private alias for test_flow_and_stats.py.
    Returns 'HIGH' | 'NORMAL' | 'UNKNOWN' (string, not bool/None).
    Accepts optional threshold override.
    """
    if vol is None or oi is None or oi == 0:
        return "UNKNOWN"
    return "HIGH" if (vol / oi) >= threshold else "NORMAL"


def _compute_normalized_premium(
    premium: Optional[float],
    underlying_price: Optional[float],
) -> Optional[float]:
    """
    Private alias for test_flow_and_stats.py.
    Returns round(premium / underlying_price, 4) or None on bad inputs.
    """
    if premium is None or underlying_price is None or underlying_price == 0:
        return None
    return round(premium / underlying_price, 4)


# ---------------------------------------------------------------------------
# Episode helpers
# ---------------------------------------------------------------------------

def _episode_key(ticker: str, direction: str, contract_type: str, strike: float, expiry: str) -> str:
    """Stable merge-key string for _episode_locks and _episode_in_flight."""
    return f"{ticker}|{direction}|{contract_type}|{strike}|{expiry}"


def _get_episode_lock(key: str) -> asyncio.Lock:
    """Return the existing lock for key, or create one atomically."""
    if key not in _episode_locks:
        _episode_locks[key] = asyncio.Lock()
    return _episode_locks[key]


def _set_episode_in_flight(
    key: str,
    row_id: int,
    trade_count: int,
    total_premium: float,
    ask_side_count: int,
) -> None:
    """
    Cache the just-inserted/patched episode so concurrent waiters skip the DB GET.

    REARCH-004: ask_side_count is now carried in the in-flight cache so that
    incremental PATCH recomputation of ask_side_pct requires no DB round-trip.
    """
    _episode_in_flight[key] = {
        "id":             row_id,
        "trade_count":    trade_count,
        "total_premium":  total_premium,
        "ask_side_count": ask_side_count,
    }


def reset_episode_state() -> None:
    """
    Clear the in-flight cache and lock dict for session reset (market-open boundary).
    Called from the market-open handler and from tests.
    """
    _episode_in_flight.clear()
    _episode_locks.clear()


# ---------------------------------------------------------------------------
# ING-007: async lookback enrichment queue
# ---------------------------------------------------------------------------
_LOOKBACK_QUEUE_MAX = 5_000
_lookback_queue: asyncio.Queue = asyncio.Queue(maxsize=_LOOKBACK_QUEUE_MAX)
_lookback_stats: Dict[str, int] = {
    "lookback_queued":         0,
    "lookback_queue_overflow": 0,
}

# ---------------------------------------------------------------------------
# ING-009: episode upsert stats
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
    Never raises — overflow is silently counted.
    """
    try:
        _lookback_queue.put_nowait(key)
        _lookback_stats["lookback_queued"] += 1
    except asyncio.QueueFull:
        _lookback_stats["lookback_queue_overflow"] += 1
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
            await _async_sleep(_RETRY_DELAY_S)  # patched via services.flow_store._async_sleep
    log.error(
        f"[flow_store] insert into {table} failed after {_RETRY_MAX} attempts "
        f"-- {len(rows)} rows DISCARDED. Check Supabase connectivity."
    )
    return False


async def _flush_flow_events():
    global _flow_event_buffer
    while True:
        await _async_sleep(_FLUSH_INTERVAL)
        if not _flow_event_buffer:
            continue
        batch = _flow_event_buffer[:_FLUSH_MAX_ROWS]
        _flow_event_buffer = _flow_event_buffer[_FLUSH_MAX_ROWS:]
        ok = await _insert_rows_with_retry("flow_events", batch)
        if ok:
            log.info(f"[flow_store] flushed {len(batch)} flow_events to DB")


async def persist_flow_event(ev_dict):
    global _flow_event_buffer

    if not _is_configured():
        log.warning(
            "[flow_store] persist_flow_event: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY "
            "not set — event dropped. Set env vars to enable DB persistence."
        )
        return
    # EV-OBJ-001: normalize OptionsFlowEvent → dict so .get() calls work
    # regardless of whether caller passes a dataclass/object or a plain dict.
    if not isinstance(ev_dict, dict):
        ev_dict = vars(ev_dict)

    # FIX-PERSIST-TYPE: normalise dict vs OptionsFlowEvent dataclass.
    if isinstance(ev, dict):
        _g = ev.get
    else:
        def _g(key, default=None):
            return getattr(ev, key, default)

    expiry = _g("expiry") or None
    strike = _g("strike")

    ticker     = _g("ticker", "UNKNOWN")
    occ_symbol = _g("occ_symbol") or _g("id")  # occ_symbol preferred; id fallback for legacy dicts

    if not expiry:
        log.warning(f"[flow_store] {ticker}: expiry is empty -- OCC parse may have failed")
    if strike == 0.0:
        log.warning(f"[flow_store] {ticker}: strike=0.0 -- verify OCC parse for this symbol")

    # ING-008: capture intraday vol/OI snapshot from chain_store cache.
    contract_volume_snapshot: Optional[int] = None
    contract_oi: Optional[int] = None
    if occ_symbol:
        try:
            contract_volume_snapshot, contract_oi = get_contract_vol_oi(occ_symbol)
        except Exception:
            pass

    # REARCH-003: compute quality dimension tags.
    fill_price       = _g("fill_price", 0.0) or 0.0
    bid              = _g("bid", 0.0) or 0.0
    ask              = _g("ask", 0.0) or 0.0
    underlying_price = _g("underlying_price", 0.0) or 0.0
    premium          = _g("premium", 0.0) or 0.0
    dte              = _g("dte")

    open_interest = _g("open_interest") or None

    # PBE-1: classify_bid_ask() returns Tuple[str, bool] — unpack directly.
    bid_ask_cls, is_ask_side = classify_bid_ask(fill_price, bid, ask)

    vol_oi_signal: Optional[bool] = compute_vol_oi_signal(
        contract_volume_snapshot, contract_oi
    )

    normalized_premium: Optional[float] = _compute_normalized_premium(premium, underlying_price)

    normalized_oi: Optional[float] = _compute_vol_oi_ratio(open_interest, contract_oi)

    dte_bucket    = _compute_dte_bucket(dte)
    notional_tier = _compute_notional_tier(premium)

    # Execution mechanic: derive from bid_ask_class and is_aggressive for
    # columns added in migration 012 that have NOT NULL defaults.
    # We write the correct derived value rather than relying solely on the DB default.
    trade_type = _g("trade_type", "UNKNOWN") or "UNKNOWN"
    is_agg     = bool(_g("is_aggressive", False))
    _bac       = bid_ask_cls  # already resolved above
    if _bac == "ASK" and is_agg:
        execution_mechanic = "AGGRESSIVE_BUY"
    elif _bac == "BID" and is_agg:
        execution_mechanic = "AGGRESSIVE_SELL"
    elif _bac == "ASK":
        execution_mechanic = "PASSIVE_BUY"
    elif _bac == "BID":
        execution_mechanic = "PASSIVE_SELL"
    else:
        execution_mechanic = "AMBIGUOUS_LONG"

    sentiment = _g("sentiment", "NEUTRAL") or "NEUTRAL"
    # strong_sentiment: True when ask-side aggressive on a non-synthetic quote
    strong_sentiment = bool(
        is_ask_side and is_agg and not bool(_g("is_synthetic_quote", False))
    )

    global _flow_event_buffer
    row = {
        "ticker":                   ticker,
        "contract_type":            _g("contract_type"),
        "strike":                   strike,
        "expiry":                   expiry,
        "dte":                      _g("dte", 0),
        "fill_price":               fill_price,
        "bid":                      bid,
        "ask":                      ask,
        "size":                     _g("size", 0),
        "premium":                  premium,
        "trade_type":               trade_type,
        "bid_ask_class":            bid_ask_cls,
        "is_ask_side":              is_ask_side,
        "is_aggressive":            is_agg,
        "sentiment":                sentiment,
        "order_side":               _g("order_side", "UNKNOWN") or "UNKNOWN",
        "strong_sentiment":         strong_sentiment,
        "execution_mechanic":       execution_mechanic,
        "exchange_count":           _g("exchange_count", 1),
        "fill_count":               _g("fill_count", 1),
        "open_interest":            _g("open_interest", 0),
        "iv":                       _g("iv", 0.0),
        "underlying_price":         underlying_price,
        "occ_symbol":               occ_symbol,
        "is_synthetic_quote":       bool(_g("is_synthetic_quote", False)),
        "quote_source":             "live",
        "contract_volume_snapshot": contract_volume_snapshot,
        "contract_oi":              contract_oi,
        "vol_oi_signal":            vol_oi_signal,
        "normalized_premium":       normalized_premium,
        "normalized_oi":            normalized_oi,
        "dte_bucket":               dte_bucket,
        "notional_tier":            notional_tier,
    }
    _flow_event_buffer.append(row)

    if len(_flow_event_buffer) >= _FLUSH_MAX_ROWS:
        batch = _flow_event_buffer[:_FLUSH_MAX_ROWS]
        _flow_event_buffer = _flow_event_buffer[_FLUSH_MAX_ROWS:]
        ok = await _insert_rows_with_retry("flow_events", batch)
        if ok:
            log.info(f"[flow_store] early flush ({_FLUSH_MAX_ROWS} rows) -- buffer hit max")


async def upgrade_to_sweep_in_db(occ_symbol: str, fill_price: float, size: int) -> bool:
    """C-003: Retroactively upgrade flow_events rows to trade_type='SWEEP'."""
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
    ING-009: Query flow_episodes for an open same-session episode.

    REARCH-004: select= extended to include ask_side_count so the DB-lookup
    PATCH path can recompute ask_side_pct without an additional round-trip.
    Pre-REARCH rows return NULL for ask_side_count — callers must COALESCE
    with 0 before incrementing (PBE-1 NULL contract).
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
        f"&order=signal_ts.desc&limit=1"
        f"&select=id,trade_count,total_premium,ask_side_count"
    )

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=get_headers)
        if resp.status_code == 200:
            rows = resp.json()
            return rows[0] if rows else None
        log.warning(
            f"[flow_store] _lookup_open_episode failed: {resp.status_code} -- {resp.text[:200]}"
        )
        return None
    except Exception as e:
        log.error(f"[flow_store] _lookup_open_episode exception: {e}")
        return None


async def _insert_rows_with_episode_id(
    table: str,
    row: dict,
    key: str,
    premium: float,
    current_oi: Optional[int] = None,
    ask_side_count: int = 0,
) -> bool:
    """
    ING-009-EXTRACT: POST a single episode row to *table* with
    Prefer: return=representation so PostgREST echoes back the generated id.

    On success:
      - If the response body contains an id field, populate _episode_in_flight
        so concurrent waiters for the same key can PATCH without a DB round-trip.
      - If the response body has no id (e.g. return=minimal fallback from an
        older PostgREST version), _episode_in_flight is NOT populated — the
        next waiter safely falls back to _lookup_open_episode (E-16 safe path).

    REARCH-004: ask_side_count parameter added. Passed through to
    _set_episode_in_flight() so the in-flight cache carries the seeded value
    from the INSERT (0 or 1 depending on whether the seed event was ask-side).

    Returns True on HTTP 200/201, False otherwise.

    NOTE: Does NOT increment _episode_stats["created_episodes"] — that counter
    lives in persist_flow_episode() so patching this function in tests still
    triggers the counter correctly (E-1 asserts created_episodes == 1 after
    mocking _insert_rows_with_episode_id to return True).
    """
    if not _SUPABASE_URL or not _SUPABASE_KEY:
        return False

    insert_headers = {**_headers(), "Prefer": "return=representation"}
    url = f"{_SUPABASE_URL}/rest/v1/{table}"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, headers=insert_headers, json=row)

        if resp.status_code in (200, 201):
            try:
                row_data = resp.json()
                # PostgREST may return a list or a single object depending on version.
                row_obj = row_data[0] if isinstance(row_data, list) else row_data
                if row_obj and "id" in row_obj:
                    _set_episode_in_flight(
                        key,
                        row_obj["id"],
                        row.get("trade_count") or 1,
                        premium,
                        ask_side_count,
                    )
                    log.debug(
                        f"[flow_store] episode INSERT id={row_obj['id']} cached in-flight: {key}"
                    )
                else:
                    log.debug(
                        f"[flow_store] episode INSERT: no id in response body for {key} "
                        f"— in-flight not populated (safe fallback)"
                    )
            except Exception as parse_exc:
                log.debug(
                    f"[flow_store] episode INSERT: response parse error for {key}: {parse_exc}"
                )
            return True

        log.warning(
            f"[flow_store] _insert_rows_with_episode_id failed: "
            f"{resp.status_code} -- {resp.text[:200]}"
        )
        return False

    except Exception as e:
        log.error(f"[flow_store] _insert_rows_with_episode_id exception: {e}")
        return False


async def persist_flow_episode(signal_data) -> None:
    """
    ING-009: Upsert a flow_episodes row for the given contract.

    FIX-PERSIST-TYPE (fix/ingestion-persist-correctness):
      Previously accepted only `signal_data: dict` but was called from
      _process_trade() with a RepetitionEpisode dataclass after the
      (non-existent) accumulator.to_episode() call was removed.
      Fix: normalise dict vs RepetitionEpisode via a local _g() accessor.
      RepetitionEpisode fields are accessed via getattr; dict fields via .get().
      dominant_direction is a property on RepetitionEpisode — it is computed
      inline and stored in `direction` before any DB I/O.

    FIX-OCC-SYMBOL (fix/ingestion-persist-correctness):
      The vol/OI lookup used an ad-hoc OCC symbol construction:
        f"{ticker}{expiry}{contract_type[0].upper()}{int(strike*1000):08d}"
      This produced invalid symbols (e.g. "AAPL2026-01-17C00200000") because:
        1. expiry is in YYYY-MM-DD format, not YYMMDD
        2. The ticker is not padded to 6 chars
      chain_store.get_contract_vol_oi expects the raw OCC format from the stream
      (e.g. "AAPL  260117C00200000").  We use the occ_symbol field if available
      or construct the proper format otherwise.

    FIX-VOL-OI-SIGNAL (fix/ingestion-persist-correctness):
      flow_episodes.vol_oi_signal (BOOLEAN NOT NULL DEFAULT FALSE) was never
      written — INSERT and PATCH payloads both omitted it. Now computed from
      compute_vol_oi_signal(contract_volume_at_close, contract_oi_at_open) and
      written on every INSERT and PATCH.

    FIX-STEAMROOM-SCORE (fix/ingestion-persist-correctness):
      flow_episodes.episode_steamroom_score (INTEGER NOT NULL DEFAULT 0) was
      never computed or written. Now computed by _compute_episode_steamroom_score()
      on every INSERT and recomputed on every PATCH.
    """
    # Normalise RepetitionEpisode vs dict input.
    if isinstance(signal_data, dict):
        _g = signal_data.get
        # dict path: dominant_direction comes from the "direction" key
        direction = _g("direction", "UNKNOWN")
    else:
        # RepetitionEpisode dataclass path
        def _g(key, default=None):
            return getattr(signal_data, key, default)
        # dominant_direction is a computed property — read it once here
        try:
            direction = signal_data.dominant_direction
        except Exception:
            direction = "UNKNOWN"

    ticker        = _g("ticker", "UNKNOWN")
    contract_type = _g("contract_type", "CALL")
    strike        = _g("strike")
    # Coerce empty-string expiry to None (FS-TEST-FIX)
    expiry        = _g("expiry") or None
    premium       = _g("total_premium") or _g("premium") or 0.0
    signal_ts     = _g("signal_ts") or _g("last_seen") or _g("timestamp") or None
    is_multi_day_repeat = _g("is_multi_day_repeat")

    # REARCH-004: seed event quality tags
    # For RepetitionEpisode: derive is_ask_side and dte from constituent events.
    # For dict: read directly with fallback defaults.
    if isinstance(signal_data, dict):
        is_ask_side   = bool(_g("is_ask_side", False))
        dte           = _g("dte")
        event_premium = _g("premium") or premium
    else:
        # Compute ask-side majority from constituent events (bid_ask_class field)
        events        = getattr(signal_data, "events", []) or []
        ask_side_events = sum(
            1 for e in events
            if str(getattr(e, "bid_ask_class", "")).upper() in (
                "AT_ASK", "ABOVE_ASK", "ASK"
            )
        )
        is_ask_side   = (ask_side_events / len(events) >= 0.5) if events else False
        # DTE from the most recent constituent event
        dte           = getattr(events[-1], "dte", None) if events else None
        event_premium = premium  # total episode premium as seed value

    if strike is None:
        log.debug(
            f"[flow_store] persist_flow_episode: skipping {ticker} — strike={strike}"
        )
        return

    # FIX-OCC-SYMBOL: build a valid OCC symbol for the chain_store lookup.
    # For RepetitionEpisode, try to get it from the most recent event first
    # (avoids re-constructing when it was already parsed from the stream).
    # Fallback: construct from components in the standard OCC format.
    contract_occ_symbol: Optional[str] = None
    if not isinstance(signal_data, dict):
        events = getattr(signal_data, "events", []) or []
        if events:
            contract_occ_symbol = getattr(events[-1], "occ_symbol", None) or None
    if not contract_occ_symbol:
        contract_occ_symbol = _g("occ_symbol") or None
    if not contract_occ_symbol and expiry and strike is not None:
        # Last-resort construction using proper OCC YYMMDD format.
        try:
            from datetime import date as _date
            exp = _date.fromisoformat(expiry)
            yy, mm, dd = f"{exp.year % 100:02d}", f"{exp.month:02d}", f"{exp.day:02d}"
            ticker_padded = ticker.ljust(6)
            cp = contract_type[0].upper()
            contract_occ_symbol = f"{ticker_padded}{yy}{mm}{dd}{cp}{int(strike * 1000):08d}"
        except Exception:
            pass

    # ING-008: vol/OI snapshot at episode persist time.
    contract_oi_at_open: Optional[int] = None
    contract_volume_at_close: Optional[int] = None
    vol_snapshot: Optional[int] = None
    if contract_occ_symbol:
        try:
            vol_snapshot, contract_oi_at_open = get_contract_vol_oi(contract_occ_symbol)
            contract_volume_at_close = vol_snapshot
        except Exception:
            pass

    volume_oi_ratio: Optional[float] = _compute_vol_oi_ratio(
        contract_volume_at_close, contract_oi_at_open
    )

    # FIX-VOL-OI-SIGNAL: compute boolean signal for episode column.
    ep_vol_oi_signal: bool = bool(
        compute_vol_oi_signal(contract_volume_at_close, contract_oi_at_open)
    )

    # REARCH-004: compute seed-event quality dimensions (used on INSERT only).
    seed_dte_bucket    = _compute_dte_bucket(dte)
    seed_notional_tier = _compute_notional_tier(event_premium)

    ts = (signal_ts.isoformat() if hasattr(signal_ts, "isoformat") else signal_ts) \
        or datetime.now(timezone.utc).isoformat()

    key = _episode_key(ticker, direction, contract_type, strike or 0, expiry or "")
    lock = _get_episode_lock(key)

    async with lock:
        in_flight = _episode_in_flight.get(key)

        if in_flight:
            new_trade_count   = in_flight["trade_count"] + 1
            new_total_premium = in_flight["total_premium"] + premium
            # REARCH-004: increment ask_side_count from in-flight cache and recompute pct.
            new_ask_side_count = (in_flight.get("ask_side_count") or 0) + (1 if is_ask_side else 0)
            new_ask_side_pct   = round(new_ask_side_count / new_trade_count, 4)
            # FIX-STEAMROOM-SCORE: recompute on every PATCH.
            new_steamroom_score = _compute_episode_steamroom_score(
                ask_side_pct   = new_ask_side_pct,
                vol_oi_signal  = ep_vol_oi_signal,
                total_premium  = new_total_premium,
                dte_bucket     = seed_dte_bucket,
                trade_count    = new_trade_count,
            )
            patch_url = (
                f"{_SUPABASE_URL}/rest/v1/flow_episodes"
                f"?id=eq.{in_flight['id']}"
            )
            patch_payload: dict = {
                "trade_count":              new_trade_count,
                "total_premium":            new_total_premium,
                "signal_ts":                ts,
                "contract_volume_at_close": contract_volume_at_close,
                "volume_oi_ratio":          volume_oi_ratio,
                "ask_side_count":           new_ask_side_count,
                "ask_side_pct":             new_ask_side_pct,
                # FIX-VOL-OI-SIGNAL: write on every PATCH.
                "vol_oi_signal":            ep_vol_oi_signal,
                # FIX-STEAMROOM-SCORE: write on every PATCH.
                "episode_steamroom_score":  new_steamroom_score,
                # REARCH-004 SA-3: dte_bucket and notional_tier intentionally
                # excluded — they are locked at episode open (seed-event-only).
            }
            if is_multi_day_repeat is not None:
                patch_payload["is_multi_day_repeat"] = is_multi_day_repeat

            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.patch(
                        patch_url, headers=_headers(), json=patch_payload
                    )
                if resp.status_code in (200, 204):
                    _set_episode_in_flight(
                        key,
                        in_flight["id"],
                        new_trade_count,
                        new_total_premium,
                        new_ask_side_count,
                    )
                    _episode_stats["merged_episodes"] += 1
                    log.debug(f"[flow_store] episode merged (in-flight): {key}")
                else:
                    log.warning(
                        f"[flow_store] episode in-flight PATCH failed: "
                        f"{resp.status_code} -- {resp.text[:200]}"
                    )
            except Exception as e:
                log.error(f"[flow_store] persist_flow_episode in-flight PATCH exception: {e}")
            return

        existing = await _lookup_open_episode(
            ticker, direction, contract_type, strike, expiry or ""
        )

        if existing:
            new_trade_count   = existing["trade_count"] + 1
            new_total_premium = existing["total_premium"] + premium
            # REARCH-004: COALESCE(NULL, 0) handles pre-REARCH rows (PBE-1 NULL contract).
            new_ask_side_count = (existing.get("ask_side_count") or 0) + (1 if is_ask_side else 0)
            new_ask_side_pct   = round(new_ask_side_count / new_trade_count, 4)
            new_steamroom_score = _compute_episode_steamroom_score(
                ask_side_pct   = new_ask_side_pct,
                vol_oi_signal  = ep_vol_oi_signal,
                total_premium  = new_total_premium,
                dte_bucket     = seed_dte_bucket,
                trade_count    = new_trade_count,
            )
            patch_url = (
                f"{_SUPABASE_URL}/rest/v1/flow_episodes"
                f"?id=eq.{existing['id']}"
            )
            patch_payload = {
                "trade_count":              new_trade_count,
                "total_premium":            new_total_premium,
                "signal_ts":                ts,
                "contract_volume_at_close": contract_volume_at_close,
                "volume_oi_ratio":          volume_oi_ratio,
                "ask_side_count":           new_ask_side_count,
                "ask_side_pct":             new_ask_side_pct,
                "vol_oi_signal":            ep_vol_oi_signal,
                "episode_steamroom_score":  new_steamroom_score,
                # REARCH-004 SA-3: dte_bucket and notional_tier intentionally excluded.
            }
            if is_multi_day_repeat is not None:
                patch_payload["is_multi_day_repeat"] = is_multi_day_repeat

            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.patch(
                        patch_url, headers=_headers(), json=patch_payload
                    )
                if resp.status_code in (200, 204):
                    _set_episode_in_flight(
                        key,
                        existing["id"],
                        new_trade_count,
                        new_total_premium,
                        new_ask_side_count,
                    )
                    _episode_stats["merged_episodes"] += 1
                    log.debug(f"[flow_store] episode merged (DB lookup): {key}")
                else:
                    log.warning(
                        f"[flow_store] episode DB PATCH failed: "
                        f"{resp.status_code} -- {resp.text[:200]}"
                    )
            except Exception as e:
                log.error(f"[flow_store] persist_flow_episode DB PATCH exception: {e}")
        else:
            seed_ask_side_count = 1 if is_ask_side else 0
            seed_ask_side_pct   = round(seed_ask_side_count / 1, 4)  # trade_count=1 at seed
            seed_steamroom_score = _compute_episode_steamroom_score(
                ask_side_pct   = seed_ask_side_pct,
                vol_oi_signal  = ep_vol_oi_signal,
                total_premium  = premium,
                dte_bucket     = seed_dte_bucket,
                trade_count    = 1,
            )
            insert_payload = {
                "ticker":                   ticker,
                "direction":                direction,
                "contract_type":            contract_type,
                "strike":                   strike,
                "expiry":                   expiry,
                "trade_count":              1,
                "total_premium":            premium,
                "signal_ts":                ts,
                "contract_oi_at_open":      contract_oi_at_open,
                "contract_volume_at_close": contract_volume_at_close,
                "volume_oi_ratio":          volume_oi_ratio,
                # FIX-VOL-OI-SIGNAL: write on INSERT.
                "vol_oi_signal":            ep_vol_oi_signal,
                # REARCH-004: seed-event quality aggregate columns.
                "ask_side_count":           seed_ask_side_count,
                "ask_side_pct":             seed_ask_side_pct,
                "dte_bucket":               seed_dte_bucket,
                "notional_tier":            seed_notional_tier,
                # FIX-STEAMROOM-SCORE: write on INSERT.
                "episode_steamroom_score":  seed_steamroom_score,
            }
            if is_multi_day_repeat is not None:
                insert_payload["is_multi_day_repeat"] = is_multi_day_repeat

            ok = await _insert_rows_with_episode_id(
                "flow_episodes",
                insert_payload,
                key,
                premium,
                contract_oi_at_open,
                ask_side_count=seed_ask_side_count,
            )
            if ok:
                _episode_stats["created_episodes"] += 1
                log.debug(f"[flow_store] episode created: {key}")
            else:
                log.warning(f"[flow_store] episode INSERT failed for {key}")


async def _update_episode_multiday(
    ticker: str,
    contract_type: str,
    strike: float,
    expiry: str,
    is_multi_day_repeat: bool,
) -> None:
    """
    ING-007 / PBE-F2: Two-step GET+PATCH to update is_multi_day_repeat on the
    most recent flow_episodes row without touching older rows.
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
        f"&order=signal_ts.desc&limit=1&select=id"
    )

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            get_resp = await client.get(get_url, headers=get_headers)

        if get_resp.status_code != 200:
            log.warning(
                f"[flow_store] _update_episode_multiday GET failed: "
                f"{get_resp.status_code} -- {get_resp.text[:200]}"
            )
            return

        rows = get_resp.json()
        if not rows:
            log.info(
                f"[flow_store] _update_episode_multiday: no episode row found for "
                f"{ticker} {contract_type} {strike} {expiry}"
            )
            return

        row = rows[0]
        if "id" not in row:
            log.warning(
                f"[flow_store] _update_episode_multiday: row missing id for "
                f"{ticker} {contract_type} {strike} {expiry}"
            )
            return

        row_id = row["id"]
        patch_url = (
            f"{_SUPABASE_URL}/rest/v1/flow_episodes"
            f"?id=eq.{row_id}"
        )

        async with httpx.AsyncClient(timeout=5.0) as client:
            patch_resp = await client.patch(
                patch_url,
                headers=_headers(),
                json={"is_multi_day_repeat": is_multi_day_repeat},
            )

        if patch_resp.status_code not in (200, 204):
            log.warning(
                f"[flow_store] _update_episode_multiday PATCH failed: "
                f"{patch_resp.status_code} -- {patch_resp.text[:200]}"
            )

    except Exception as e:
        log.error(f"[flow_store] _update_episode_multiday exception: {e}")


async def start_lookback_worker(accumulator=None) -> None:
    """
    ING-007: Drain the lookback queue and enrich flow_episodes with
    is_multi_day_repeat from contract_day_cache.

    ING-007-SIG: Accepts an optional accumulator argument.
    - When provided (test path), uses it directly — avoids importing
      get_accumulator() so tests can inject a mock without patching.
    - When None (production/main.py zero-arg call), falls back to
      get_accumulator() from ingestion.processor.

    Both call sites work correctly:
      main.py:   asyncio.create_task(start_lookback_worker())
      tests:     asyncio.create_task(fs.start_lookback_worker(acc))
    """
    from utils.contract_day_cache import get_lookback

    if accumulator is None:
        try:
            from ingestion.processor import get_accumulator
            accumulator = get_accumulator()
        except Exception:
            accumulator = None

    # SA-F1: correct attr name + traverse list-of-tuples
    dte_tiers = getattr(accumulator, "_dte_tiers", None) or []
    min_days  = getattr(accumulator, "_multi_day_min_days", 2) if accumulator is not None else 2

    if dte_tiers:
        min_premium = min(floor for _, floors in dte_tiers for floor in floors.values())
    else:
        min_premium = 10_000.0

    log.debug("[lookback] min_premium floor resolved: %.2f", min_premium)
    log.info("[lookback] worker started")

    while True:
        try:
            key = await _lookback_queue.get()

            try:
                result = await get_lookback(key, min_premium)
            except Exception as e:
                log.error("[lookback] get_lookback exception for %s: %s", key, e)
                _lookback_queue.task_done()
                continue

            ticker        = key.ticker
            contract_type = key.contract_type
            strike        = key.strike
            expiry        = key.expiry

            prior_days_active = getattr(result, "prior_days_active", 0)
            is_repeat = prior_days_active >= min_days

            await _update_episode_multiday(
                ticker=ticker,
                contract_type=contract_type,
                strike=strike,
                expiry=expiry,
                is_multi_day_repeat=is_repeat,
            )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"[lookback] worker exception: {e}")
        finally:
            try:
                _lookback_queue.task_done()
            except Exception:
                pass


async def _bus_signal_listener() -> None:
    """
    EPISODE-FIX (retained as no-op bus consumer for db_writer channel).
    flow_episodes are now written directly from _process_trade() in
    tradier_stream.py. This listener no longer writes to the DB.
    """
    try:
        queue = bus.subscribe("db_writer")
    except Exception:
        return
    while True:
        try:
            await queue.get()
        except Exception:
            await _async_sleep(1.0)


async def start_flow_writer() -> None:
    """
    Entry point called once from main.py lifespan.
    Starts the flush loop and bus listener as background tasks.

    FS-HANG: Returns early when not configured so no infinite background
    tasks are created. Previously, start_lookback_worker() was spawned
    unconditionally and blocked forever on _lookback_queue.get() in test
    environments where SUPABASE_URL is None, causing CI timeout.
    """
    if not _is_configured():
        log.warning(
            "[flow_store] start_flow_writer: SUPABASE_URL or SUPABASE_SERVICE_ROLE_KEY "
            "not set — skipping background task creation."
        )
        return
    log.info("[flow_store] starting flow writer")
    asyncio.create_task(_flush_flow_events())
    asyncio.create_task(_bus_signal_listener())
    asyncio.create_task(start_lookback_worker())
