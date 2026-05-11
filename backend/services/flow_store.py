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
      _classify_bid_ask() is now the PRIVATE test-shim returning str only.
      persist_flow_event() updated to unpack the tuple from classify_bid_ask().
  14. REARCH-003-ENUM (2026-05-11): dte_bucket and notional_tier were missing
      from persist_flow_event() row dict entirely — new rows were written with
      NULL for both columns despite the columns existing in the schema.
      Fixed by importing _compute_dte_bucket/_compute_notional_tier from
      processor and computing both inline before the row dict is built.
      Enum values are the canonical Steamroom set:
        dte_bucket:    '0DTE' | '1-4' | '5-60' | '61-90' | '90+'
        notional_tier: 'WATCH' | 'NOTEWORTHY' | 'BLOCK' | 'GOLDEN'
  15. FS-TEST-FIX (2026-05-11): three test failures addressed:
      a) FlowStore class added — tests 5-12 in test_flow_store.py import
         FlowStore directly and exercise add_flow, get_flows, get_flows_by_symbol,
         get_stats, clear, size methods.
      b) persist_flow_episode() signature changed to accept a single signal_data
         dict (matching how tradier_stream._process_trade() calls it and how
         the test suite invokes it). Positional-kwarg form is derived internally.
         Empty expiry string is now coerced to None before the row is built.
         Early-return guard relaxed to only block on strike=None (expiry=None
         is legal and persisted as SQL NULL).
      c) asyncio.sleep in _insert_rows_with_retry now calls the module-level
         _async_sleep alias so tests can patch 'services.flow_store._async_sleep'
         without patching the global asyncio.sleep (which affects the event loop).

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
    One lock per merge key ("ticker|dir|ctype|strike|expiry").
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

