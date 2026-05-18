"""
Cipher Backend — FastAPI entry point

Startup sequence:
  0. gate_config_store.load()          — load tier gate config from DB into memory  [ING-010]
  1. validate_ingestion_config()       — warn on missing ingestion config rows  [RC-3]
  2. _resolve_startup_universe_fast()  — DB-only path: fresh snapshot HIT -> done;
                                         MISS -> seed with stale snapshot and schedule
                                         background refresh  [RENDER-STARTUP-HANG]
  3. init_registry()                   — in-memory init (instant)
  4. registry.load_from_db(snapshot_id) — seed OCC chains from DB via P1 fallback
  5. yield                             — SERVER IS LIVE, health probe passes (~6 s)
  6. Parallel background tasks launched:
     a. _background_build_and_upsert   — incremental/full OCC build (P4);
                                         sets _registry_build_done event on completion
     b. _background_universe_resolve() — MISS only: load_universe() + save + tier assign
                                         + stream_task relaunch with fresh symbols
                                         [STREAM-RELOAD]
     c. registry.refresh_loop()        — scheduled 30-min rebuilds
     d. _registry_prewarm_loop()       — 9:15 AM ET daily pre-warm
     e. stream_options_flow()          — waits for is_ready(), then streams
     f. start_flow_writer()            — DB flush loop
     g. start_signal_writer()          — signal DB writer
     h. _universe_refresh_loop()       — 24h universe refresh
     i. _chain_refresh_after_build()   — SEQ-002: awaits _registry_build_done Event,
                                         then sleeps 60 s (SEQ-002-STAGGER: quota
                                         recovery after build+upsert burst), THEN
                                         starts 5-min chain cache refresh loop
     j. _self_ping_worker()            — RENDER: pings /health every 5 min to prevent spin-down
     k. gate_config_store.start_refresh_loop(300) — ING-010-RELOAD: re-polls gate_configs
                                         from DB every 5 min so externally-written gate
                                         changes (Supabase dashboard, SQL, separate deploy)
                                         propagate to the running worker without a restart.
                                         Max stale-config window: 300 s.

Key architectural fixes:
  P1 (chain_store)    — snapshot-agnostic fallback load
  P2 (lifespan)       — non-blocking startup via background build task
  P3 (tradier_client) — dedicated bulk semaphore for build()
  P4 (symbol_registry)— incremental warm-restart build
  D-001 (tradier_stream) — pass registry to stream_options_flow(), no duplicate build
  D-002 (tradier_stream) — remove extra refresh_loop() create_task from stream
  RC-3 (ingestion_config) — validate_ingestion_config() at startup warns on missing DB rows
  H1   (main)         — _post_build_upsert reuses raw_quotes from build();
                        no duplicate _fetch_batch_quotes call on warm-restart
  M-1/M-2 (symbol_registry) — _build_complete flag; is_ready() no longer fires
                        on first DB-seeded contract; stream workers spawn only
                        after build() fully completes with fresh Tradier data
  M-3  (main)         — _post_build_upsert split into two guarded phases;
                        assign_tiers() failure raises so upsert_symbol_quotes()
                        is skipped and the error is visible in logs/metrics
  STREAM-5 (main)     — graceful shutdown: stream_task cancelled and awaited
                        FIRST so Tradier HTTP connections close cleanly before
                        the process exits, freeing session quota for the next
                        container start immediately.
  ING-010 (main)      — gate_config_store.load() is step 0 in startup so all
                        tier gate values are hot in memory before any service
                        (stream, accumulator, parser) runs its first tick.
  ING-010-ACC (main)  — after registry.set_tier_map(), tradier_stream.accumulator
                        .set_tier_map() is also called so the module-level hot-path
                        accumulator receives the same tier map. Without this, Gate 2
                        in _get_episode_min_premium() resolves every ticker to tier 1
                        (strict cold-start default) for the entire trading session.
  ING-010-RELOAD (main) — gate_config_store.start_refresh_loop(300) launched as
                        background task (Step 6-k). Re-polls gate_configs every
                        5 min so external DB writes propagate without a restart.
                        Root cause of May 15 leak: T1 min_premium was set to
                        $75,000 on May 7 via Supabase dashboard but the running
                        worker held the old $25,000 default from startup -- the
                        cache was frozen for the entire session lifetime.
  ING-008 (main)      — start_chain_refresh_worker() launched as a background task
                        after yield. Refreshes options chain vol/OI for all
                        stream_eligible symbols every 5 minutes via Tradier chain API.
                        Zero live API calls on the flow hot path -- persist_flow_event
                        and persist_flow_episode read from the in-process cache only.
                        FIX: wired with correct two-callable signature:
                          get_tracked_symbols -- lambda returning live OCC symbol list
                          fetch_chain_fn      -- _fetch_tradier_chain(symbol) helper
                        invalidate_vol_oi_cache() called at market-open boundary in
                        _registry_prewarm_loop() so yesterday's volume never bleeds.
  REARCH-002 (main)   — ingestion_config router mounted: GET/PATCH /admin/ingestion-config
                        now reachable. Previously the router was created but never
                        included in app.include_router().
  REARCH-005 (main)   — signal_config router mounted: GET/PATCH /admin/signal-config
                        now reachable. Reads/writes signal_config table; enforces
                        premium pyramid, DTE window, tier multiplier, and floor
                        ordering invariants. Calls reload_signal_config() on every
                        successful PATCH for immediate in-process snapshot refresh.
  MAIN-FIX-001        — start_lookback_worker() was refactored (FS-HANG) to fetch
                        its own accumulator internally via get_accumulator() -- it
                        takes 0 positional args. Removed stale registry.accumulator
                        argument from the create_task() call site.
  RENDER-KEEPALIVE    — _self_ping_worker() pings GET /health every 5 minutes.
                        Reads RENDER_EXTERNAL_URL env var (injected automatically by
                        Render). No-ops locally when the var is absent. Prevents
                        free-tier spin-down (15-min idle threshold).
                        Hardened (HOTFIX-KEEPALIVE-001):
                          - 5-min interval (down from 10) -- more headroom vs threshold
                          - Fresh httpx.AsyncClient each cycle -- avoids stale pool
                          - fail_streak counter -- log.warning per failure,
                            log.error after 3 consecutive failures
                        Does NOT prevent restarts caused by deploys, crashes, or OOM
                        -- tradier_stream reconnect logic handles those cases.
  RENDER-STARTUP-HANG — _resolve_startup_universe() split into a fast DB-only phase
                        (before yield) and a slow background phase (after yield).
                        The slow load_universe() call (CBOE + Tradier, 60-120 s) no
                        longer blocks yield, so Render's health probe passes in ~6 s
                        from cold start instead of timing out and restarting the
                        container in a loop.
  HOTFIX-IMPORT-001   — gate_config_store import corrected: the module exports `store`,
                        not `gate_config_store`. Fixed as `store as gate_config_store`.
                        This was crashing uvicorn on every boot with ImportError before
                        the lifespan even started.
  HOTFIX-CHAIN-HOURS  — _fetch_tradier_chain() now returns [] immediately outside
                        market hours (Mon-Fri 9:15 AM - 4:30 PM ET; never on weekends).
                        Tradier's chain endpoint returns HTTP 400 when markets are
                        closed, flooding logs with warnings all night. The guard is
                        co-located in the fetch helper so the chain_refresh_task loop
                        itself is untouched -- it simply sleeps its normal interval
                        between empty-return calls.
  HOTFIX-KEEPALIVE-001 — _self_ping_worker() hardened:
                        - interval dropped to 5 min (was 10) -- more margin vs 15-min
                          Render spin-down threshold
                        - httpx.AsyncClient recreated each cycle -- avoids stale
                          connection pool that causes silent failures on Render
                        - fail_streak counter replaces silent except-swallow;
                          log.warning on every failure, log.error after streak >= 3
                          with actionable guidance to check RENDER_EXTERNAL_URL
  SEQ-001 (main)      — _chain_refresh_after_build() wrapper introduced.
                        chain_refresh_worker was previously launched in parallel with
                        _background_build_and_upsert(), causing it to fire Tradier
                        chain API requests for all 3900 tickers while the OCC symbol
                        map was still being populated. This produced HTTP 400 storms
                        and incomplete chain data.
                        Fix: poll registry.is_ready() every 5 s (max 30 min) before
                        delegating to start_chain_refresh_worker(). The two fetch
                        paths are now strictly serialized:
                          symbol_registry.build() completes -> chain_refresh starts.
                        All other tasks (stream, db_write, signal_write, etc.) are
                        unaffected and continue to launch in parallel as before.
  SEQ-002 (main)      — Replace SEQ-001 polling with asyncio.Event (_registry_build_done).
                        Root cause of SEQ-001 failure: registry.is_ready() fires as soon
                        as _build_complete is set inside build(), which happens before
                        _post_build_upsert() completes. On warm restarts the registry is
                        pre-seeded from DB (7275 contracts) and _build_complete can be set
                        for a partial incremental build -- earlier than intended.
                        Fix: _registry_build_done = asyncio.Event() created in lifespan().
                          _background_build_and_upsert() sets it in a finally block after
                          both build() and _post_build_upsert() have finished (or failed).
                          _chain_refresh_after_build() awaits the event with a 30-min
                          timeout safety valve, then delegates to start_chain_refresh_worker().
                        The event fires exactly once, guaranteed, even on build failure or
                        CancelledError -- chain_refresh is never permanently blocked.
                        Zero polling, zero race conditions, zero timing ambiguity.
  SEQ-002-FIX (main)  — Correct the event-set placement in _background_build_and_upsert.
                        The previous implementation set build_done_event inside the
                        finally block of the try/except around registry.build() only --
                        meaning the event fired BEFORE _post_build_upsert() ran. This
                        re-introduced Tradier quota contention: chain_refresh could
                        unblock and start fetching chains while _post_build_upsert()
                        was still making its own Tradier calls (assign_tiers,
                        upsert_symbol_quotes).
                        Fix: single outer try/finally wraps BOTH build() and
                        _post_build_upsert(). The event is set only in the outer
                        finally -- guaranteed to fire whether either phase succeeds,
                        fails, or the task is cancelled. An inner try/except still
                        catches build() failures and returns early (skipping upsert)
                        while preserving the outer finally guarantee.
  SEQ-002-STAGGER (main) — Add 60 s quota-recovery delay between build_done_event
                        firing and start_chain_refresh_worker() being called.
                        Root cause: build+upsert exhausts the Tradier 120 req/min
                        window. Firing chain refresh immediately causes a burst of
                        concurrent chain API calls at the busiest point of startup.
                        Additionally, registry.refresh_loop() runs a 30-min rebuild
                        in the background -- it could be mid-build when chain refresh
                        fires its first pull.
                        Fix: await asyncio.sleep(60) after build_done_event.wait()
                        and before start_chain_refresh_worker(). The 60 s window:
                          - gives the Tradier rate-limit window a full reset
                          - stream is already live; _vol_oi_cache from the previous
                            session's final refresh (or DB-seeded chain) is valid
                          - first chain pull still happens well before any real flow
                            event of the trading day matters
  PREWARM-RACE (main) — _registry_prewarm_loop() and _universe_refresh_loop() both
                        called registry.build() directly with no guard on the initial
                        build completing first.
                        _registry_prewarm_loop(): if the process restarts between
                        ~9:00-9:15 AM ET, sleep_secs is near-zero and prewarm fires
                        immediately. It acquires _build_lock behind the still-running
                        _background_build_and_upsert, then starts a second full build
                        when the lock releases -- now chain_refresh AND a second build
                        are both hammering Tradier simultaneously.
                        Fix: skip prewarm's registry.build() if registry.epoch == 0
                        (initial build not yet complete). Prewarm is a daily warm-up
                        for the *next* trading day's open, not a substitute for the
                        startup build.
                        _universe_refresh_loop(): called registry.build() directly
                        inside its 24h refresh cycle with no guard. If the 24h timer
                        fired near a restart, two build() calls queued on _build_lock
                        producing identical contention. registry.refresh_loop() already
                        handles periodic OCC rebuilds every 30 min; _universe_refresh_loop
                        should not duplicate that path. Removed the direct build() call;
                        OI data is read from the live registry (get_oi_map()) which
                        is kept fresh by refresh_loop().
  STREAM-RELOAD (main) — _background_universe_resolve() now relaunches stream_task
                        with the fresh symbol list when the background universe refresh
                        completes on a cache-miss boot.
                        Root cause: _background_universe_resolve() computed a fresh
                        stream_symbols list from load_universe() but never applied it --
                        stream_task kept running against the stale seed symbol list
                        (often empty or very small) for the entire trading session.
                        Fix: _background_universe_resolve() accepts two mutable
                        single-element wrappers:
                          stream_task_ref[0]          -- the active asyncio.Task
                          stream_symbols_container    -- the list[str] used by
                                                        _get_tracked_tickers() closure
                        After tier_map is patched, if stream_symbols differ from the
                        seed, stream_task is cancelled + awaited (5 s grace), the
                        container is updated in-place, and a new stream_task is created
                        and written back into stream_task_ref[0].
                        lifespan() passes [stream_task] and stream_symbols as the
                        wrappers. Shutdown reads stream_task_ref[0] so it always
                        cancels the active task regardless of replacement.
  FIX-LOAD-FROM-DB-ARG (main) — Pass snapshot_id to registry.load_from_db() at Step 4.
                        root cause: _resolve_startup_universe_fast() loads the fresh
                        snapshot from universe_store but returned snapshot_id="" (empty
                        string) instead of the real UUID. Step 4 called
                        registry.load_from_db() with no args ->
                          TypeError: SymbolRegistry.load_from_db() missing 1 required
                          positional argument: 'snapshot_id'
                        Logged as ERROR and fell through silently. The registry started
                        with 0 OCC contracts from DB, so every cold start triggered a
                        full Tradier chain-pull (all 4122 tickers, ~5-8 min) with no
                        incremental warm-start benefit -- explaining the flood of
                        per-ticker stall warnings in the logs.
                        Fix: _resolve_startup_universe_fast() now surfaces the actual
                        snapshot UUID from universe_store.load_fresh_snapshot() on HIT,
                        and returns None on MISS. Step 4 passes this value to
                        load_from_db(snapshot_id). None triggers the P1
                        snapshot-agnostic fallback (loads the latest snapshot row
                        regardless of UUID), so MISS boots still seed from DB correctly.
                        After this fix the warm-start path works as designed:
                        load_from_db() populates _registry with the persisted snapshot,
                        build() sees a non-empty registry and runs incrementally
                        (expired-DTE tickers only), dropping cold-start chain-pull
                        time from ~5-8 min to ~50-400 tickers.
  FIX-P1-SKIP-BUILD (main) — Skip full H3 build when P1 fallback seeded sufficient
                        contracts. Root cause: load_from_db() seeds _registry with
                        127K+ contracts from the prior snapshot but does NOT set
                        _build_complete=True. _background_build_and_upsert() always
                        ran a full Tradier chain-pull (~7-14 min), blocking stream
                        workers for the entire window. Fix: if registry already has
                        > _P1_MIN_CONTRACTS (10,000) and epoch==0 (never built from
                        Tradier this session), set _build_complete=True and
                        epoch=1 directly, fire build_done_event immediately, and skip
                        registry.build(). refresh_loop() handles the background Tradier
                        refresh without blocking the stream.
  FIX-P1-SKIP-TIERS (main) — Fix stale T3 tier_map infinite-loop regression caused by
                        FIX-P1-SKIP-BUILD. Root cause: the early-return guard in
                        _background_build_and_upsert() returned before calling
                        _post_build_upsert(), so assign_tiers() was never invoked.
                        The tier_map loaded at startup (all T3 from a stale DB snapshot)
                        was never refreshed. Every subsequent boot read the same stale
                        T3=all data, creating an infinite stale-tier loop.
                        Confirmed by logs:
                          [build] FIX-P1-SKIP-BUILD: registry seeded with 55393 contracts
                          (epoch=0) - skipping full Tradier build, streaming immediately.
                        and startup tier_map showing T1=0 T2=0 T3=4357 on every boot.
                        Fix: after the skip guard fires, call _fetch_batch_quotes() on
                        stream_symbols (shallow volume/OI fetch, not a chain-pull --
                        completes in seconds) and pass the result to _post_build_upsert().
                        If the fetch returns nothing, log a warning and continue -- tiers
                        remain as-loaded from DB rather than crashing the stream.
                        The full Tradier chain-pull (registry.build()) is still skipped;
                        only tier assignment is added to the skip path.
  MAIN-DEBUG-001 (main) — _background_build_and_upsert() and lifespan() now log the
                        exact tradier_stream bug surface:
                          - persist_flow_event call-site: logs whether ev is passed as
                            OptionsFlowEvent object or dict (TypeError source).
                          - get_signal() await: confirms coroutine is awaited.
                          - _WORKER_SPAWN_DELAY_S: confirmed location is tradier_stream.py,
                            not main.py. Logged at startup for observability.
  BUILD-QUOTE-TYPE (main) — _post_build_upsert() now filters raw_quotes to only
                        objects that have the `average_volume` attribute before
                        passing to assign_tiers() and upsert_symbol_quotes().
                        Root cause: registry.build() returns a list that can contain
                        plain int objects (OI counts / internal accumulation artefacts
                        leaking from the build pipeline) mixed with SymbolQuote
                        instances. _classify() accessed quote.average_volume on an int
                        and raised AttributeError, causing _post_build_upsert to fail
                        entirely on every boot -- tier_map was never set from a fresh
                        Tradier build and upsert_symbol_quotes() was never called.
                        Fix: filter at the top of _post_build_upsert; log the count
                        of dropped non-SymbolQuote items so the underlying build()
                        contamination remains visible in logs.
  SAVE-SNAPSHOT-SET (main) — Fix save_snapshot() call sites passing stream_eligible_set
                        as the third positional arg (provider parameter).
                        Root cause: both call sites used the positional signature:
                          save_snapshot(symbols, source, stream_eligible_set)
                        but universe_store.save_snapshot() is defined as:
                          save_snapshot(symbols, source="tradier", provider="tradier", symbol_rows=None)
                        stream_eligible_set (a Python set) landed in `provider` and
                        was embedded in the INSERT payload, causing:
                          TypeError: Object of type set is not JSON serializable
                        in _sync_save_snapshot() on every boot.
                        Fix: both call sites now build an explicit symbol_rows list
                        that encodes stream_eligible per-symbol correctly, and pass it
                        as the keyword argument. provider defaults to "tradier".
                        Fixed in:
                          1. _background_universe_resolve()
                          2. _universe_refresh_loop()
  FIX-UPSERT-ARG-ORDER (main) — Correct upsert_symbol_quotes() argument order at both
                        call sites. Root cause: both _post_build_upsert() and
                        _background_universe_resolve() called:
                          upsert_symbol_quotes(quote_rows, tier_map)
                        but the signature is:
                          upsert_symbol_quotes(snapshot_id: str, quote_rows) -> None
                        quote_rows landed in snapshot_id; tier_map (a dict) landed in
                        quote_rows; _sync_upsert_symbol_quotes normalised dict ->
                        list(values()) producing list[int] (tier values); _get_symbol()
                        returned "" for every int; upsert_rows was always empty ->
                        (0 rows) logged on every boot. Additionally, no real snapshot
                        UUID was ever passed.
                        Fix:
                          1. Add _enrich_quotes_with_tier(quotes, tier_map) helper
                             that stamps q.tier on each SymbolQuote in-place so
                             _rget(r, "tier") returns a real value in the upsert.
                          2. _post_build_upsert(): after assign_tiers(), call
                             universe_store.get_latest_snapshot_id() to get the real
                             UUID, enrich quotes, then call
                             upsert_symbol_quotes(snapshot_id, enriched_quotes).
                          3. _background_universe_resolve(): after save_snapshot()
                             succeeds, call get_latest_snapshot_id() for the
                             freshly-written UUID, enrich quotes, then call
                             upsert_symbol_quotes(snapshot_id, enriched_quotes).
  STARTUP-TIMEOUT (main) — Wrap Step 2a (_resolve_startup_universe_fast) body in
                        asyncio.wait_for() with an 8-second hard timeout.
                        Root cause: _resolve_startup_universe_fast() awaits several
                        universe_store DB calls (load_fresh_snapshot,
                        get_latest_snapshot_id, load_tier_map, load_any_snapshot)
                        synchronously before yield. On a cold Render boot where the
                        Supabase connection pool is not yet warm, any of these calls
                        can stall for 10-30+ seconds, blocking yield and causing
                        Render's health probe to time out and restart the container
                        in a restart loop -- the exact problem RENDER-STARTUP-HANG
                        was supposed to fix.
                        Fix: the entire body of _resolve_startup_universe_fast() is
                        extracted into an inner coroutine _inner() and wrapped in
                        asyncio.wait_for(_inner(), timeout=8.0). On TimeoutError or
                        any unexpected exception, the function logs a warning and
                        returns ([], {}, None, True) immediately, so yield is always
                        reached within budget. _background_universe_resolve() (Step 6b)
                        still runs after yield and populates symbols via the slow path.
                        The 8 s budget gives Supabase one full round-trip attempt
                        (p99 < 2 s normally) while staying well within Render's
                        ~30 s health-probe window.
                        STALE path also now surfaces snapshot_id via
                        get_latest_snapshot_id() so Step 4 registry.load_from_db()
                        receives a real UUID on stale-snapshot boots (previously
                        returned None on MISS, triggering the slower P1 fallback
                        scan every time).
"""
import asyncio
import json
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from routers import auth, flow, simulation, ws, smart_signals
from routers.smart_signals import stream_stats
from routers import history
from routers import admin
from routers import health
from routers import ingestion_config as ingestion_config_router  # REARCH-002
from routers import signal_config as signal_config_router        # REARCH-005
from core.auth import get_current_user
from services.flow_store import start_flow_writer, start_lookback_worker
from services.chain_store import start_chain_refresh_worker, invalidate_vol_oi_cache  # ING-008
from services.symbols_loader import load_universe, _fetch_batch_quotes
from services import universe_store
from services.signal_store import start_signal_writer
from services.tier_engine import assign_tiers
from services.symbol_registry import init_registry, get_registry
from services.ingestion_config import validate_ingestion_config
from services.tradier_stream import stream_options_flow
from services.gate_config_store import store as gate_config_store  # HOTFIX-IMPORT-001

import httpx

# FIX-P1-SKIP-BUILD: minimum contract count from load_from_db() that is
# considered sufficient to skip the full Tradier chain-pull at startup.
_P1_MIN_CONTRACTS = 10_000

# SEQ-002-STAGGER: seconds to wait after build_done_event before starting
# chain refresh worker. Gives the Tradier 120 req/min window a full reset
# after the build+upsert burst.
_CHAIN_REFRESH_STAGGER_S = 60

# STARTUP-TIMEOUT: hard budget (seconds) for the synchronous DB calls in
# _resolve_startup_universe_fast(). Must complete before yield so Render's
# health probe passes. On timeout the function falls through to an