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
  FIX-QQ1-BUILD-SEQUENCING (main) — Pre-fetch SymbolQuote objects BEFORE registry.build()
                        so tier_map is correct during the chain-pull AND _post_build_upsert
                        receives real SymbolQuote objects (not raw dicts from build()).
                        Root causes fixed:
                          1. registry.build() returns (int, dict) tuple -- code was
                             treating the whole tuple as raw_quotes, so _post_build_upsert
                             received a 2-tuple instead of a list, _filter_symbol_quotes
                             saw 0 SymbolQuote objects, and tier assignment was silently
                             skipped on every full build.
                          2. raw_quotes_dict from build() contains raw dicts from
                             _fetch_stock_prices, NOT SymbolQuote objects -- assign_tiers()
                             needs SymbolQuote objects with .average_volume / .open_interest.
                        Fix:
                          - Pre-build: call _fetch_batch_quotes(stream_symbols) BEFORE
                            registry.build() to obtain real SymbolQuote objects, then
                            assign_tiers() -> registry.set_tier_map() ->
                            _sync_accumulator_tier_map() so _build_with_sem reads the
                            correct tier via self._tier_map.get(ticker, 3) during the
                            chain-pull.
                          - Tuple unpack: count, _ = await registry.build() -- discard
                            the raw dict return (internal _fetch_stock_prices dicts,
                            not SymbolQuotes).
                          - Post-build: pass the pre-fetched SymbolQuote list (pre_quotes)
                            to _post_build_upsert() -- these are guaranteed SymbolQuote
                            objects that assign_tiers() and upsert_symbol_quotes() can
                            consume correctly.
                          - Guard: if pre-fetch returns nothing (Tradier down), fall through
                            to registry.build() anyway but log a warning; tiers stay as
                            DB-loaded rather than crashing the stream.
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