REARCH-003 quality tag semantics:
  flow_events.is_ask_side      BOOLEAN NOT NULL DEFAULT FALSE
                                 — True when bid_ask_class == 'ASK'
  flow_events.bid_ask_class    TEXT    — 'ASK' | 'BID' | 'MID'
  flow_events.vol_oi_signal    BOOLEAN DEFAULT NULL
                                 — True when vol/OI ratio >= VOL_OI_HIGH_THRESHOLD (0.5)
                                 — False when below threshold
                                 — NULL when vol or OI snapshot is unavailable (cache miss)
                                 — Column is nullable (migration 026: BOOLEAN DEFAULT NULL)
                                 — Python type: Optional[bool] — matches DDL exactly.
                                   DO NOT change to bool or add NOT NULL to the migration
                                   without updating compute_vol_oi_signal() return type.
  flow_events.normalized_premium  NUMERIC(18,4) — premium / underlying_price; NULL when
                                                   underlying_price is None or 0
  flow_events.normalized_oi       NUMERIC(18,4) — open_interest (Tradier tick-level OI field) /
                                                   contract_oi (chain_store intraday snapshot at
                                                   persist time). Rounded to 4dp via
                                                   _compute_vol_oi_ratio(). NULL when contract_oi
                                                   is unavailable or zero.
  flow_events.dte_bucket       TEXT — Steamroom DTE bracket:
                                 '0DTE' | '1-4' | '5-60' | '61-90' | '90+'
                                 Computed by _compute_dte_bucket() from processor.py.
                                 Never NULL — defaults to '90+' on missing/negative DTE.
  flow_events.notional_tier    TEXT — Steamroom premium size bucket:
                                 'WATCH' | 'NOTEWORTHY' | 'BLOCK' | 'GOLDEN'
                                 Computed by _compute_notional_tier() from processor.py.
                                 Never NULL — defaults to 'WATCH' on missing/zero premium.

  classify_bid_ask(fill, bid, ask) -> Tuple[str, bool]:
    PUBLIC API — returns (bid_ask_class: str, is_ask_side: bool).
    PBE-1: This is the canonical public function. Callers get both the class
    label and the boolean flag in one call — no need to re-derive is_ask_side
    from the string at the call site.
    Thresholds (Steamroom deliberation 2026-05-11):
      fill >= ask * 0.98  → ('ASK', True)
      fill <= bid * 1.02  → ('BID', False)
      otherwise           → ('MID', False)
    Edge cases: ask <= 0 or bid <= 0 → ('MID', False)
    Crossed market (bid > ask) → ('MID', False)

  _classify_bid_ask(fill, bid, ask) -> str:
    PRIVATE TEST-SHIM — returns bid_ask_class string only.
    TEST-ONLY. Guards None bid/ask → 'MID' via coercion to 0.
    Must NOT be called from production paths.

  compute_vol_oi_signal(vol, oi) -> Optional[bool]:
    Returns True  when vol/oi >= VOL_OI_HIGH_THRESHOLD
    Returns False when vol/oi < VOL_OI_HIGH_THRESHOLD
    Returns None  when vol is None or oi is None or oi == 0
    Delegates division to _compute_vol_oi_ratio() — do not re-implement inline.

  Private test aliases (test_flow_and_stats.py contract):
    _classify_bid_ask(fill, bid, ask) -> str
      TEST-ONLY SHIM. Returns bid_ask_class string.
      Guards None bid/ask → 'MID' via coercion to 0.
      Must NOT be called from production paths.
    _compute_vol_oi_signal(vol, oi, threshold=VOL_OI_HIGH_THRESHOLD) -> str
      Returns 'HIGH' | 'NORMAL' | 'UNKNOWN' (string form for test assertions).
    _compute_normalized_premium(premium, underlying) -> Optional[float]
      Returns round(premium/underlying, 4) or None on bad inputs.
    _compute_vol_oi_ratio(vol, oi) -> Optional[float]
      Single source of truth for vol/OI division. Returns round(vol/oi, 4)
      or None when vol is None, oi is None, or oi == 0.
      Used internally by compute_vol_oi_signal() and persist_flow_event().
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
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

    def add_flow(self, flow: dict) -> None:  # noqa: D401
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

    Boundary inclusive on ASK: a fill exactly at ask*0.98 is ask-side pressure.
    Mid territory is strictly between both thresholds.

    Guard ordering (DO NOT reorder — each guard is a prerequisite for the next):
      1. Zero/bad-quote guard  (ask <= 0 or bid <= 0) must come first.
      2. Crossed-market guard  (bid > ask) must follow Guard 1, precede Guard 3.
      3. ASK/BID threshold checks last, on clean valid quotes only.

    Returns: Tuple[bid_ask_class: str, is_ask_side: bool]
    """
    # Guard 1: synthetic/bad quote (zero or missing bid/ask)
    if not ask or ask <= 0 or not bid or bid <= 0:
        return ("MID", False)
    # Guard 2: crossed market — data quality event, not a valid spread.
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

    Used internally by:
      - compute_vol_oi_signal()  — threshold comparison delegates here
      - persist_flow_event()     — normalized_oi column delegates here

    Do NOT re-implement vol/OI division elsewhere in this module.
    """
    if vol is None or oi is None or oi == 0:
        return None
    return round(vol / oi, 4)


