"""
Cipher Backend — FastAPI entry point

Startup sequence:
  0. init_http_client()                — warm shared httpx connection pool
  0. gate_config_store.load()          — load tier gate config from DB into memory  [ING-010]
  1. validate_ingestion_config()       — warn on missing ingestion config rows  [RC-3]
  2. _resolve_startup_universe_fast()  — DB-only path: fresh snapshot HIT → done;
                                         MISS → seed with stale snapshot and schedule
                                         background refresh  [RENDER-STARTUP-HANG]
  3. init_registry()                   — in-memory init (instant)
  4. registry.load_from_db()           — seed OCC chains from DB via P1 fallback
  5. yield                             — SERVER IS LIVE, health probe passes (~6 s)
  6. Parallel background tasks launched:
     a. _background_build_and_upsert   — incremental/full OCC build (P4)
     b. _background_universe_resolve() — MISS only: load_universe() + save + tier assign
     c. registry.refresh_loop()        — scheduled 30-min rebuilds
     d. _registry_prewarm_loop()       — 9:15 AM ET daily pre-warm
     e. stream_options_flow()          — waits for is_ready(), then streams
     f. start_flow_writer()            — DB flush loop
     g. start_signal_writer()          — signal DB writer
     h. _universe_refresh_loop()       — 24h universe refresh
     i. start_chain_refresh_worker()   — ING-008: 5-min chain cache refresh loop
     j. _self_ping_worker()            — RENDER: pings /health every 5 min to prevent spin-down

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
  ING-008 (main)      — start_chain_refresh_worker() launched as a background task
                        after yield. Refreshes options chain vol/OI for all
                        stream_eligible symbols every 5 minutes via Tradier chain API.
                        Zero live API calls on the flow hot path — persist_flow_event
                        and persist_flow_episode read from the in-process cache only.
                        FIX: wired with correct two-callable signature:
                          get_tracked_symbols — lambda returning live OCC symbol list
                          fetch_chain_fn      — _fetch_tradier_chain(symbol) helper
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
                        its own accumulator internally via get_accumulator() — it
                        takes 0 positional args. Removed stale registry.accumulator
                        argument from the create_task() call site.
  RENDER-KEEPALIVE    — _self_ping_worker() pings GET /health every 5 minutes.
                        Reads RENDER_EXTERNAL_URL env var (injected automatically by
                        Render). No-ops locally when the var is absent. Prevents
                        free-tier spin-down (15-min idle threshold).
                        Hardened (HOTFIX-KEEPALIVE-001):
                          - 5-min interval (down from 10) — more headroom vs threshold
                          - Fresh httpx.AsyncClient each cycle — avoids stale pool
                          - fail_streak counter — log.warning per failure,
                            log.error after 3 consecutive failures
                        Does NOT prevent restarts caused by deploys, crashes, or OOM
                        — tradier_stream reconnect logic handles those cases.
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
                        market hours (Mon-Fri 9:15 AM – 4:30 PM ET; never on weekends).
                        Tradier's chain endpoint returns HTTP 400 when markets are
                        closed, flooding logs with warnings all night. The guard is
                        co-located in the fetch helper so the chain_refresh_task loop
                        itself is untouched — it simply sleeps its normal interval
                        between empty-return calls.
  HOTFIX-KEEPALIVE-001 — _self_ping_worker() hardened:
                        - interval dropped to 5 min (was 10) — more margin vs 15-min
                          Render spin-down threshold
                        - httpx.AsyncClient recreated each cycle — avoids stale
                          connection pool that causes silent failures on Render
                        - fail_streak counter replaces silent except-swallow;
                          log.warning on every failure, log.error after streak >= 3
                          with actionable guidance to check RENDER_EXTERNAL_URL
  STATS-STREAM-ALIAS  — /stream/stats stub replaced: was returning {"status": "ok"}
                        (dead stub that discarded all funnel stats). Now calls
                        get_stats() from tradier_stream and returns the full raw
                        stats dict — identical to GET /stats — for ops debugging.
                        include_in_schema=False retained (internal ops only).
  STEP2A-LOG-FIX      — Step 2a registry-seed log moved outside `if snapshot_id:`
                        so it fires on both warm (snapshot present) and cold
                        (no snapshot in DB) startup paths. registry.epoch removed
                        from this log — epoch is a build() artifact not set here,
                        logging it pre-build always showed 0 (misleading).
  FLAW3-POOL          — Shared httpx connection pool via tradier_client._shared_client.
                        init_http_client() called at lifespan step 0 (before any
                        Tradier I/O). close_http_client() called during graceful
                        shutdown after stream_task is cancelled so the pool remains
                        live for any in-flight retries. _fetch_tradier_chain() in
                        main.py updated to use the shared pool instead of creating
                        a new AsyncClient per call. Eliminates ~22 min of TCP
                        handshake overhead during cold-start registry build.
  FIX-CHAIN-400       — _fetch_tradier_chain() was sending GET /v1/markets/options/chains
                        with only {symbol, greeks=false}. Tradier requires
                        ?expiration=YYYY-MM-DD and returns HTTP 400 without it.
                        Because _refresh_one() in chain_store treated [] as a
                        non-error return, every cycle logged refreshed=N errors=0
                        cache_size=0 — the vol/OI cache was permanently empty.
                        Fix: call get_expirations(symbol) first to resolve all active
                        expiry dates, then call get_option_chain(symbol, expiry) per
                        date and flatten results. Matches the registry build() path
                        exactly. The inline httpx block is removed — all Tradier I/O
                        now routes through tradier_client functions consistently.
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
from services.tradier_stream import stream_options_flow, get_stats as get_stream_stats
from services.gate_config_store import store as gate_config_store  # HOTFIX-IMPORT-001
from utils.tradier_client import (  # FLAW3-POOL + FIX-CHAIN-400
    init_http_client,
    close_http_client,
    get_expirations,
    get_option_chain,
)

import httpx


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


# ---------------------------------------------------------------------------
# RENDER-KEEPALIVE: Self-ping worker to prevent free-tier spin-down.
#
# Render's free tier spins down a service after 15 minutes of no inbound
# traffic. This worker pings GET /health every 5 minutes to keep the
# instance alive (well inside the 15-min threshold).
#
# RENDER_EXTERNAL_URL is injected automatically by Render (e.g.
# https://cipher-backend.onrender.com). When absent (local dev, other
# platforms) the worker exits immediately — no-op.
#
# HOTFIX-KEEPALIVE-001 hardening:
#   - Interval dropped to 5 min (was 10) — more headroom vs threshold
#   - Fresh AsyncClient per cycle — avoids stale connection pool on Render
#   - fail_streak counter — visible log.warning per failure; log.error
#     with actionable guidance after 3 consecutive failures
#
# This only prevents idle spin-down. It does NOT prevent restarts caused
# by deploys, crashes, or OOM — tradier_stream reconnect logic handles those.
# ---------------------------------------------------------------------------
async def _self_ping_worker() -> None:
    """RENDER-KEEPALIVE: pings /health every 5 min. First ping at T+5."""
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url:
        log.info("[keepalive] RENDER_EXTERNAL_URL not set — self-ping disabled (non-Render env)")
        return
    ping_url = f"{url}/health"
    log.info("[keepalive] Self-ping worker started — target: %s (every 5 min)", ping_url)
    fail_streak = 0
    while True:
        await asyncio.sleep(300)  # 5 minutes — well inside the 15-min threshold
        try:
            # Fresh client each cycle — avoids stale connection pool on Render
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(ping_url)
            fail_streak = 0
            log.debug("[keepalive] Ping OK — HTTP %d", resp.status_code)
        except Exception as exc:
            fail_streak += 1
            log.warning(
                "[keepalive] Ping FAILED (streak=%d): %s",
                fail_streak, exc,
            )
            if fail_streak >= 3:
                log.error(
                    "[keepalive] 3 consecutive ping failures — "
                    "Render may spin down. Check RENDER_EXTERNAL_URL and network.",
                )


# ---------------------------------------------------------------------------
# HOTFIX-CHAIN-HOURS: Market hours guard for Tradier chain fetches.
#
# Tradier's GET /v1/markets/options/chains returns HTTP 400 outside regular
# market hours. The chain_refresh_task runs continuously 24/7, so without
# this guard every symbol floods the warning log all night and on weekends.
#
# Window: Mon-Fri, 9:15 AM – 4:30 PM America/New_York.
#   - 9:15 AM: 15-min pre-market buffer before the 9:30 open so the cache
#     is warm by the time flow events start arriving.
#   - 4:30 PM: 30-min post-close buffer to capture any late prints.
# ---------------------------------------------------------------------------
_ET = ZoneInfo("America/New_York")


def _is_market_hours() -> bool:
    """HOTFIX-CHAIN-HOURS: True only during Mon-Fri 9:15 AM – 4:30 PM ET."""
    now = datetime.now(_ET)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return time(9, 15) <= now.time() <= time(16, 30)


# ---------------------------------------------------------------------------
# FIX-CHAIN-400: Tradier chain fetch helper for the background refresh worker.
#
# Root cause: the previous implementation sent only {symbol, greeks=false}
# to GET /v1/markets/options/chains. Tradier requires ?expiration=YYYY-MM-DD
# and returns HTTP 400 without it. Because _refresh_one() in chain_store.py
# treats an empty [] return as a non-error success, every cycle logged:
#   refreshed=N errors=0 cache_size=0
# meaning the vol/OI cache was permanently empty.
#
# Fix: call get_expirations(symbol) first to resolve all active expiry dates
# for the ticker, then call get_option_chain(symbol, expiry) per date and
# flatten all returned contract dicts. This matches exactly what the registry
# build() path does via get_option_chain_bulk(). The previous inline httpx
# block (url/headers/params constructed inside main.py) is removed — all
# Tradier I/O now routes through tradier_client functions consistently.
#
# HOTFIX-CHAIN-HOURS: Returns [] immediately outside market hours
# (Mon-Fri 9:15 AM – 4:30 PM ET) — Tradier returns HTTP 400 when markets
# are closed, so there is nothing useful to fetch.
#
# Used exclusively by start_chain_refresh_worker() as the fetch_chain_fn
# callable. Returns a flat list of option contract dicts. On any network or
# API error returns [] so the worker continues to the next symbol without
# aborting the refresh cycle.
# ---------------------------------------------------------------------------
async def _fetch_tradier_chain(symbol: str) -> list:
    """
    FIX-CHAIN-400: Fetch the full options chain for `symbol` across all
    active expiration dates.

    Calls get_expirations(symbol) to resolve all active expiry dates, then
    calls get_option_chain(symbol, expiry) for each date and returns the
    flattened list of contract dicts.

    Each returned dict includes at minimum:
      - "symbol"        : str  (21-char OCC symbol)
      - "volume"        : int  (today's volume for this contract)
      - "open_interest" : int

    Returns [] immediately outside market hours (HOTFIX-CHAIN-HOURS) or on
    any error. Never raises — callers must handle [] as a valid no-data signal.
    """
    if not _is_market_hours():
        log.debug("[chain_refresh] %s — skipped (market closed)", symbol)
        return []

    if not settings.TRADIER_API_KEY:
        return []

    try:
        expirations = await get_expirations(symbol)
        if not expirations:
            log.debug("[chain_refresh] %s — no expirations returned", symbol)
            return []

        all_contracts: list = []
        for expiry in expirations:
            contracts = await get_option_chain(symbol, expiry)
            all_contracts.extend(contracts)

        log.debug(
            "[chain_refresh] %s — fetched %d contracts across %d expirations",
            symbol, len(all_contracts), len(expirations),
        )
        return all_contracts

    except Exception as exc:
        log.warning(
            "[chain_refresh] _fetch_tradier_chain(%s) error: %s",
            symbol, exc,
        )
        return []


# ---------------------------------------------------------------------------
# RENDER-STARTUP-HANG fix
#
# The original _resolve_startup_universe() ran the full load_universe() path
# (CBOE + Tradier validate, 60-120 s) before yield, blocking the health probe.
#
# Split into two functions:
#
#   _resolve_startup_universe_fast()  — DB reads only (<2 s), runs before yield.
#     Returns (stream_symbols, tier_map, snapshot_id, needs_refresh).
#     needs_refresh=True when no fresh snapshot exists; the caller must then
#     schedule _background_universe_resolve() as a post-yield task.
#
#   _background_universe_resolve()    — slow path, runs after yield as a task.
#     Calls load_universe(), saves the snapshot, assigns tiers, and patches
#     the live registry + accumulator without blocking the health probe.
# ---------------------------------------------------------------------------

async def _resolve_startup_universe_fast() -> tuple[list[str], dict[str, int], str, bool]:
    """
    RENDER-STARTUP-HANG: DB-only startup universe resolution.

    Fast path (cache HIT — normal daily operation):
      Loads the fresh snapshot from DB (max_age=24h) and returns immediately.
      needs_refresh=False — no background refresh is needed.

    Slow path (cache MISS — first deploy or snapshot >24 h old):
      Falls back to the most-recent stale snapshot as a minimal seed so the
      stream and registry start with something rather than an empty list.
      needs_refresh=True — caller must schedule _background_universe_resolve().

    No Tradier calls, no CBOE calls.  All DB I/O via universe_store.
    Expected wall-clock time: <2 s in both paths.
    """
    log.info("[universe] Step 2a: checking for fresh DB snapshot (max_age=24h)")

    fresh = await universe_store.load_fresh_snapshot(max_age_hours=24)
    if fresh:
        log.info(
            "[universe] Step 2a HIT: loaded fresh universe from DB (%d symbols) — stream starting",
            len(fresh),
        )
        tier_map = await universe_store.load_tier_map()
        log.info(
            "[universe] Step 2a: tier_map loaded (%d symbols mapped, T1=%d T2=%d T3=%d)",
            len(tier_map),
            sum(1 for t in tier_map.values() if t == 1),
            sum(1 for t in tier_map.values() if t == 2),
            sum(1 for t in tier_map.values() if t == 3),
        )
        active_snap = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: universe_store._client()
                .table("options_universe_snapshots")
                .select("id")
                .eq("is_active", True)
                .order("fetched_at", desc=True)
                .limit(1)
                .execute()
                .data,
        )
        snapshot_id = active_snap[0]["id"] if active_snap else ""
        return fresh, tier_map, snapshot_id, False  # needs_refresh=False

    # Cache MISS — seed with stale snapshot so the registry and stream start
    # with something.  The slow refresh runs post-yield.
    log.info(
        "[universe] Step 2a MISS: no fresh snapshot — "
        "seeding from stale snapshot, scheduling background refresh"
    )
    stale = await universe_store.load_any_snapshot()
    if stale:
        log.info(
            "[universe] Step 2a stale seed: %d symbols available for stream seed",
            len(stale),
        )
        tier_map = await universe_store.load_tier_map()
        active_snap = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: universe_store._client()
                .table("options_universe_snapshots")
                .select("id")
                .eq("is_active", True)
                .order("fetched_at", desc=True)
                .limit(1)
                .execute()
                .data,
        )
        snapshot_id = active_snap[0]["id"] if active_snap else ""
        return stale, tier_map, snapshot_id, True  # needs_refresh=True

    log.warning(
        "[universe] Step 2a: no snapshot in DB at all — "
        "starting with empty symbol list, background refresh will populate"
    )
    return [], {}, "", True  # needs_refresh=True


async def _background_universe_resolve(registry) -> None:
    """
    RENDER-STARTUP-HANG: slow universe refresh that runs post-yield.

    Called only when _resolve_startup_universe_fast() returned needs_refresh=True
    (i.e. no fresh 24h snapshot existed at startup).

    Runs the full load_universe() pipeline (CBOE + Tradier validate + screen),
    saves the snapshot to DB, assigns tiers, then live-patches:
      - registry.set_tier_map()
      - tradier_stream.accumulator.set_tier_map()  (ING-010-ACC)
      - universe_store.upsert_symbol_quotes()

    The stream workers are already running at this point (they started with the
    stale seed).  Patching the registry tier_map is thread-safe — set_tier_map()
    replaces the dict atomically.
    """
    log.info("[universe] Background universe refresh starting (cache miss at startup)")
    try:
        stale = await universe_store.load_any_snapshot()
        log.info(
            "[universe] Step 2b: checking env — TRADIER_API_KEY set=%s SUPABASE_URL set=%s",
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

    if source == "tradier_validated":
        log.info(
            "[universe] Step 2e: persisting tradier_validated snapshot (%d symbols, %d eligible) to DB",
            len(symbols),
            len(stream_eligible_set) if stream_eligible_set is not None else len(symbols),
        )
        try:
            saved = await universe_store.save_snapshot(symbols, source, stream_eligible_set)
            if saved:
                log.info("[universe] Step 2e SUCCESS: snapshot persisted to DB")
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
                    "[universe] Step 2f: preliminary tiers — T1=%d T2=%d T3=%d",
                    sum(1 for t in tier_map.values() if t == 1),
                    sum(1 for t in tier_map.values() if t == 2),
                    sum(1 for t in tier_map.values() if t == 3),
                )
        except Exception as exc:
            log.error("[universe] Step 2f: quote/tier fetch failed: %s", exc, exc_info=True)
    else:
        log.warning(
            "[universe] Step 2e SKIPPED: source=%s (not tradier_validated) — DB will NOT be updated",
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

    if quotes and tier_map:
        try:
            await universe_store.upsert_symbol_quotes(quotes, tier_map)
            log.info("[universe] Background resolve: upsert_symbol_quotes complete")
        except Exception as exc:
            log.error("[universe] Background resolve: upsert_symbol_quotes failed: %s", exc, exc_info=True)

    stream_symbols = (
        [s for s in symbols if s in stream_eligible_set]
        if stream_eligible_set is not None
        else symbols
    )
    log.info(
        "[universe] Background resolve COMPLETE: %d stream symbols (source=%s, universe=%d)",
        len(stream_symbols), source, len(symbols),
    )


def _stamp_oi(quotes: list, oi_map: dict[str, int]) -> None:
    for q in quotes:
        q.open_interest = oi_map.get(q.symbol, 0)


def _sync_accumulator_tier_map(tier_map: dict[str, int]) -> None:
    """
    ING-010-ACC: propagate the freshly-assigned tier_map to the module-level
    accumulator in tradier_stream.py.
    """
    try:
        import services.tradier_stream as _ts
        _ts.accumulator.set_tier_map(tier_map)
        log.info(
            "[ING-010-ACC] tradier_stream.accumulator tier_map synced "
            "(T1=%d T2=%d T3=%d)",
            sum(1 for t in tier_map.values() if t == 1),
            sum(1 for t in tier_map.values() if t == 2),
            sum(1 for t in tier_map.values() if t == 3),
        )
    except Exception as exc:
        log.warning(
            "[ING-010-ACC] Could not sync tier_map to tradier_stream.accumulator: %s",
            exc,
        )


async def _post_build_upsert(
    registry,
    stream_symbols: list[str],
    raw_quotes: Optional[dict[str, dict]] = None,
) -> None:
    from services.symbols_loader import SymbolQuote

    try:
        if raw_quotes is not None:
            log.info(
                "[post_build] Reusing %d raw_quotes from build() — "
                "skipping duplicate _fetch_batch_quotes call",
                len(raw_quotes),
            )
            oi_map = registry.get_oi_map()
            quotes = []
            for sym in stream_symbols:
                q = raw_quotes.get(sym, {})
                price = 0.0
                for key in ("last", "last_price", "close", "prevclose"):
                    val = q.get(key)
                    if val:
                        try:
                            price = float(val)
                            break
                        except (TypeError, ValueError):
                            pass
                vol = avg_vol = 0
                try:
                    vol = int(q.get("volume") or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    avg_vol = int(q.get("average_volume") or 0)
                except (TypeError, ValueError):
                    pass
                if price > 0:
                    quotes.append(SymbolQuote(
                        symbol         = sym,
                        last_price     = price,
                        volume         = vol,
                        average_volume = avg_vol,
                        open_interest  = oi_map.get(sym, 0),
                    ))
        else:
            log.info("[post_build] Fetching quotes for %d symbols", len(stream_symbols))
            quotes = await _fetch_batch_quotes(stream_symbols)

        if not quotes:
            log.warning("[post_build] No quotes available — skipping upsert")
            return

        oi_map = registry.get_oi_map()
        _stamp_oi(quotes, oi_map)
        log.info(
            "[post_build] OI stamped on %d quotes (%d tickers with oi>0)",
            len(quotes), sum(1 for v in oi_map.values() if v > 0),
        )
    except Exception as exc:
        log.error(
            "[post_build] Phase 1 (quote assembly) failed — upsert skipped: %s",
            exc, exc_info=True,
        )
        return

    try:
        tier_map = await assign_tiers(quotes)
        registry.set_tier_map(tier_map)
        _sync_accumulator_tier_map(tier_map)
        log.info(
            "[post_build] Tier map updated — T1=%d T2=%d T3=%d",
            sum(1 for t in tier_map.values() if t == 1),
            sum(1 for t in tier_map.values() if t == 2),
            sum(1 for t in tier_map.values() if t == 3),
        )
    except Exception as exc:
        log.warning(
            "[post_build] Phase 2 (assign_tiers) FAILED — "
            "DB tier columns NOT updated, upsert skipped: %s",
            exc, exc_info=True,
        )
        raise

    try:
        await universe_store.upsert_symbol_quotes(quotes, tier_map)
        log.info("[post_build] upsert_symbol_quotes complete — DB columns now populated")
    except Exception as exc:
        log.error(
            "[post_build] Phase 3 (upsert_symbol_quotes) FAILED — "
            "DB columns may be stale: %s",
            exc, exc_info=True,
        )


async def _background_build_and_upsert(registry, stream_symbols: list[str]) -> None:
    log.info("[registry] Background build starting (server already live)")
    try:
        count, raw_quotes = await registry.build()
        log.info("[registry] Background build complete: %d OCC symbols", count)
    except Exception as exc:
        log.error("[registry] Background build failed: %s", exc, exc_info=True)
        return
    try:
        await _post_build_upsert(registry, stream_symbols, raw_quotes=raw_quotes)
    except Exception as exc:
        log.error(
            "[registry] _post_build_upsert raised (tier assignment failed) — "
            "DB tier columns NOT updated this cycle: %s",
            exc, exc_info=True,
        )


async def _universe_refresh_loop():
    REFRESH_INTERVAL = 24 * 60 * 60
    while True:
        await asyncio.sleep(REFRESH_INTERVAL)
        log.info("[universe] Background refresh starting")
        try:
            stale = await universe_store.load_any_snapshot()
            symbols, source, stream_eligible_set = await load_universe(db_snapshot=stale)
            if source == "tradier_validated":
                saved = await universe_store.save_snapshot(symbols, source, stream_eligible_set)
                quotes = await _fetch_batch_quotes(symbols)
                tier_map: dict[str, int] = {}
                if saved and quotes:
                    registry = get_registry()
                    if registry:
                        log.info("[universe] Background refresh: rebuilding registry for OI roll-up")
                        await registry.build()
                        oi_map = registry.get_oi_map()
                        _stamp_oi(quotes, oi_map)
                        log.info(
                            "[universe] Background refresh: OI stamped on %d quotes "
                            "(%d tickers with oi>0)",
                            len(quotes), sum(1 for v in oi_map.values() if v > 0),
                        )
                    tier_map = await assign_tiers(quotes)
                    await universe_store.upsert_symbol_quotes(quotes, tier_map)

                registry = get_registry()
                if registry and tier_map:
                    registry.set_tier_map(tier_map)
                    _sync_accumulator_tier_map(tier_map)
                    log.info(
                        "[universe] Background refresh: registry tier_map updated "
                        "(T1=%d T2=%d T3=%d)",
                        sum(1 for t in tier_map.values() if t == 1),
                        sum(1 for t in tier_map.values() if t == 2),
                        sum(1 for t in tier_map.values() if t == 3),
                    )
                log.info(
                    "[universe] Background refresh complete: %d symbols saved=%s",
                    len(symbols), saved,
                )
            else:
                log.warning(
                    "[universe] Background refresh could not reach Tradier (source=%s)",
                    source,
                )
        except Exception as e:
            log.error("[universe] Background refresh failed (non-fatal): %s", e, exc_info=True)


_PREWARM_TIME = time(9, 15)


async def _registry_prewarm_loop() -> None:
    while True:
        now = datetime.now(_ET)

        if now.weekday() >= 5:
            log.info("[prewarm] Weekend — sleeping 1h before next check")
            await asyncio.sleep(3600)
            continue

        next_prewarm = now.replace(
            hour=_PREWARM_TIME.hour,
            minute=_PREWARM_TIME.minute,
            second=0,
            microsecond=0,
        )

        if now >= next_prewarm:
            next_prewarm += timedelta(days=1)
            while next_prewarm.weekday() >= 5:
                next_prewarm += timedelta(days=1)

        sleep_secs = (next_prewarm - now).total_seconds()
        log.info(
            "[prewarm] Registry pre-warm scheduled in %.1f min (at %s ET)",
            sleep_secs / 60,
            next_prewarm.strftime("%Y-%m-%d %H:%M"),
        )
        await asyncio.sleep(sleep_secs)

        invalidate_vol_oi_cache()
        log.info("[prewarm] vol/OI cache invalidated ahead of market open")

        log.info("[prewarm] Building OCC registry ahead of market open...")
        try:
            registry = get_registry()
            if registry is None:
                log.warning("[prewarm] Registry not initialised — skipping pre-warm")
                continue
            count, _ = await registry.build()
            log.info(
                "[prewarm] Registry warm: %d OCC contracts ready for market open",
                count,
            )
        except Exception as exc:
            log.error("[prewarm] Registry pre-warm failed (non-fatal): %s", exc, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting Cipher backend...")

    # FLAW3-POOL: Initialise the shared httpx connection pool FIRST, before any
    # code path that calls Tradier. All downstream callers (_client()) will reuse
    # the 30-connection pool rather than opening a fresh TCP socket per call.
    init_http_client()

    # Step 0 [ING-010]: Load tier gate config into memory before any service
    # reads gate values.
    log.info("[gate_config] Loading tier gate configuration from DB...")
    await gate_config_store.load()
    log.info(
        "[gate_config] Gate config ready (epoch=%d, loaded=%s)",
        gate_config_store.epoch,
        gate_config_store.epoch > 0,
    )

    # Step 1 [RC-3]: Warn on any missing ingestion config rows in DB.
    await validate_ingestion_config()

    # Step 2 [RENDER-STARTUP-HANG]: Fast DB-only universe resolution.
    stream_symbols, tier_map, snapshot_id, needs_universe_refresh = (
        await _resolve_startup_universe_fast()
    )
    log.info(
        "[universe] Fast resolve done: %d symbols, snapshot_id=%r, needs_refresh=%s",
        len(stream_symbols), snapshot_id, needs_universe_refresh,
    )

    # Step 3: Init in-memory symbol registry.
    registry = init_registry(watchlist=stream_symbols, tier_map=tier_map)
    log.info(
        "[registry] Initialised with %d stream symbols, %d tiers mapped",
        len(stream_symbols), len(tier_map),
    )

    # Step 4: Seed OCC chains from DB (P1 fallback — no Tradier call).
    _db_count = 0
    if snapshot_id:
        _db_count = await registry.load_from_db(snapshot_id)
        log.info(
            "[registry] DB chain seed: %d OCC contracts pre-loaded "
            "(is_ready=%s — waiting for build() to complete)",
            _db_count, registry.is_ready(),
        )

    log.info(
        "[universe] Step 2a — registry seed complete: "
        "occ_symbols=%d db_seeded=%d is_ready=%s path=%s",
        registry.size(),
        _db_count,
        registry.is_ready(),
        "warm" if _db_count > 0 else ("cold (no snapshot)" if not snapshot_id else "cold (empty seed)"),
    )

    log.info(
        "[registry] Server starting (is_ready=%s, %d OCC contracts seeded). "
        "Stream workers will spawn only after background build() sets _build_complete.",
        registry.is_ready(), registry.size(),
    )

    registry_refresh_task = asyncio.create_task(registry.refresh_loop())
    prewarm_task          = asyncio.create_task(_registry_prewarm_loop())
    stream_task           = asyncio.create_task(
        stream_options_flow(stream_symbols, registry=registry)
    )
    db_write_task         = asyncio.create_task(start_flow_writer())
    signal_write_task     = asyncio.create_task(start_signal_writer())
    refresh_task          = asyncio.create_task(_universe_refresh_loop())
    build_task            = asyncio.create_task(
        _background_build_and_upsert(registry, stream_symbols)
    )
    lookback_task         = asyncio.create_task(start_lookback_worker())

    universe_resolve_task = (
        asyncio.create_task(_background_universe_resolve(registry))
        if needs_universe_refresh
        else None
    )
    if universe_resolve_task:
        log.info(
            "[universe] Cache miss at startup — background universe resolve task spawned"
        )

    def _get_tracked_tickers() -> list:
        reg = get_registry()
        if reg is not None and hasattr(reg, "_registry") and reg._registry:
            return list({v.ticker for v in reg._registry.values()})
        return list(stream_symbols)

    chain_refresh_task    = asyncio.create_task(
        start_chain_refresh_worker(
            get_tracked_symbols=_get_tracked_tickers,
            fetch_chain_fn=_fetch_tradier_chain,
        )
    )

    self_ping_task        = asyncio.create_task(_self_ping_worker())

    yield

    # STREAM-5: cancel stream first so Tradier HTTP connections close cleanly.
    log.info("[shutdown] Closing Tradier stream connections first...")
    stream_task.cancel()
    lookback_task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(stream_task), timeout=5.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    log.info("[shutdown] Stream task stopped — Tradier session quota released")

    build_task.cancel()
    refresh_task.cancel()
    prewarm_task.cancel()
    registry_refresh_task.cancel()
    db_write_task.cancel()
    signal_write_task.cancel()
    chain_refresh_task.cancel()
    self_ping_task.cancel()
    if universe_resolve_task:
        universe_resolve_task.cancel()

    shutdown_tasks = [
        build_task,
        db_write_task,
        signal_write_task,
        refresh_task,
        registry_refresh_task,
        prewarm_task,
        lookback_task,
        chain_refresh_task,
        self_ping_task,
    ]
    if universe_resolve_task:
        shutdown_tasks.append(universe_resolve_task)

    for task in shutdown_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass

    # FLAW3-POOL: close the shared httpx pool after all tasks are done.
    await close_http_client()
    log.info("Cipher backend stopped.")


app = FastAPI(
    title       = "Cipher API",
    description = "Institutional options flow intelligence platform.",
    version     = "1.0.0",
    lifespan    = lifespan,
)

_explicit_origins = settings.origins
_explicit_patterns = [
    re.escape(o) for o in _explicit_origins if o != "*"
]
_origin_pattern = "|".join(filter(None, [
    r"https://[a-zA-Z0-9\-]+\.vercel\.app",
    r"http://localhost:(3000|3001)",
    r"http://127\.0\.0\.1:3000",
] + _explicit_patterns))

log.info("CORS allow_origin_regex: %s", _origin_pattern)

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex = _origin_pattern,
    allow_credentials  = True,
    allow_methods      = ["*"],
    allow_headers      = ["*"],
    expose_headers     = ["*"],
)

app.include_router(auth.router)
app.include_router(flow.router)
app.include_router(simulation.router)
app.include_router(ws.router)
app.include_router(smart_signals.router)
app.include_router(history.router)
app.include_router(admin.router)
app.include_router(health.router)
app.include_router(ingestion_config_router.router)
app.include_router(signal_config_router.router)


@app.get("/stream/stats", tags=["health"], include_in_schema=False)
async def stream_stats_health_alias():
    """
    STATS-STREAM-ALIAS: Returns full stream funnel stats for ops debugging.
    Identical payload to GET /stats. include_in_schema=False (internal only).
    """
    return JSONResponse(content=get_stream_stats())


@app.get("/stats", tags=["health"])
async def get_stats_endpoint(user=Depends(get_current_user)):
    return JSONResponse(content=get_stream_stats())


@app.get("/config", tags=["health"])
async def config_endpoint(cfg=Depends(get_config)):
    return JSONResponse(content=cfg)