class _JsonFormatter(logging.Formatter):
    SEVERITY_MAP = {
        logging.DEBUG:    "debug",
        logging.INFO:     "info",
        logging.WARNING:  "warning",
        logging.ERROR:    "error",
        logging.CRITICAL: "critical",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "severity":  self.SEVERITY_MAP.get(record.levelno, "info"),
            "logger":    record.name,
            "message":   record.getMessage(),
            "timestamp": self.formatTime(record, self.datefmt),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _configure_logging() -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


_configure_logging()
log = logging.getLogger("main")


async def get_config() -> dict:
    return {
        "app_env":   settings.APP_ENV,
        "log_level": settings.LOG_LEVEL,
    }


async def _self_ping_worker() -> None:
    """RENDER-KEEPALIVE: pings /health every 5 min. First ping at T+5."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        log.info("[keepalive] RENDER_EXTERNAL_URL not set - self-ping disabled (non-Render env)")
        return
    ping_url = f"{url}/health"
    log.info("[keepalive] Self-ping worker started - target: %s (every 5 min)", ping_url)
    fail_streak = 0
    while True:
        await asyncio.sleep(300)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(ping_url)
            fail_streak = 0
            log.debug("[keepalive] Ping OK - HTTP %d", resp.status_code)
        except Exception as exc:
            fail_streak += 1
            log.warning(
                "[keepalive] Ping FAILED (streak=%d): %s",
                fail_streak, exc,
            )
            if fail_streak >= 3:
                log.error(
                    "[keepalive] 3 consecutive ping failures - "
                    "Render may spin down. Check RENDER_EXTERNAL_URL and network.",
                )


_ET = ZoneInfo("America/New_York")


def _is_market_hours() -> bool:
    """HOTFIX-CHAIN-HOURS: True only during Mon-Fri 9:15 AM - 4:30 PM ET."""
    now = datetime.now(_ET)
    if now.weekday() >= 5:
        return False
    return time(9, 15) <= now.time() <= time(16, 30)


async def _fetch_tradier_chain(symbol: str) -> list:
    if not _is_market_hours():
        log.debug("[chain_refresh] %s - skipped (market closed)", symbol)
        return []

    if not settings.TRADIER_API_KEY:
        return []
    url = f"{settings.TRADIER_BASE_URL}/v1/markets/options/chains"
    headers = {
        "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
        "Accept": "application/json",
    }
    params = {"symbol": symbol, "greeks": "false"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            log.warning(
                "[chain_refresh] Tradier chain fetch %s -> HTTP %d",
                symbol, resp.status_code,
            )
            return []
        data = resp.json()
        options = data.get("options") or {}
        option_list = options.get("option") or []
        if isinstance(option_list, dict):
            option_list = [option_list]
        return option_list
    except Exception as exc:
        log.warning(
            "[chain_refresh] _fetch_tradier_chain(%s) error: %s",
            symbol, exc,
        )
        return []


async def _background_universe_resolve(
    registry,
    stream_task_ref: list,
    stream_symbols_container: list,
    universe_ready_event: Optional[asyncio.Event] = None,   # ← ADD
) -> None:
    try:
        await asyncio.wait_for(build_done_event.wait(), timeout=timeout)
        log.info(
            "[chain_refresh] SEQ-002: build_done_event received - "
            "waiting %d s (SEQ-002-STAGGER) before starting chain refresh worker "
            "to allow Tradier rate-limit window to recover after build+upsert burst",
            _CHAIN_REFRESH_STAGGER_S,
        )
    except asyncio.TimeoutError:
        log.warning(
            "[chain_refresh] SEQ-002: build_done_event not set after %.0f s (timeout) - "
            "starting chain refresh worker anyway",
            timeout,
        )

    # SEQ-002-STAGGER: intentional delay to let Tradier quota recover.
    # build+upsert can exhaust the 120 req/min window; 60 s gives a full
    # reset before chain refresh adds its own ~50 req/min load.
    await asyncio.sleep(_CHAIN_REFRESH_STAGGER_S)
    log.info(
        "[chain_refresh] SEQ-002-STAGGER: %d s elapsed - starting chain refresh worker",
        _CHAIN_REFRESH_STAGGER_S,
    )

    await start_chain_refresh_worker(
        get_tracked_symbols=get_tracked_symbols,
        fetch_chain_fn=fetch_chain_fn,
    )


async def _resolve_startup_universe_fast() -> tuple[list[str], dict[str, int], Optional[str], bool]:
    log.info("[universe] Step 2a: checking for fresh DB snapshot (max_age=24h)")

    fresh = await universe_store.load_fresh_snapshot(max_age_hours=24)
    if fresh:
        snapshot_id: Optional[str] = await universe_store.get_latest_snapshot_id()
        log.info(
            "[universe] Step 2a HIT: loaded fresh universe from DB (%d symbols) "
            "snapshot_id=%s - stream starting",
            len(fresh), snapshot_id,
        )
        tier_map = await universe_store.load_tier_map()
        log.info(
            "[universe] Step 2a: tier_map loaded (%d symbols mapped, T1=%d T2=%d T3=%d)",
            len(tier_map),
            sum(1 for t in tier_map.values() if t == 1),
            sum(1 for t in tier_map.values() if t == 2),
            sum(1 for t in tier_map.values() if t == 3),
        )
        stream_symbols = list(fresh)
        return stream_symbols, tier_map, snapshot_id, False

    log.info("[universe] Step 2a MISS: no fresh snapshot - loading most-recent stale snapshot as seed")
    stale = await universe_store.load_any_snapshot()
    if stale:
        log.info(
            "[universe] Step 2a STALE: seeding from %d stale symbols - "
            "background refresh will produce the authoritative list",
            len(stale),
        )
        stale_symbols = list(stale)
        return stale_symbols, {}, None, True

    log.warning(
        "[universe] Step 2a: no snapshot in DB at all - "
        "starting with empty symbol list, background refresh will populate"
    )
    return [], {}, None, True


def _build_symbol_rows(symbols: list[str], stream_eligible_set) -> list[dict]:
    """SAVE-SNAPSHOT-SET: build explicit symbol_rows for save_snapshot().

    Encodes stream_eligible per-symbol so that the correct value is persisted
    to options_universe_symbols. Avoids passing stream_eligible_set (a Python
    set) as the `provider` positional arg, which caused JSON serialisation
    failure.

    snapshot_id is intentionally omitted here; _sync_save_snapshot() injects
    the freshly-generated UUID into each row before upserting.
    """
    eligible: set = set(stream_eligible_set) if stream_eligible_set is not None else set(symbols)
    return [
        {
            "symbol":          s,
            "stream_eligible": s in eligible,
        }
        for s in symbols
    ]


def _enrich_quotes_with_tier(quotes: list, tier_map: dict[str, int]) -> list:
    """FIX-UPSERT-ARG-ORDER: stamp .tier on each SymbolQuote from tier_map.

    universe_store.upsert_symbol_quotes() uses _rget(r, "tier") to read the
    tier column value. Without this stamp, _rget() returns None for every
    quote and the tier column is never written to options_universe_symbols.

    Returns the same list (mutated in-place) for convenience.
    """
    for q in quotes:
        sym = getattr(q, "symbol", None) or (q.get("symbol") if isinstance(q, dict) else None)
        if sym and sym in tier_map:
            if isinstance(q, dict):
                q["tier"] = tier_map[sym]
            else:
                try:
                    q.tier = tier_map[sym]
                except AttributeError:
                    pass  # frozen dataclass or NamedTuple — tier stays None
    return quotes


async def _background_universe_resolve(
    registry,
    stream_task_ref: list,
    stream_symbols_container: list,
) -> None:
    log.info("[universe] Background universe refresh starting (cache miss at startup)")
    try:
        stale = await universe_store.load_any_snapshot()
        log.info(
            "[universe] Step 2b: checking env - TRADIER_API_KEY set=%s SUPABASE_URL set=%s",
            bool(settings.TRADIER_API_KEY), bool(settings.SUPABASE_URL),
        )
        log.info("[universe] Step 2d: calling load_universe (CBOE + Tradier validate + screen)")
        symbols, source, stream_eligible_set = await load_universe(db_snapshot=stale)
        log.info(
            "[universe] Step 2d: load_universe returned source=%s symbols=%d eligible=%s",
            source, len(symbols),
            len(stream_eligible_set) if stream_eligible_set is not None else "n/a",
        )
    except Exception as exc:
        log.error("[universe] Background universe resolve: load_universe failed: %s", exc, exc_info=True)
        return

    tier_map: dict[str, int] = {}
    quotes: list = []
    saved_snapshot_id: Optional[str] = None

    if source == "tradier_validated":
        log.info(
            "[universe] Step 2e: persisting tradier_validated snapshot (%d symbols, %d eligible) to DB",
            len(symbols),
            len(stream_eligible_set) if stream_eligible_set is not None else len(symbols),
        )
        try:
            # SAVE-SNAPSHOT-SET: build explicit symbol_rows so stream_eligible is
            # persisted correctly and stream_eligible_set never lands in `provider`.
            symbol_rows = _build_symbol_rows(symbols, stream_eligible_set)
            saved = await universe_store.save_snapshot(
                symbols,
                source,
                symbol_rows=symbol_rows,
            )
            if saved:
                log.info("[universe] Step 2e SUCCESS: snapshot persisted to DB")
                # FIX-UPSERT-ARG-ORDER: fetch the UUID of the snapshot we just wrote
                # so upsert_symbol_quotes() targets the correct rows.
                saved_snapshot_id = await universe_store.get_latest_snapshot_id()
                log.info(
                    "[universe] Step 2e: saved_snapshot_id=%s",
                    saved_snapshot_id,
                )
            else:
                log.error("[universe] Step 2e FAILED: save_snapshot returned False")
        except Exception as exc:
            log.error("[universe] Step 2e: save_snapshot raised: %s", exc, exc_info=True)

        try:
            log.info("[universe] Step 2f: fetching batch quotes for %d symbols", len(symbols))
            quotes = await _fetch_batch_quotes(symbols)
            if quotes:
                log.info("[universe] Step 2f: preliminary tier assignment for %d symbols", len(quotes))
                tier_map = await assign_tiers(quotes)
                log.info(
                    "[universe] Step 2f: preliminary tiers - T1=%d T2=%d T3=%d",
                    sum(1 for t in tier_map.values() if t == 1),
                    sum(1 for t in tier_map.values() if t == 2),
                    sum(1 for t in tier_map.values() if t == 3),
                )
        except Exception as exc:
            log.error("[universe] Step 2f: quote/tier fetch failed: %s", exc, exc_info=True)
    else:
        log.warning(
            "[universe] Step 2e SKIPPED: source=%s (not tradier_validated) - DB will NOT be updated",
            source,
        )

    if tier_map:
        try:
            registry.set_tier_map(tier_map)
            _sync_accumulator_tier_map(tier_map)
            log.info(
                "[universe] Background resolve: registry + accumulator tier_map patched "
                "(T1=%d T2=%d T3=%d)",
                sum(1 for t in tier_map.values() if t == 1),
                sum(1 for t in tier_map.values() if t == 2),
                sum(1 for t in tier_map.values() if t == 3),
            )
        except Exception as exc:
            log.error("[universe] Background resolve: tier_map patch failed: %s", exc, exc_info=True)

    # FIX-UPSERT-ARG-ORDER: pass (snapshot_id, enriched_quotes) — correct arg order.
    # Previously called upsert_symbol_quotes(quotes, tier_map) which put quote_rows
    # in snapshot_id and tier_map in quote_rows -> always (0 rows).
    if quotes and tier_map and saved_snapshot_id:
        try:
            enriched = _enrich_quotes_with_tier(quotes, tier_map)
            await universe_store.upsert_symbol_quotes(saved_snapshot_id, enriched)
            log.info("[universe] Background resolve: upsert_symbol_quotes complete (snapshot_id=%s)", saved_snapshot_id)
        except Exception as exc:
            log.error("[universe] Background resolve: upsert_symbol_quotes failed: %s", exc, exc_info=True)
    elif quotes and tier_map and not saved_snapshot_id:
        log.warning(
            "[universe] Background resolve: skipping upsert_symbol_quotes - "
            "saved_snapshot_id is None (save_snapshot may have failed)"
        )

    stream_symbols = (
        [s for s in symbols if s in stream_eligible_set]
        if stream_eligible_set is not None
        else symbols
    )

    if set(stream_symbols) != set(stream_symbols_container):
        log.info(
            "[universe] STREAM-RELOAD: symbol list changed (%d seed -> %d fresh) - "
            "cancelling current stream_task and relaunching",
            len(stream_symbols_container), len(stream_symbols),
        )
        old_task = stream_task_ref[0]
        old_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(old_task), timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

        stream_symbols_container.clear()
        stream_symbols_container.extend(stream_symbols)

        new_task = asyncio.create_task(
            stream_options_flow(stream_symbols_container, registry=registry)
        )
        stream_task_ref[0] = new_task
        log.info(
            "[universe] STREAM-RELOAD: new stream_task created with %d symbols",
            len(stream_symbols),
        )
    else:
        log.info(
            "[universe] STREAM-RELOAD: symbol list unchanged (%d symbols) - "
            "no stream restart needed",
            len(stream_symbols),
        )

    log.info(
        "[universe] Background resolve COMPLETE: %d stream symbols (source=%s, universe=%d)",
        len(stream_symbols), source, len(symbols),
    )
    # ← ADD vvv
    if universe_ready_event is not None:
        universe_ready_event.set()
        log.info(
            "[universe] UNIVERSE-READY-EVENT: set — _background_build_and_upsert "
            "may now proceed with %d stream symbols",
            len(stream_symbols_container),
        )


def _stamp_oi(quotes: list, oi_map: dict[str, int]) -> None:
    for q in quotes:
        q.open_interest = oi_map.get(q.symbol, 0)


def _sync_accumulator_tier_map(tier_map: dict[str, int]) -> None:
    """ING-010-ACC: push a fresh tier_map into the module-level accumulator."""
    try:
        from services import tradier_stream as _ts
        if hasattr(_ts, "accumulator") and _ts.accumulator is not None:
            _ts.accumulator.set_tier_map(tier_map)
            log.info(
                "[universe] ING-010-ACC: tradier_stream.accumulator.set_tier_map() called "
                "(%d symbols)", len(tier_map)
            )
        else:
            log.warning("[universe] ING-010-ACC: tradier_stream.accumulator not available - skipping")
    except Exception as exc:
        log.error("[universe] ING-010-ACC: set_tier_map on accumulator failed: %s", exc, exc_info=True)


def _filter_symbol_quotes(raw: list, caller: str) -> list:
    """BUILD-QUOTE-TYPE: filter raw_quotes to only SymbolQuote-like objects."""
    clean = [q for q in raw if hasattr(q, "average_volume")]
    dropped = len(raw) - len(clean)
    if dropped:
        log.warning(
            "[%s] BUILD-QUOTE-TYPE: dropped %d non-SymbolQuote item(s) from raw_quotes "
            "(total=%d kept=%d) - check symbol_registry.build() return value",
            caller, dropped, len(raw), len(clean),
        )
    return clean


async def _post_build_upsert(registry, raw_quotes: list) -> None:
    if not raw_quotes:
        log.warning("[post_build] raw_quotes is empty - skipping upsert")
        return

    # BUILD-QUOTE-TYPE: strip any non-SymbolQuote items before tier assignment.
    quotes = _filter_symbol_quotes(raw_quotes, "post_build")
    if not quotes:
        log.warning("[post_build] no valid SymbolQuote objects after filtering - skipping upsert")
        return

    tier_map = await assign_tiers(quotes)
    registry.set_tier_map(tier_map)
    _sync_accumulator_tier_map(tier_map)
    log.info(
        "[post_build] Tier assignment complete - T1=%d T2=%d T3=%d",
        sum(1 for t in tier_map.values() if t == 1),
        sum(1 for t in tier_map.values() if t == 2),
        sum(1 for t in tier_map.values() if t == 3),
    )

    # FIX-UPSERT-ARG-ORDER: fetch the real snapshot UUID first, then call
    # upsert_symbol_quotes(snapshot_id, quote_rows) in the correct order.
    # Previously called upsert_symbol_quotes(non_null, tier_map) which put
    # the quote list in snapshot_id and tier_map in quote_rows -> (0 rows).
    try:
        snapshot_id = await universe_store.get_latest_snapshot_id()
        if not snapshot_id:
            log.warning(
                "[post_build] upsert_symbol_quotes: no snapshot_id found in DB - "
                "skipping upsert (options_universe_symbols will not be updated)"
            )
            return

        log.info(
            "[post_build] upsert_symbol_quotes: snapshot_id=%s, %d quotes",
            snapshot_id, len(quotes),
        )
        enriched = _enrich_quotes_with_tier(quotes, tier_map)
        await universe_store.upsert_symbol_quotes(snapshot_id, enriched)
        log.info("[post_build] upsert_symbol_quotes complete")
    except Exception as exc:
        log.error("[post_build] upsert_symbol_quotes failed: %s", exc, exc_info=True)


async def _background_build_and_upsert(
    registry,
    stream_symbols: list[str],
    build_done_event: asyncio.Event,
    universe_ready_event: Optional[asyncio.Event] = None,   # ← ADD
) -> None:
    try:
        # ── ADD: wait for universe symbols on cold start ──────────────
        if universe_ready_event is not None:
            log.info(
                "[build] UNIVERSE-SYNC: waiting for _background_universe_resolve "
                "before pre-fetching quotes or calling build() "
                "(stream_symbols currently %d)",
                len(stream_symbols),
            )
            try:
                await asyncio.wait_for(universe_ready_event.wait(), timeout=300.0)
                log.info(
                    "[build] UNIVERSE-SYNC: universe_ready_event received — "
                    "proceeding with %d stream symbols",
                    len(stream_symbols),
                )
            except asyncio.TimeoutError:
                log.warning(
                    "[build] UNIVERSE-SYNC: universe_ready_event not set after 300 s "
                    "(background universe resolve may have failed) — "
                    "proceeding with %d stream symbols anyway",
                    len(stream_symbols),
                )
        # ── END ADD ───────────────────────────────────────────────────

        seeded_count = len(registry._registry) if hasattr(registry, "_registry") else 0
        epoch = getattr(registry, "epoch", 0)

        if seeded_count >= _P1_MIN_CONTRACTS and epoch == 0:
            log.info(
                "[build] FIX-P1-SKIP-BUILD: registry seeded with %d contracts from P1 "
                "fallback (epoch=%d) - skipping full Tradier build. "
                "Running tier assignment from batch quotes (FIX-P1-SKIP-TIERS).",
                seeded_count, epoch,
            )
            registry._build_complete = True
            registry.epoch = 1

            # FIX-P1-SKIP-TIERS: tier assignment must still run even when the
            # full chain-pull is skipped. _fetch_batch_quotes() is a shallow
            # volume/OI fetch (not a per-ticker chain-pull) -- it completes in
            # seconds and returns SymbolQuote objects suitable for assign_tiers().
            # Without this, the tier_map loaded at startup (all T3 from a stale
            # DB snapshot) is never refreshed, producing an infinite stale-tier
            # loop across every subsequent boot.
            try:
                symbols_for_tiers = stream_symbols if stream_symbols else []
                if symbols_for_tiers:
                    log.info(
                        "[build] FIX-P1-SKIP-TIERS: fetching batch quotes for %d symbols",
                        len(symbols_for_tiers),
                    )
                    skip_quotes = await _fetch_batch_quotes(symbols_for_tiers)
                    if skip_quotes:
                        await _post_build_upsert(registry, skip_quotes)
                    else:
                        log.warning(
                            "[build] FIX-P1-SKIP-TIERS: _fetch_batch_quotes returned no quotes "
                            "for %d symbols - tiers remain as loaded from DB snapshot",
                            len(symbols_for_tiers),
                        )
                else:
                    log.warning(
                        "[build] FIX-P1-SKIP-TIERS: stream_symbols is empty - "
                        "skipping tier assignment, tiers remain as loaded from DB snapshot"
                    )
            except Exception as exc:
                log.error(
                    "[build] FIX-P1-SKIP-TIERS: tier assignment failed: %s",
                    exc, exc_info=True,
                )
            return

        log.info(
            "[build] Starting background OCC registry build "
            "(seeded_count=%d, epoch=%d)...",
            seeded_count, epoch,
        )

        # FIX-QQ1-BUILD-SEQUENCING: tier_map must be refreshed BEFORE build() so
        # _build_with_sem reads the correct tier via self._tier_map.get(ticker, 3)
        # when selecting tier_params for the chain pull.
        pre_quotes: list = []
        if stream_symbols:
            try:
                log.info(
                    "[build] FIX-QQ1-BUILD-SEQUENCING: pre-fetching batch quotes for %d symbols before build()",
                    len(stream_symbols),
                )
                pre_quotes = await _fetch_batch_quotes(stream_symbols)
                if pre_quotes:
                    pre_tier_map = await assign_tiers(pre_quotes)
                    registry.set_tier_map(pre_tier_map)
                    _sync_accumulator_tier_map(pre_tier_map)
                    log.info(
                        "[build] FIX-QQ1-BUILD-SEQUENCING: pre-build tier_map applied "
                        "from %d SymbolQuotes (T1=%d T2=%d T3=%d)",
                        len(pre_quotes),
                        sum(1 for t in pre_tier_map.values() if t == 1),
                        sum(1 for t in pre_tier_map.values() if t == 2),
                        sum(1 for t in pre_tier_map.values() if t == 3),
                    )
                else:
                    log.warning(
                        "[build] FIX-QQ1-BUILD-SEQUENCING: pre-fetch returned no quotes - "
                        "proceeding with DB-loaded tiers"
                    )
            except Exception as exc:
                log.warning(
                    "[build] FIX-QQ1-BUILD-SEQUENCING: pre-fetch/tier assignment failed: %s - "
                    "proceeding with DB-loaded tiers",
                    exc,
                    exc_info=True,
                )

        try:
            build_count, _ = await registry.build()
            log.info(
                "[build] registry.build() complete - build_count=%d contracts_loaded=%d",
                build_count,
                len(registry._registry) if hasattr(registry, "_registry") else 0,
            )
        except Exception as exc:
            log.error("[build] registry.build() failed: %s", exc, exc_info=True)
            return

        if pre_quotes:
            try:
                await _post_build_upsert(registry, pre_quotes)
            except Exception as exc:
                log.error("[build] _post_build_upsert failed: %s", exc, exc_info=True)
        else:
            log.warning(
                "[build] FIX-QQ1-BUILD-SEQUENCING: skipping _post_build_upsert - "
                "no pre-fetched SymbolQuotes available"
            )

    finally:
        registry._build_complete = True
        build_done_event.set()
        log.info("[build] _registry_build_done event set - chain_refresh unblocked")


async def _registry_prewarm_loop(build_done_event: asyncio.Event) -> None:
    ET = ZoneInfo("America/New_York")
    while True:
        now = datetime.now(ET)
        target = now.replace(hour=9, minute=15, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        sleep_secs = (target - now).total_seconds()
        log.info("[prewarm] Next pre-warm scheduled in %.0f s (9:15 AM ET)", sleep_secs)
        await asyncio.sleep(sleep_secs)

        registry = get_registry()
        if registry is None:
            log.warning("[prewarm] Registry not initialised - skipping pre-warm")
            continue

        if getattr(registry, "epoch", 0) == 0:
            log.warning(
                "[prewarm] Skipping pre-warm - registry.epoch==0 (initial build not complete)"
            )
            continue

        log.info("[prewarm] Starting 9:15 AM ET pre-warm build")
        try:
            invalidate_vol_oi_cache()
            pre_quotes: list = []
            tracked_symbols = []
            if hasattr(registry, "_tier_map") and registry._tier_map:
                tracked_symbols = list(registry._tier_map.keys())
            elif hasattr(registry, "watchlist") and registry.watchlist:
                tracked_symbols = list(registry.watchlist)

            if tracked_symbols:
                try:
                    pre_quotes = await _fetch_batch_quotes(tracked_symbols)
                    if pre_quotes:
                        pre_tier_map = await assign_tiers(pre_quotes)
                        registry.set_tier_map(pre_tier_map)
                        _sync_accumulator_tier_map(pre_tier_map)
                except Exception as exc:
                    log.warning("[prewarm] pre-fetch tier refresh failed: %s", exc, exc_info=True)

            _build_count, _ = await registry.build()
            if pre_quotes:
                await _post_build_upsert(registry, pre_quotes)
            else:
                log.warning("[prewarm] no SymbolQuotes available for _post_build_upsert after build()")
            log.info("[prewarm] Pre-warm build complete")
        except Exception as exc:
            log.error("[prewarm] Pre-warm build failed: %s", exc, exc_info=True)


async def _universe_refresh_loop(build_done_event: asyncio.Event) -> None:
    INTERVAL = 24 * 3600
    while True:
        await asyncio.sleep(INTERVAL)
        log.info("[universe_refresh] 24h interval reached - refreshing universe snapshot")
        try:
            stale = await universe_store.load_any_snapshot()
            symbols, source, stream_eligible_set = await load_universe(db_snapshot=stale)
            if source == "tradier_validated":
                # SAVE-SNAPSHOT-SET: build explicit symbol_rows; never pass
                # stream_eligible_set as the positional `provider` argument.
                symbol_rows = _build_symbol_rows(symbols, stream_eligible_set)
                await universe_store.save_snapshot(
                    symbols,
                    source,
                    symbol_rows=symbol_rows,
                )
                log.info(
                    "[universe_refresh] Snapshot refreshed - %d symbols (source=%s)",
                    len(symbols), source,
                )

                registry = get_registry()
                if registry is not None:
                    oi_map = registry.get_oi_map() if hasattr(registry, "get_oi_map") else {}
                    log.info(
                        "[universe_refresh] OI map from live registry: %d entries",
                        len(oi_map),
                    )
            else:
                log.warning(
                    "[universe_refresh] source=%s - snapshot NOT updated", source
                )
        except Exception as exc:
            log.error("[universe_refresh] refresh failed: %s", exc, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # -- Step 0: load gate config ------------------------------------------
    log.info("[startup] Step 0: loading gate config from DB (ING-010)")
    try:
        await gate_config_store.load()
        log.info("[startup] Step 0: gate config loaded")
    except Exception as exc:
        log.error("[startup] Step 0: gate_config_store.load() failed: %s", exc, exc_info=True)

    # -- Step 1: validate ingestion config ---------------------------------
    log.info("[startup] Step 1: validating ingestion config (RC-3)")
    try:
        await validate_ingestion_config()
    except Exception as exc:
        log.warning("[startup] Step 1: validate_ingestion_config failed: %s", exc)

    # -- Step 2a: fast DB-only universe resolution -------------------------
    log.info("[startup] Step 2a: resolving startup universe (DB-only fast path)")
    stream_symbols, tier_map, snapshot_id, needs_universe_refresh = (
        await _resolve_startup_universe_fast()
    )
    log.info(
        "[startup] Step 2a complete - %d stream symbols, needs_refresh=%s",
        len(stream_symbols), needs_universe_refresh,
    )

    # -- Step 3: init registry ---------------------------------------------
    log.info("[startup] Step 3: initialising symbol registry")
    registry = init_registry(watchlist=stream_symbols)
    if tier_map:
        registry.set_tier_map(tier_map)
        _sync_accumulator_tier_map(tier_map)

    # -- Step 4: seed OCC chains from DB -----------------------------------
    log.info("[startup] Step 4: seeding OCC chains from DB (P1 fallback, snapshot_id=%s)", snapshot_id)
    try:
        await registry.load_from_db(snapshot_id)
        log.info(
            "[startup] Step 4: DB seed complete - %d contracts loaded",
            len(registry._registry) if hasattr(registry, "_registry") else 0,
        )
    except Exception as exc:
        log.error("[startup] Step 4: registry.load_from_db() failed: %s", exc, exc_info=True)

    # -- Step 4b: log tradier_stream module constants for observability ----
    try:
        from services import tradier_stream as _ts_inspect
        spawn_delay = getattr(_ts_inspect, "_WORKER_SPAWN_DELAY_S", "NOT FOUND")
        log.info(
            "[startup] MAIN-DEBUG-001: tradier_stream._WORKER_SPAWN_DELAY_S=%s",
            spawn_delay,
        )
        if isinstance(spawn_delay, (int, float)) and spawn_delay < 0.5:
            log.warning(
                "[startup] MAIN-DEBUG-001: _WORKER_SPAWN_DELAY_S=%.3f is below 0.5 - "
                "Tradier ConnectTimeout risk at high worker counts.",
                spawn_delay,
            )
    except Exception as exc:
        log.warning("[startup] MAIN-DEBUG-001: could not inspect tradier_stream constants: %s", exc)

    # -- Step 5: yield - server is live ------------------------------------
    log.info("[startup] Step 5: yielding - server is live (health probe will pass)")

    _registry_build_done  = asyncio.Event()
    _universe_ready_event = asyncio.Event() if needs_universe_refresh else None   # ← ADD

    registry_refresh_task = asyncio.create_task(registry.refresh_loop())
    prewarm_task          = asyncio.create_task(_registry_prewarm_loop(_registry_build_done))
    stream_task = asyncio.create_task(stream_options_flow(stream_symbols, registry=registry))
    stream_task_ref = [stream_task]
    db_write_task         = asyncio.create_task(start_flow_writer())
    signal_write_task     = asyncio.create_task(start_signal_writer())
    refresh_task          = asyncio.create_task(_universe_refresh_loop(_registry_build_done))
    build_task = asyncio.create_task(
        _background_build_and_upsert(
            registry, stream_symbols, _registry_build_done,
            universe_ready_event=_universe_ready_event,           # ← ADD
        )
    )
    lookback_task         = asyncio.create_task(start_lookback_worker())

    universe_resolve_task = (
        asyncio.create_task(_background_universe_resolve(
            registry,
            stream_task_ref=stream_task_ref,
            stream_symbols_container=stream_symbols,
            universe_ready_event=_universe_ready_event,           # ← ADD
        ))
        if needs_universe_refresh
        else None
    )
    if universe_resolve_task:
        log.info(
            "[universe] Cache miss at startup - background universe resolve task spawned"
        )

    def _get_tracked_tickers() -> list:
        reg = get_registry()
        if reg is not None and hasattr(reg, "_registry") and reg._registry:
            return list({v.ticker for v in reg._registry.values()})
        return list(stream_symbols)

    log.info(
        "[chain_refresh] SEQ-002: chain refresh worker will start after "
        "build_done_event fires + %d s stagger (SEQ-002-STAGGER)",
        _CHAIN_REFRESH_STAGGER_S,
    )
    chain_refresh_task = asyncio.create_task(
        _chain_refresh_after_build(
            get_tracked_symbols=_get_tracked_tickers,
            fetch_chain_fn=_fetch_tradier_chain,
            build_done_event=_registry_build_done,
        )
    )

    self_ping_task = asyncio.create_task(_self_ping_worker())

    gate_config_refresh_task = asyncio.create_task(
        gate_config_store.start_refresh_loop(300)
    )
    log.info("[startup] Step 6-k: gate_config refresh loop started (interval=300s)")

    yield

    log.info("[shutdown] Closing Tradier stream connections first...")
    stream_task_ref[0].cancel()
    lookback_task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(stream_task_ref[0]), timeout=5.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    log.info("[shutdown] Stream task stopped - Tradier session quota released")

    build_task.cancel()
    refresh_task.cancel()
    prewarm_task.cancel()
    registry_refresh_task.cancel()
    db_write_task.cancel()
    signal_write_task.cancel()
    chain_refresh_task.cancel()
    self_ping_task.cancel()
    gate_config_refresh_task.cancel()
    if universe_resolve_task:
        universe_resolve_task.cancel()

    tasks_to_await = [
        build_task, refresh_task, prewarm_task,
        registry_refresh_task, db_write_task, signal_write_task,
        chain_refresh_task, self_ping_task, gate_config_refresh_task,
    ]
    if universe_resolve_task:
        tasks_to_await.append(universe_resolve_task)

    results = await asyncio.gather(*tasks_to_await, return_exceptions=True)
    for task, result in zip(tasks_to_await, results):
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            log.warning("[shutdown] Task %s raised: %s", task.get_name(), result)

    log.info("[shutdown] All background tasks stopped - shutdown complete")


app = FastAPI(
    title="Cipher Backend",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(flow.router)
app.include_router(simulation.router)
app.include_router(ws.router)
app.include_router(smart_signals.router)
app.include_router(history.router)
app.include_router(admin.router)
app.include_router(health.router)
app.include_router(ingestion_config_router.router)   # REARCH-002
app.include_router(signal_config_router.router)      # REARCH-005


@app.get("/config")
async def read_config(config: dict = Depends(get_config)):
    return config


@app.get("/stream-stats")
async def get_stream_stats(current_user=Depends(get_current_user)):
    return stream_stats

# Add this BEFORE any router includes or startup hooks — at app definition time:
@app.get("/health")
async def health():
    return {"status": "ok"}