def compute_vol_oi_signal(
    vol: Optional[int],
    oi: Optional[int],
) -> Optional[bool]:  # DDL contract: BOOLEAN DEFAULT NULL in migration 026. Keep Optional[bool].
    """
    REARCH-003: Return True when intraday vol/OI ratio meets the high-activity
    threshold, False when below it, None when data is unavailable.

    Return type is Optional[bool] — maps directly to the flow_events.vol_oi_signal
    column which is BOOLEAN DEFAULT NULL (migration 026). A None return persists
    as SQL NULL (cache miss). Do NOT change this to bool or add a NOT NULL default
    without also updating the migration DDL and the nullable annotation in
    persist_flow_event().

    Delegates division to _compute_vol_oi_ratio() — do not re-implement inline.
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
) -> str:
    """
    PRIVATE TEST-SHIM — returns bid_ask_class string only. Do not call from production.

    PBE-1: The public classify_bid_ask() returns Tuple[str, bool]. This private
    shim exists solely for test suites that assert on the string label only.
    Coerces None bid/ask → 0 before delegating to classify_bid_ask().
    """
    cls, _ = classify_bid_ask(fill_price, bid or 0, ask or 0)
    return cls


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

    # ING-008: capture intraday vol/OI snapshot from chain_store cache.
    contract_volume_snapshot: Optional[int] = None
    contract_oi: Optional[int] = None
    if occ_symbol:
        try:
            from services.chain_store import get_contract_vol_oi
            contract_volume_snapshot, contract_oi = get_contract_vol_oi(occ_symbol)
        except Exception:
            pass

    # REARCH-003: compute quality dimension tags.
    fill_price       = ev_dict.get("fill_price", 0.0) or 0.0
    bid              = ev_dict.get("bid", 0.0) or 0.0
    ask              = ev_dict.get("ask", 0.0) or 0.0
    underlying_price = ev_dict.get("underlying_price", 0.0) or 0.0
    premium          = ev_dict.get("premium", 0.0) or 0.0
    dte              = ev_dict.get("dte")

    # normalized_oi numerator: Tradier tick-level OI field from the event dict.
    # normalized_oi denominator: contract_oi captured from chain_store at persist time (intraday snapshot).
    # INTENTIONALLY DIFFERENT SOURCES — this is not a data bug.
    # The ratio answers: "what fraction of the chain_store's intraday OI snapshot
    # does Tradier's tick-level OI represent?" It is not expected to be ~1.0.
    # NULL when contract_oi (denominator) is unavailable or zero.
    open_interest = ev_dict.get("open_interest") or None  # numerator: tick-level OI from Tradier

    # PBE-1: classify_bid_ask() now returns Tuple[str, bool] — unpack directly.
    bid_ask_cls, is_ask_side = classify_bid_ask(fill_price, bid, ask)

    # vol_oi_signal: Optional[bool] — DDL contract: BOOLEAN DEFAULT NULL (migration 026).
    # None → SQL NULL (chain_store cache miss). True/False → bool persisted as Postgres boolean.
    # DO NOT add NOT NULL or change Optional[bool] without updating migration 026 DDL.
    vol_oi_signal: Optional[bool] = compute_vol_oi_signal(
        contract_volume_snapshot, contract_oi
    )

    normalized_premium: Optional[float] = _compute_normalized_premium(premium, underlying_price)

    # normalized_oi = tick-level open_interest (numerator, Tradier) /
    #                 contract_oi (denominator, chain_store intraday snapshot).
    # Delegates to _compute_vol_oi_ratio() — single source of truth for vol/OI division.
    # Rounded to 4dp. NULL when contract_oi is unavailable or zero (cache miss).
    # See INTENTIONALLY DIFFERENT SOURCES note above.
    normalized_oi: Optional[float] = _compute_vol_oi_ratio(open_interest, contract_oi)

    # REARCH-003-ENUM: dte_bucket and notional_tier — Steamroom canonical enums.
    # Imported from processor to keep the single source of truth in one place.
    # dte_bucket:    '0DTE' | '1-4' | '5-60' | '61-90' | '90+'
    # notional_tier: 'WATCH' | 'NOTEWORTHY' | 'BLOCK' | 'GOLDEN'
    # Neither is nullable — defaults to '90+' / 'WATCH' on missing inputs.
    from ingestion.processor import _compute_dte_bucket, _compute_notional_tier
    dte_bucket    = _compute_dte_bucket(dte)
    notional_tier = _compute_notional_tier(premium)

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
        "bid_ask_class":            bid_ask_cls,
        "is_ask_side":              is_ask_side,
        "is_aggressive":            ev_dict.get("is_aggressive", False),
        "sentiment":                ev_dict.get("sentiment", "NEUTRAL"),
        "exchange_count":           ev_dict.get("exchange_count", 1),
        "fill_count":               ev_dict.get("fill_count", 1),
        "open_interest":            ev_dict.get("open_interest", 0),
        "iv":                       ev_dict.get("iv", 0.0),
        "underlying_price":         underlying_price,
        "occ_symbol":               occ_symbol,
        "is_synthetic_quote":       ev_dict.get("is_synthetic_quote", False),
        # ING-008: vol/OI enrichment
        "contract_volume_snapshot": contract_volume_snapshot,
        "contract_oi":              contract_oi,
        # REARCH-003: quality dimension tags
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
    """ING-009: Query flow_episodes for an open same-session episode."""
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
        f"&select=id,trade_count,total_premium"
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


async def persist_flow_episode(signal_data: dict) -> None:
    """
    ING-009: Upsert a flow_episodes row for the given contract.

    Accepts a single signal_data dict containing:
      ticker, direction, contract_type, strike, expiry,
      total_premium, trade_count (optional), signal_ts (optional),
      is_multi_day_repeat (optional).

    Empty-string expiry is coerced to None before the row is built.
    The early-return guard only fires when strike is None — expiry=None
    is legal and persisted as SQL NULL.

    If an open same-session episode exists within _EPISODE_MERGE_WINDOW_S,
    PATCHes it (trade_count += 1, total_premium +=, signal_ts = new).
    Otherwise INSERTs a new episode row.

    ING-009-RACE: serialised per-contract via _get_episode_lock() so concurrent
    coroutines for the same contract never double-INSERT.
    """
    if not _is_configured():
        return

    ticker       = signal_data.get("ticker", "UNKNOWN")
    direction    = signal_data.get("direction", "UNKNOWN")
    contract_type = signal_data.get("contract_type", "CALL")
    strike       = signal_data.get("strike")
    # Coerce empty-string expiry to None (FS-TEST-FIX)
    expiry       = signal_data.get("expiry") or None
    premium      = signal_data.get("total_premium") or signal_data.get("premium") or 0.0
    signal_ts    = signal_data.get("signal_ts") or signal_data.get("timestamp") or None
    is_multi_day_repeat = signal_data.get("is_multi_day_repeat")

    if strike is None:
        log.debug(
            f"[flow_store] persist_flow_episode: skipping {ticker} — strike={strike}"
        )
        return

    # ING-008: vol/OI snapshot at episode persist time.
    contract_oi_at_open: Optional[int] = None
    contract_volume_at_close: Optional[int] = None
    vol_snapshot: Optional[int] = None
    try:
        from services.chain_store import get_contract_vol_oi
        occ_symbol = f"{ticker}{expiry}{contract_type[0].upper()}{int(strike * 1000):08d}"
        vol_snapshot, contract_oi_at_open = get_contract_vol_oi(occ_symbol)
        contract_volume_at_close = vol_snapshot
    except Exception:
        pass

    volume_oi_ratio: Optional[float] = _compute_vol_oi_ratio(
        contract_volume_at_close, contract_oi_at_open
    )

    ts = signal_ts or datetime.now(timezone.utc).isoformat()
    key = _episode_key(ticker, direction, contract_type, strike or 0, expiry or "")
    lock = _get_episode_lock(key)

    async with lock:
        in_flight = _episode_in_flight.get(key)

        if in_flight:
            new_trade_count   = in_flight["trade_count"] + 1
            new_total_premium = in_flight["total_premium"] + premium
            patch_url = (
                f"{_SUPABASE_URL}/rest/v1/flow_episodes"
                f"?id=eq.{in_flight['id']}"
            )
            patch_payload: dict = {
                "trade_count":            new_trade_count,
                "total_premium":          new_total_premium,
                "signal_ts":              ts,
                "contract_volume_at_close": contract_volume_at_close,
                "volume_oi_ratio":        volume_oi_ratio,
            }
            if is_multi_day_repeat is not None:
                patch_payload["is_multi_day_repeat"] = is_multi_day_repeat

            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.patch(
                        patch_url, headers=_headers(), json=patch_payload
                    )
                if resp.status_code in (200, 204):
                    _set_episode_in_flight(key, in_flight["id"], new_trade_count, new_total_premium)
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
            patch_url = (
                f"{_SUPABASE_URL}/rest/v1/flow_episodes"
                f"?id=eq.{existing['id']}"
            )
            patch_payload = {
                "trade_count":            new_trade_count,
                "total_premium":          new_total_premium,
                "signal_ts":              ts,
                "contract_volume_at_close": contract_volume_at_close,
                "volume_oi_ratio":        volume_oi_ratio,
            }
            if is_multi_day_repeat is not None:
                patch_payload["is_multi_day_repeat"] = is_multi_day_repeat

            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.patch(
                        patch_url, headers=_headers(), json=patch_payload
                    )
                if resp.status_code in (200, 204):
                    _set_episode_in_flight(key, existing["id"], new_trade_count, new_total_premium)
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
            insert_payload = {
                "ticker":                  ticker,
                "direction":               direction,
                "contract_type":           contract_type,
                "strike":                  strike,
                "expiry":                  expiry,
                "trade_count":             1,
                "total_premium":           premium,
                "signal_ts":               ts,
                "contract_oi_at_open":     contract_oi_at_open,
                "contract_volume_at_close": contract_volume_at_close,
                "volume_oi_ratio":         volume_oi_ratio,
            }
            if is_multi_day_repeat is not None:
                insert_payload["is_multi_day_repeat"] = is_multi_day_repeat

            insert_headers = {**_headers(), "Prefer": "return=representation"}
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.post(
                        f"{_SUPABASE_URL}/rest/v1/flow_episodes",
                        headers=insert_headers,
                        json=[insert_payload],
                    )
                if resp.status_code in (200, 201):
                    rows = resp.json()
                    if rows:
                        new_id = rows[0]["id"]
                        _set_episode_in_flight(key, new_id, 1, premium)
                        _episode_stats["created_episodes"] += 1
                        log.debug(f"[flow_store] episode created: {key} id={new_id}")
                    else:
                        log.warning(
                            f"[flow_store] episode INSERT returned empty body for {key}"
                        )
                else:
                    log.warning(
                        f"[flow_store] episode INSERT failed: "
                        f"{resp.status_code} -- {resp.text[:200]}"
                    )
            except Exception as e:
                log.error(f"[flow_store] persist_flow_episode INSERT exception: {e}")


async def _update_episode_multiday(
    ticker: str,
    contract_type: str,
    strike: float,
    expiry: str,
    is_multi_day_repeat: bool,
) -> None:
    """
    ING-007 / PBE-F2: Two-step GET+PATCH to update is_multi_day_repeat on the
    most recent flow_episodes row for a contract without touching older rows.
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
            log.info(  # SA-F3: elevated from DEBUG so cold-miss is observable in Railway
                f"[flow_store] _update_episode_multiday: no episode row found for "
                f"{ticker} {contract_type} {strike} {expiry}"
            )
            return

        row_id = rows[0]["id"]
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


