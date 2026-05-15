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

    Used exclusively by start_chain_refresh_worker() as the fetch_chain_fn
    callable.  Returns a flat list of option contract dicts.  On any
    network or API error returns [] so the worker continues to the next
    symbol without aborting the refresh cycle.

    HOTFIX-CHAIN-HOURS: Returns [] immediately outside market hours
    (Mon-Fri 9:15 AM – 4:30 PM ET) — Tradier returns HTTP 400 when markets
    are closed, so there is nothing useful to fetch.

    API: GET /v1/markets/options/chains?symbol=AAPL&greeks=false
    Each returned dict includes at minimum:
      - "symbol"        : str  (21-char OCC symbol)
      - "volume"        : int  (today's volume for this contract)
      - "open_interest" : int
    """
    # HOTFIX-CHAIN-HOURS: skip entirely outside market hours.
    if not _is_market_hours():
        log.debug("[chain_refresh] %s — skipped (market closed)", symbol)
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
            # Single-contract response (rare edge case)
            option_list = [option_list]
        return option_list
    except Exception as exc:
        log.warning(
            "[chain_refresh] _fetch_tradier_chain(%s) error: %s",
            symbol, exc,
        )
        return []


# ---------------------------------------------------------------------------
# SEQ-002 / SEQ-002-FIX: Sequenced chain refresh — waits for registry build
# AND post_build_upsert via asyncio.Event.
#
# SEQ-001 polled registry.is_ready() every 5 s, but is_ready() fires as soon
# as _build_complete is set inside build() — before _post_build_upsert() runs.
#
# SEQ-002 introduced asyncio.Event but the original implementation set the
# event inside the finally block of the try/except around registry.build()
# only — meaning the event fired BEFORE _post_build_upsert() ran. This
# re-introduced Tradier quota contention: chain_refresh could unblock while
# _post_build_upsert() was still making its own Tradier/DB calls.
#
# SEQ-002-FIX: _background_build_and_upsert() now wraps BOTH build() and
# _post_build_upsert() in a single outer try/finally. The event is set only
# in the outer finally — guaranteed to fire whether either phase succeeds,
# fails, or the task is cancelled. An inner try/except still catches build()
# failures and returns early (skipping upsert) while the outer finally
# guarantee is preserved.
# ---------------------------------------------------------------------------
async def _chain_refresh_after_build(
    get_tracked_symbols,
    fetch_chain_fn,
    build_done_event: asyncio.Event,
    timeout: float = 1800.0,
) -> None:
    """
    SEQ-002: Gate chain_refresh strictly behind registry build + upsert completion.

    Awaits `build_done_event` which is set by _background_build_and_upsert()
    in its outer finally block — guaranteed to fire after BOTH registry.build()
    AND _post_build_upsert() have completed (or failed/cancelled). Once the
    event fires, delegates to start_chain_refresh_worker() which runs its
    internal loop forever.

    `timeout` (default 30 min) is a last-resort safety valve: if
    build_done_event is never set (e.g. catastrophic shutdown before the
    finally block runs), chain_refresh starts anyway so the vol/OI cache
    is not permanently empty. A warning is logged in that case.
    """
    try:
        await asyncio.wait_for(build_done_event.wait(), timeout=timeout)
        log.info(
            "[chain_refresh] SEQ-002: build_done_event received — "
            "starting chain refresh worker (symbol_registry build + upsert fully complete)"
        )
    except asyncio.TimeoutError:
        log.warning(
            "[chain_refresh] SEQ-002: build_done_event not set after %.0f s (timeout) — "
            "starting chain refresh worker anyway to avoid permanent cache miss",
            timeout,
        )

    # Delegate to the real worker — runs its loop forever.
    await start_chain_refresh_worker(
        get_tracked_symbols=get_tracked_symbols,
        fetch_chain_fn=fetch_chain_fn,
    )


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
        stream_symbols = [s["symbol"] for s in fresh if s.get("stream_eligible", True)]
        snapshot_id    = fresh[0].get("snapshot_id", "") if fresh else ""
        return stream_symbols, tier_map, snapshot_id, False  # needs_refresh=False

    # Cache MISS — fall back to the most-recent stale snapshot as a seed.
    log.info("[universe] Step 2a MISS: no fresh snapshot — loading most-recent stale snapshot as seed")
    stale = await universe_store.load_any_snapshot()
    if stale:
        log.info(
            "[universe] Step 2a STALE: seeding from %d stale symbols — "
            "background refresh will produce the authoritative list",
            len(stale),
        )
        stale_symbols = [s["symbol"] for s in stale if s.get("stream_eligible", True)]
        snapshot_id   = stale[0].get("snapshot_id", "") if stale else ""
        return stale_symbols, {}, snapshot_id, True  # needs_refresh=True

    log.warning(
        "[universe] Step 2a: no snapshot in DB at all — "
        "starting with empty symbol list, background refresh will populate"
    )
    return [], {}, "", True  # needs_refresh=True


async def _background_universe_resolve(
    registry,
    stream_task_ref: list,
    stream_symbols_container: list,
) -> None:
    """
    RENDER-STARTUP-HANG / STREAM-RELOAD: slow universe refresh that runs post-yield.

    Called only when _resolve_startup_universe_fast() returned needs_refresh=True
    (i.e. no fresh 24h snapshot existed at startup).

    Runs the full load_universe() pipeline (CBOE + Tradier validate + screen),
    saves the snapshot to DB, assigns tiers, then live-patches:
      - registry.set_tier_map()
      - tradier_stream.accumulator.set_tier_map()  (ING-010-ACC)
      - universe_store.upsert_symbol_quotes()
      - stream_task relaunch with fresh symbol list (STREAM-RELOAD)

    stream_task_ref[0]       — mutable wrapper holding the active asyncio.Task.
    stream_symbols_container — the list[str] captured by _get_tracked_tickers();
                               updated in-place so the closure sees the new symbols.

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

    # Live-patch the running registry and accumulator if we got fresh tiers.
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

    # STREAM-RELOAD: relaunch stream_task with the fresh symbol list if it changed.
    if set(stream_symbols) != set(stream_symbols_container):
        log.info(
            "[universe] STREAM-RELOAD: symbol list changed (%d seed → %d fresh) — "
            "cancelling current stream_task and relaunching",
            len(stream_symbols_container), len(stream_symbols),
        )
        old_task = stream_task_ref[0]
        old_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(old_task), timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

        # Update the shared container in-place so _get_tracked_tickers() picks it up.
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
            "[universe] STREAM-RELOAD: symbol list unchanged (%d symbols) — "
            "no stream restart needed",
            len(stream_symbols),
        )

    log.info(
        "[universe] Background resolve COMPLETE: %d stream symbols (source=%s, universe=%d)",
        len(stream_symbols), source, len(symbols),
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
            log.warning("[universe] ING-010-ACC: tradier_stream.accumulator not available — skipping")
    except Exception as exc:
        log.error("[universe] ING-010-ACC: set_tier_map on accumulator failed: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# Post-build upsert helpers
# ---------------------------------------------------------------------------

async def _post_build_upsert(registry, raw_quotes: list) -> None:
    """
    M-3: Two-phase post-build upsert — assign_tiers() then upsert_symbol_quotes().

    Phase 1 (assign_tiers): Reads vol/mkt-cap data from raw_quotes and assigns
    each symbol to a tier.  If this fails, the error is re-raised so the caller
    knows the upsert is incomplete and can log/metric accordingly.

    Phase 2 (upsert_symbol_quotes): Persists the quotes + tiers to DB.  Also
    guarded — but a failure here does not re-raise, since the registry is
    already live and a DB write failure is non-fatal at runtime.

    H1: raw_quotes is passed in from build() rather than re-fetched, so we do
    not make a second _fetch_batch_quotes() call on warm-restart.
    """
    if not raw_quotes:
        log.warning("[post_build] raw_quotes is empty — skipping upsert")
        return

    # Phase 1: assign tiers (raises on failure — M-3)
    tier_map = await assign_tiers(raw_quotes)
    registry.set_tier_map(tier_map)
    _sync_accumulator_tier_map(tier_map)  # ING-010-ACC
    log.info(
        "[post_build] Tier assignment complete — T1=%d T2=%d T3=%d",
        sum(1 for t in tier_map.values() if t == 1),
        sum(1 for t in tier_map.values() if t == 2),
        sum(1 for t in tier_map.values() if t == 3),
    )

    # Phase 2: persist to DB (non-fatal on failure)
    try:
        # Filter out symbols with null/zero volume to avoid polluting the DB.
        non_null = [q for q in raw_quotes if getattr(q, "volume", None)]
        log.info(
            "[post_build] upsert_symbol_quotes: %d total quotes, %d non-null volume",
            len(raw_quotes), len(non_null),
        )
        await universe_store.upsert_symbol_quotes(non_null, tier_map)
        log.info("[post_build] upsert_symbol_quotes complete")
    except Exception as exc:
        log.error("[post_build] upsert_symbol_quotes failed: %s", exc, exc_info=True)


async def _background_build_and_upsert(
    registry,
    stream_symbols: list[str],
    build_done_event: asyncio.Event,
) -> None:
    """
    P2/SEQ-002-FIX: Background OCC build + post-build upsert.

    Single outer try/finally wraps BOTH registry.build() AND _post_build_upsert()
    so build_done_event is set only after both phases complete (or fail).

    An inner try/except around registry.build() catches build failures and
    returns early (skipping upsert) while the outer finally still fires the
    event — chain_refresh is never permanently blocked even on build failure.
    """
    try:
        # Inner guard: catch build() failures without swallowing the outer finally.
        try:
            log.info("[build] Starting background OCC registry build...")
            raw_quotes = await registry.build()
            log.info(
                "[build] registry.build() complete — %d contracts loaded",
                len(registry._registry) if hasattr(registry, "_registry") else 0,
            )
        except Exception as exc:
            log.error("[build] registry.build() failed: %s", exc, exc_info=True)
            return  # outer finally will still set the event

        # Phase 2: post-build upsert (assign_tiers + upsert_symbol_quotes)
        try:
            await _post_build_upsert(registry, raw_quotes or [])
        except Exception as exc:
            log.error("[build] _post_build_upsert failed: %s", exc, exc_info=True)
            # Non-fatal — registry is live even if upsert fails.

    finally:
        # SEQ-002-FIX: fire AFTER both build() and _post_build_upsert() finish.
        build_done_event.set()
        log.info("[build] _registry_build_done event set — chain_refresh unblocked")


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------

async def _registry_prewarm_loop(build_done_event: asyncio.Event) -> None:
    """
    Daily 9:15 AM ET registry pre-warm.

    PREWARM-RACE fix: skip registry.build() if registry.epoch == 0 — the
    initial build has not completed yet.  Prewarm is for warming the cache
    before the next trading day open, not a substitute for startup build.
    """
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
            log.warning("[prewarm] Registry not initialised — skipping pre-warm")
            continue

        # PREWARM-RACE: skip if initial build not yet complete.
        if getattr(registry, "epoch", 0) == 0:
            log.warning(
                "[prewarm] Skipping pre-warm — registry.epoch==0 (initial build not complete)"
            )
            continue

        log.info("[prewarm] Starting 9:15 AM ET pre-warm build")
        try:
            invalidate_vol_oi_cache()   # HOTFIX-CHAIN-HOURS: clear yesterday's vol before build
            raw_quotes = await registry.build()
            await _post_build_upsert(registry, raw_quotes or [])
            log.info("[prewarm] Pre-warm build complete")
        except Exception as exc:
            log.error("[prewarm] Pre-warm build failed: %s", exc, exc_info=True)


async def _universe_refresh_loop(build_done_event: asyncio.Event) -> None:
    """
    24-hour universe snapshot refresh.

    PREWARM-RACE fix: removed the direct registry.build() call — OCC chains
    are kept fresh by registry.refresh_loop() (every 30 min).  This loop
    only refreshes the universe snapshot (CBOE + Tradier screen) every 24 h.
    """
    INTERVAL = 24 * 3600
    while True:
        await asyncio.sleep(INTERVAL)
        log.info("[universe_refresh] 24h interval reached — refreshing universe snapshot")
        try:
            stale = await universe_store.load_any_snapshot()
            symbols, source, stream_eligible_set = await load_universe(db_snapshot=stale)
            if source == "tradier_validated":
                await universe_store.save_snapshot(symbols, source, stream_eligible_set)
                log.info(
                    "[universe_refresh] Snapshot refreshed — %d symbols (source=%s)",
                    len(symbols), source,
                )

                # Re-read OI from the live registry (kept fresh by refresh_loop).
                registry = get_registry()
                if registry is not None:
                    oi_map = registry.get_oi_map() if hasattr(registry, "get_oi_map") else {}
                    log.info(
                        "[universe_refresh] OI map from live registry: %d entries",
                        len(oi_map),
                    )
            else:
                log.warning(
                    "[universe_refresh] source=%s — snapshot NOT updated", source
                )
        except Exception as exc:
            log.error("[universe_refresh] refresh failed: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Step 0: load gate config ──────────────────────────────────────────
    log.info("[startup] Step 0: loading gate config from DB (ING-010)")
    try:
        await gate_config_store.load()
        log.info("[startup] Step 0: gate config loaded")
    except Exception as exc:
        log.error("[startup] Step 0: gate_config_store.load() failed: %s", exc, exc_info=True)

    # ── Step 1: validate ingestion config ─────────────────────────────────
    log.info("[startup] Step 1: validating ingestion config (RC-3)")
    try:
        await validate_ingestion_config()
    except Exception as exc:
        log.warning("[startup] Step 1: validate_ingestion_config failed: %s", exc)

    # ── Step 2a: fast DB-only universe resolution ──────────────────────────
    log.info("[startup] Step 2a: resolving startup universe (DB-only fast path)")
    stream_symbols, tier_map, snapshot_id, needs_universe_refresh = (
        await _resolve_startup_universe_fast()
    )
    log.info(
        "[startup] Step 2a complete — %d stream symbols, needs_refresh=%s",
        len(stream_symbols), needs_universe_refresh,
    )

    # ── Step 3: init registry ─────────────────────────────────────────────
    log.info("[startup] Step 3: initialising symbol registry")
    registry = init_registry(watchlist=stream_symbols)
    if tier_map:
        registry.set_tier_map(tier_map)
        _sync_accumulator_tier_map(tier_map)  # ING-010-ACC

    # ── Step 4: seed OCC chains from DB ───────────────────────────────────
    log.info("[startup] Step 4: seeding OCC chains from DB (P1 fallback)")
    try:
        await registry.load_from_db()
        log.info(
            "[startup] Step 4: DB seed complete — %d contracts loaded",
            len(registry._registry) if hasattr(registry, "_registry") else 0,
        )
    except Exception as exc:
        log.error("[startup] Step 4: registry.load_from_db() failed: %s", exc, exc_info=True)

    # ── Step 5: yield — server is live ────────────────────────────────────
    log.info("[startup] Step 5: yielding — server is live (health probe will pass)")

    # ── Post-yield: launch background tasks ───────────────────────────────
    # PREWARM-RACE: also passed into _registry_prewarm_loop() and
    # _universe_refresh_loop() so they can guard their own registry.build()
    # calls (prewarm) or OI reads (universe refresh) behind initial build
    # completion.
    _registry_build_done = asyncio.Event()

    registry_refresh_task = asyncio.create_task(registry.refresh_loop())
    prewarm_task          = asyncio.create_task(_registry_prewarm_loop(_registry_build_done))
    stream_task           = asyncio.create_task(
        stream_options_flow(stream_symbols, registry=registry)
    )
    # STREAM-RELOAD: mutable wrapper so _background_universe_resolve() can
    # cancel + replace the task, and shutdown always reads the active task.
    stream_task_ref = [stream_task]
    db_write_task         = asyncio.create_task(start_flow_writer())
    signal_write_task     = asyncio.create_task(start_signal_writer())
    refresh_task          = asyncio.create_task(_universe_refresh_loop(_registry_build_done))
    build_task            = asyncio.create_task(
        _background_build_and_upsert(registry, stream_symbols, _registry_build_done)
    )
    # MAIN-FIX-001: start_lookback_worker() was refactored (FS-HANG fix) to
    # fetch its own accumulator internally via get_accumulator() — it takes 0
    # positional arguments. The previous call site passed registry.accumulator,
    # causing TypeError at every TestClient startup.
    lookback_task         = asyncio.create_task(start_lookback_worker())

    # RENDER-STARTUP-HANG: spawn background universe refresh only on cache MISS.
    # On HIT this is None and is excluded from the shutdown cancel list.
    universe_resolve_task = (
        asyncio.create_task(_background_universe_resolve(
            registry,
            stream_task_ref=stream_task_ref,
            stream_symbols_container=stream_symbols,
        ))
        if needs_universe_refresh
        else None
    )
    if universe_resolve_task:
        log.info(
            "[universe] Cache miss at startup — background universe resolve task spawned"
        )

    # SEQ-002-FIX: chain_refresh_after_build — strictly serialized behind
    # BOTH build_task and _post_build_upsert.
    #
    # _chain_refresh_after_build() awaits _registry_build_done (asyncio.Event)
    # which is now set by _background_build_and_upsert() in its OUTER finally
    # block — after BOTH registry.build() AND _post_build_upsert() have
    # completed (or failed). This guarantees zero Tradier chain-fetch
    # contention with either the registry build or the post-build upsert.
    #
    # get_tracked_symbols returns the unique set of underlying tickers from the
    # live registry (not OCC symbols — one chain call per ticker covers all
    # strikes/expiries). Falls back to stream_symbols if registry is not ready.
    def _get_tracked_tickers() -> list:
        reg = get_registry()
        if reg is not None and hasattr(reg, "_registry") and reg._registry:
            # Unique underlying tickers — one chain call covers all strikes/expiries.
            return list({v.ticker for v in reg._registry.values()})
        # Fallback: use the startup stream_symbols list.
        return list(stream_symbols)

    log.info(
        "[chain_refresh] SEQ-002: chain refresh worker will start after "
        "build_done_event fires (registry build + post_build_upsert complete)"
    )
    chain_refresh_task = asyncio.create_task(
        _chain_refresh_after_build(
            get_tracked_symbols=_get_tracked_tickers,
            fetch_chain_fn=_fetch_tradier_chain,
            build_done_event=_registry_build_done,
        )
    )

    # RENDER-KEEPALIVE: prevent free-tier idle spin-down (15-min threshold).
    # Pings GET /health every 5 minutes. No-ops when RENDER_EXTERNAL_URL is unset.
    # HOTFIX-KEEPALIVE-001: hardened — fresh client per cycle, fail-streak logging.
    self_ping_task        = asyncio.create_task(_self_ping_worker())

    yield

    log.info("[shutdown] Closing Tradier stream connections first...")
    # STREAM-RELOAD: cancel the *active* task (may have been replaced).
    stream_task_ref[0].cancel()
    lookback_task.cancel()
    try:
        await asyncio.wait_for(asyncio.shield(stream_task_ref[0]), timeout=5.0)
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

    tasks_to_await = [
        build_task, refresh_task, prewarm_task,
        registry_refresh_task, db_write_task, signal_write_task,
        chain_refresh_task, self_ping_task,
    ]
    if universe_resolve_task:
        tasks_to_await.append(universe_resolve_task)

    results = await asyncio.gather(*tasks_to_await, return_exceptions=True)
    for task, result in zip(tasks_to_await, results):
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            log.warning("[shutdown] Task %s raised: %s", task.get_name(), result)

    log.info("[shutdown] All background tasks stopped — shutdown complete")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

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
