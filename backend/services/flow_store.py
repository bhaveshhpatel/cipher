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
        _lookback_stats["lookback_queued"] 