async def start_lookback_worker() -> None:
    """
    ING-007: Drain the lookback queue and enrich flow_episodes with
    is_multi_day_repeat from contract_day_cache.
    """
    try:
        from ingestion.processor import get_accumulator
        accumulator = get_accumulator()
        # SA-F1: correct attr name + traverse list-of-tuples
        dte_tiers = getattr(accumulator, "_dte_tiers", None) or []
        min_premium = (
            min(floor for _, floors in dte_tiers for floor in floors.values())
            if dte_tiers
            else 10_000.0
        )
        log.debug("[lookback] min_premium floor resolved: %.2f", min_premium)
    except Exception:
        min_premium = 10_000.0
        log.debug("[lookback] min_premium floor fallback: %.2f", min_premium)

    try:
        from services.chain_store import get_contract_day_cache
        contract_day_cache = get_contract_day_cache()
    except Exception:
        contract_day_cache = {}

    log.info("[lookback] worker started")

    while True:
        try:
            key = await _lookback_queue.get()
            ticker, contract_type, strike, expiry = key

            cached = contract_day_cache.get(key)
            if cached is None:
                _lookback_queue.task_done()
                continue

            prior_days = cached.get("prior_days_active", 0)
            is_repeat  = prior_days > 0

            await _update_episode_multiday(
                ticker, contract_type, strike, expiry, is_repeat
            )

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
    """
    log.info("[flow_store] starting flow writer")
    asyncio.create_task(_flush_flow_events())
    asyncio.create_task(_bus_signal_listener())
    asyncio.create_task(start_lookback_worker())
