"""
Cipher Backend — FastAPI entry point

Startup sequence:
  0. gate_config_store.load()          — load tier gate config from DB into memory  [ING-010]
  1. validate_ingestion_config()       — warn on missing ingestion config rows  [RC-3]
  2. _resolve_startup_universe_fast()  — DB-only path: fresh snapshot HIT → done;
                                         MISS → seed with stale snapshot and schedule
                                         background refresh  [RENDER-STARTUP-HANG]
  3. init_registry()                   — in-memory init (instant)
  4. registry.load_from_db()           — seed OCC chains from DB via P1 fallback
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
     i. _chain_refresh_after_build()   — SEQ-002: awaits _registry_build_done Event
                                         THEN starts 5-min chain cache refresh loop
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
  SEQ-001 (main)      — _chain_refresh_after_build() wrapper introduced.
                        chain_refresh_worker was previously launched in parallel with
                        _background_build_and_upsert(), causing it to fire Tradier
                        chain API requests for all 3900 tickers while the OCC symbol
                        map was still being populated. This produced HTTP 400 storms
                        and incomplete chain data.
                        Fix: poll registry.is_ready() every 5 s (max 30 min) before
                        delegating to start_chain_refresh_worker(). The two fetch
                        paths are now strictly serialized:
                          symbol_registry.build() completes → chain_refresh starts.
                        All other tasks (stream, db_write, signal_write, etc.) are
                        unaffected and continue to launch in parallel as before.
  SEQ-002 (main)      — Replace SEQ-001 polling with asyncio.Event (_registry_build_done).
                        Root cause of SEQ-001 failure: registry.is_ready() fires as soon
                        as _build_complete is set inside build(), which happens before
                        _post_build_upsert() completes. On warm restarts the registry is
                        pre-seeded from DB (7275 contracts) and _build_complete can be set
                        for a partial incremental build — earlier than intended.
                        Fix: _registry_build_done = asyncio.Event() created in lifespan().
                          _background_build_and_upsert() sets it in a finally block after
                          both build() and _post_build_upsert() have finished (or failed).
                          _chain_refresh_after_build() awaits the event with a 30-min
                          timeout safety valve, then delegates to start_chain_refresh_worker().
                        The event fires exactly once, guaranteed, even on build failure or
                        CancelledError — chain_refresh is never permanently blocked.
                        Zero polling, zero race conditions, zero timing ambiguity.
  SEQ-002-FIX (main)  — Correct the event-set placement in _background_build_and_upsert.
                        The previous implementation set build_done_event inside the
                        finally block of the try/except around registry.build() only —
                        meaning the event fired BEFORE _post_build_upsert() ran. This
                        re-introduced Tradier quota contention: chain_refresh could
                        unblock and start fetching chains while _post_build_upsert()
                        was still making its own Tradier calls (assign_tiers,
                        upsert_symbol_quotes).
                        Fix: single outer try/finally wraps BOTH build() and
                        _post_build_upsert(). The event is set only in the outer
                        finally — guaranteed to fire whether either phase succeeds,
                        fails, or the task is cancelled. An inner try/except still
                        catches build() failures and returns early (skipping upsert)
                        while preserving the outer finally guarantee.
  PREWARM-RACE (main) — _registry_prewarm_loop() and _universe_refresh_loop() both
                        called registry.build() directly with no guard on the initial
                        build completing first.
                        _registry_prewarm_loop(): if the process restarts between
                        ~9:00-9:15 AM ET, sleep_secs is near-zero and prewarm fires
                        immediately. It acquires _build_lock behind the still-running
                        _background_build_and_upsert, then starts a second full build
                        when the lock releases — now chain_refresh AND a second build
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
                        stream_symbols list from load_universe() but never applied it —
                        stream_task kept running against the stale seed symbol list
                        (often empty or very small) for the entire trading session.
                        Fix: _background_universe_resolve() accepts two mutable
                        single-element wrappers:
                          stream_task_ref[0]          — the active asyncio.Task
                          stream_symbols_container    — the list[str] used by
                                                        _get_tracked_tickers() closure
                        After tier_map is patched, if stream_symbols differ from the
                        seed, stream_task is cancelled + awaited (5 s grace), the
                        container is updated in-place, and a new stream_task is created
                        and written back into stream_task_ref[0].
                        lifespan() passes [stream_task] and stream_symbols as the
                        wrappers. Shutdown reads stream_task_ref[0] so it always
                        cancels the active task regardless of replacement.
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
# ING-008: Tradier chain fetch helper for the background refresh worker.
#
# Calls GET /v1/markets/options/chains?symbol=X&greeks=false.
# Returns a list of contract dicts, each containing at minimum:
#   {"symbol": <occ_str>, "volume": int, "open_interest": int}
# Returns [] on any error — one bad symbol must never abort the refresh cycle.
# ---------------------------------------------------------------------------
async def _fetch_tradier_chain(symbol: str) -> list:
    """
    ING-008: Fetch the current options chain for `symbol` from Tradier.

    Used exclusively b