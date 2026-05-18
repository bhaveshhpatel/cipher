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
  SEQ-002-STAGGER (main) — 60 s sleep between build_done_event and chain_refresh start.
                        After build+upsert complete, Tradier's 120 req/min window can
                        be close to exhausted. The 60 s stagger gives the rate-limit
                        window a full reset before chain refresh adds ~50 req/min load.
                        Stream is already live during this window; _vol_oi_cache from
                        the previous session's final refresh remains valid.
  FIX-P1-SKIP-BUILD (main) — skip full Tradier build when registry is already seeded
                        with >= _P1_MIN_CONTRACTS (10 000) contracts from DB at epoch 0.
                        Full chain-pull takes 8-12 min and is unnecessary on warm
                        restarts where the DB snapshot is fresh. refresh_loop() handles
                        the background OCC sync. Stream starts immediately.
  FIX-P1-SKIP-TIERS (main) — tier assignment on the skip path.
                        When FIX-P1-SKIP-BUILD fires, _fetch_batch_quotes() is called
                        on stream_symbols to get a fresh SymbolQuote list, then
                        assign_tiers() -> registry.set_tier_map() ->
                        _sync_accumulator_tier_map(). If the quote fetch fails or
                        returns empty, log a warning and continue with DB-loaded tiers.
  FIX-UPSERT-ARG-ORDER (main) — upsert_symbol_quotes(snapshot_id, enriched_quotes).
                        Previous call sites passed (quote_rows, tier_map) — wrong order.
                        snapshot_id now fetched via get_latest_snapshot_id() before each
                        call so the correct UUID is always used.
  SAVE-SNAPSHOT-SET (main) — universe_store.save_snapshot() called with symbol_rows kwarg
                        (list of dicts with snapshot_id, symbol, stream_eligible) instead
                        of positional stream_eligible_set. Matches the updated signature
                        in universe_store.py after the schema migration.
  STREAM-RELOAD (main) — stream_task relaunched with fresh symbols after background
                        universe resolve completes and symbol list changes.
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import (
    admin,
    auth,
    flows,
    ingestion_config,
    signal_config,
    signals,
    snapshots,
    symbols,
    tiers,
    universe,
)
from services.chain_refresh import start_chain_refresh_worker
from services.flow_writer import start_flow_writer
from services.gate_config_store import store as gate_config_store
from services.ingestion_config_service import validate_ingestion_config
from services.lookback_worker import start_lookback_worker
from services.signal_store import assign_tiers
from services.signal_writer import start_signal_writer
from services.symbol_quotes import _fetch_batch_quotes
from services.symbol_registry import init_registry, registry
from services.tradier_stream import (
    accumulator as _stream_accumulator,
    stream_options_flow,
)
from services.universe_store import store as universe_store

log = logging.getLogger("cipher.main")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")

_P1_MIN_CONTRACTS = 10_000
_registry_build_done: asyncio.Event | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sync_accumulator_tier_map(tier_map: dict) -> None:
    try:
        _stream_accumulator.set_tier_map(tier_map)
        log.info("[tier] accumulator tier_map synced (%d symbols)", len(tier_map))
    except Exception as exc:  # noqa: BLE001
        log.warning("[tier] accumulator.set_tier_map failed: %s", exc)


def _enrich_quotes_with_tier(quotes: list, tier_map: dict) -> list:
    for q in quotes:
        try:
            q.tier = tier_map.get(getattr(q, "symbol", ""), 3)
        except Exception:  # noqa: BLE001
            pass
    return quotes


def _filter_symbol_quotes(raw: list) -> list:
    filtered = [q for q in raw if hasattr(q, "average_volume")]
    dropped = len(raw) - len(filtered)
    if dropped:
        log.warning(
            "[build] BUILD-QUOTE-TYPE: dropped %d non-SymbolQuote items from raw_quotes",
            dropped,
        )
    return filtered


# ---------------------------------------------------------------------------
# Post-build upsert
# ---------------------------------------------------------------------------

async def _post_build_upsert(raw_quotes: list) -> None:
    quotes = _filter_symbol_quotes(raw_quotes)
    if not quotes:
        log.warning(
            "[build] _post_build_upsert: no SymbolQuote objects after filter — "
            "skipping tier assign + upsert"
        )
        return

    tier_map = assign_tiers(quotes)
    registry.set_tier_map(tier_map)
    _sync_accumulator_tier_map(tier_map)
    log.info(
        "[build] tier_map set: T1=%d T2=%d T3=%d",
        sum(1 for v in tier_map.values() if v == 1),
        sum(1 for v in tier_map.values() if v == 2),
        sum(1 for v in tier_map.values() if v == 3),
    )

    try:
        snapshot_id = await universe_store.get_latest_snapshot_id()
    except Exception as exc:  # noqa: BLE001
        log.warning("[build] get_latest_snapshot_id failed: %s — skipping upsert", exc)
        return

    enriched = _enrich_quotes_with_tier(quotes, tier_map)
    try:
        await universe_store.upsert_symbol_quotes(snapshot_id, enriched)
        log.info(
            "[build] upsert_symbol_quotes complete (snapshot=%s, rows=%d)",
            snapshot_id,
            len(enriched),
        )
    except Exception as exc:  # noqa: BLE001
        log.error("[build] upsert_symbol_quotes failed: %s", exc)


# ---------------------------------------------------------------------------
# Background build task
# ---------------------------------------------------------------------------

async def _background_build_and_upsert(
    build_done_event: asyncio.Event,
    stream_symbols: list,
) -> None:
    try:
        seeded_count = len(registry._registry)  # noqa: SLF001
        if seeded_count >= _P1_MIN_CONTRACTS and registry.epoch == 0:
            log.info(
                "[build] FIX-P1-SKIP-BUILD: registry seeded with %d contracts (epoch=0) "
                "— skipping full Tradier build, streaming immediately.",
                seeded_count,
            )
            registry._build_complete = True  # noqa: SLF001
            registry.epoch = 1

            # FIX-P1-SKIP-TIERS: tier assignment on skip path
            pre_quotes: list = []
            if stream_symbols:
                try:
                    pre_quotes = await _fetch_batch_quotes(stream_symbols)
                    if pre_quotes:
                        tier_map_pre = assign_tiers(pre_quotes)
                        registry.set_tier_map(tier_map_pre)
                        _sync_accumulator_tier_map(tier_map_pre)
                        log.info("[build] FIX-P1-SKIP-TIERS: pre-build tier_map set on skip path")
                    else:
                        log.warning(
                            "[build] FIX-P1-SKIP-TIERS: _fetch_batch_quotes returned empty "
                            "— tiers remain DB-loaded"
                        )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "[build] FIX-P1-SKIP-TIERS: pre-fetch failed: %s — tiers remain DB-loaded",
                        exc,
                    )

            await _post_build_upsert(pre_quotes)
            return

        try:
            raw_quotes = await registry.build()
            log.info("[build] registry.build() complete")
        except Exception as exc:  # noqa: BLE001
            log.error("[build] registry.build() failed: %s", exc)
            return

        await _post_build_upsert(raw_quotes)

    finally:
        build_done_event.set()
        log.info("[build] _registry_build_done event set")


# ---------------------------------------------------------------------------
# Chain refresh (SEQ-002)
# ---------------------------------------------------------------------------

async def _fetch_tradier_chain(symbol: str) -> list:
    from services.tradier_client import fetch_chain  # local import to avoid circular
    try:
        import zoneinfo
        et = zoneinfo.ZoneInfo("America/New_York")
        now_et = datetime.now(tz=et)
        weekday = now_et.weekday()
        if weekday >= 5:
            return []
        from datetime import time as dtime
        t = now_et.time()
        if t < dtime(9, 15) or t > dtime(16, 30):
            return []
    except Exception:  # noqa: BLE001
        pass
    try:
        return await fetch_chain(symbol)
    except Exception as exc:  # noqa: BLE001
        log.debug("[chain] _fetch_tradier_chain(%s) error: %s", symbol, exc)
        return []


async def _chain_refresh_after_build(build_done_event: asyncio.Event, stream_symbols: list) -> None:
    timeout = 30 * 60
    try:
        await asyncio.wait_for(build_done_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        log.warning(
            "[chain] SEQ-002: build_done_event timeout after %ds — starting chain refresh anyway",
            timeout,
        )

    log.info("[chain] SEQ-002-STAGGER: sleeping 60 s for Tradier quota recovery before chain refresh")
    await asyncio.sleep(60)

    def _get_tracked() -> list:
        return stream_symbols

    await start_chain_refresh_worker(
        get_tracked_symbols=_get_tracked,
        fetch_chain_fn=_fetch_tradier_chain,
    )


# ---------------------------------------------------------------------------
# Prewarm + universe refresh loops
# ---------------------------------------------------------------------------

async def _registry_prewarm_loop() -> None:
    import zoneinfo
    et = zoneinfo.ZoneInfo("America/New_York")
    while True:
        now = datetime.now(tz=et)
        target = now.replace(hour=9, minute=15, second=0, microsecond=0)
        if now >= target:
            import datetime as _dt
            target = target + _dt.timedelta(days=1)
        sleep_secs = (target - now).total_seconds()
        log.info("[prewarm] sleeping %.0f s until 9:15 AM ET", sleep_secs)
        await asyncio.sleep(sleep_secs)

        if registry.epoch == 0:
            log.info("[prewarm] PREWARM-RACE: initial build not yet complete — skipping prewarm build")
            continue

        from services.chain_refresh import invalidate_vol_oi_cache
        invalidate_vol_oi_cache()
        try:
            raw_quotes = await registry.build()
            log.info("[prewarm] prewarm build complete")
        except Exception as exc:  # noqa: BLE001
            log.error("[prewarm] prewarm build failed: %s", exc)


async def _universe_refresh_loop() -> None:
    while True:
        await asyncio.sleep(24 * 3600)
        log.info("[universe] 24h refresh tick — reading OI from live registry (no direct build call)")
        try:
            oi_map = registry.get_oi_map()
            log.info("[universe] OI map size: %d", len(oi_map))
        except Exception as exc:  # noqa: BLE001
            log.warning("[universe] get_oi_map failed: %s", exc)


# ---------------------------------------------------------------------------
# Background universe resolve (STREAM-RELOAD)
# ---------------------------------------------------------------------------

async def _background_universe_resolve(
    stream_task_ref: list,
    stream_symbols_container: list,
) -> None:
    from services.universe import load_universe
    try:
        symbols_result = await load_universe()
        if not symbols_result:
            log.warning("[universe] background resolve: load_universe() returned empty")
            return

        all_symbols = symbols_result.get("all", [])
        stream_eligible = symbols_result.get("stream_eligible", set())

        symbol_rows = [
            {
                "snapshot_id": None,
                "symbol": s,
                "stream_eligible": (s in stream_eligible),
            }
            for s in all_symbols
        ]
        snapshot_id = await universe_store.save_snapshot(
            all_symbols,
            source="tradier",
            symbol_rows=symbol_rows,
        )
        log.info(
            "[universe] background resolve: saved snapshot %s (%d symbols)",
            snapshot_id, len(all_symbols),
        )

        stream_syms = [s for s in all_symbols if s in stream_eligible]
        raw_quotes: list = []
        if stream_syms:
            try:
                raw_quotes = await _fetch_batch_quotes(stream_syms)
            except Exception as exc:  # noqa: BLE001
                log.warning("[universe] background resolve: _fetch_batch_quotes failed: %s", exc)

        if raw_quotes:
            tier_map = assign_tiers(raw_quotes)
            registry.set_tier_map(tier_map)
            _sync_accumulator_tier_map(tier_map)
            try:
                snap_id = await universe_store.get_latest_snapshot_id()
                enriched = _enrich_quotes_with_tier(raw_quotes, tier_map)
                await universe_store.upsert_symbol_quotes(snap_id, enriched)
                log.info(
                    "[universe] background resolve: upsert_symbol_quotes complete (%d rows)",
                    len(enriched),
                )
            except Exception as exc:  # noqa: BLE001
                log.error("[universe] background resolve: upsert failed: %s", exc)

        # STREAM-RELOAD: relaunch stream_task with fresh symbols if changed
        if set(stream_syms) != set(stream_symbols_container):
            log.info(
                "[universe] STREAM-RELOAD: symbol list changed (%d -> %d) — relaunching stream_task",
                len(stream_symbols_container), len(stream_syms),
            )
            old_task = stream_task_ref[0]
            if old_task and not old_task.done():
                old_task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(old_task), timeout=5)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

            stream_symbols_container.clear()
            stream_symbols_container.extend(stream_syms)
            new_task = asyncio.create_task(stream_options_flow(registry))
            stream_task_ref[0] = new_task
            log.info("[universe] STREAM-RELOAD: new stream_task created")

    except Exception as exc:  # noqa: BLE001
        log.error("[universe] background resolve failed: %s", exc)


# ---------------------------------------------------------------------------
# Self-ping keepalive (RENDER)
# ---------------------------------------------------------------------------

async def _self_ping_worker() -> None:
    base_url = os.getenv("RENDER_EXTERNAL_URL", "")
    if not base_url:
        log.info("[keepalive] RENDER_EXTERNAL_URL not set — self-ping disabled")
        return

    interval = 300
    fail_streak = 0
    log.info("[keepalive] self-ping started — interval=%ds target=%s/health", interval, base_url)

    while True:
        await asyncio.sleep(interval)
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{base_url}/health")
            if resp.status_code == 200:
                fail_streak = 0
                log.debug("[keepalive] ping OK")
            else:
                fail_streak += 1
                log.warning("[keepalive] ping returned %d (streak=%d)", resp.status_code, fail_streak)
        except Exception as exc:  # noqa: BLE001
            fail_streak += 1
            log.warning("[keepalive] ping failed: %s (streak=%d)", exc, fail_streak)
        if fail_streak >= 3:
            log.error(
                "[keepalive] %d consecutive ping failures — check RENDER_EXTERNAL_URL=%s",
                fail_streak, base_url,
            )


# ---------------------------------------------------------------------------
# Fast startup universe resolve (DB-only)
# ---------------------------------------------------------------------------

async def _resolve_startup_universe_fast() -> tuple:
    """DB-only fast path. Returns (all_symbols, stream_symbols, snapshot_id | None)."""
    try:
        result = await universe_store.load_fresh_snapshot()
        if result:
            all_syms = result.get("symbols", [])
            stream_syms = result.get("stream_eligible", all_syms)
            snapshot_id = result.get("snapshot_id") or None
            log.info(
                "[startup] DB snapshot HIT — %d symbols, %d stream-eligible, snapshot=%s",
                len(all_syms), len(stream_syms), snapshot_id,
            )
            return all_syms, stream_syms, snapshot_id
    except Exception as exc:  # noqa: BLE001
        log.warning("[startup] load_fresh_snapshot failed: %s — falling back", exc)

    try:
        result = await universe_store.load_latest_snapshot()
        if result:
            all_syms = result.get("symbols", [])
            stream_syms = result.get("stream_eligible", all_syms)
            snapshot_id = result.get("snapshot_id") or None
            log.info(
                "[startup] DB snapshot MISS — seeding from stale: %d symbols, snapshot=%s",
                len(all_syms), snapshot_id,
            )
            return all_syms, stream_syms, snapshot_id
    except Exception as exc:  # noqa: BLE001
        log.warning("[startup] load_latest_snapshot failed: %s", exc)

    log.warning("[startup] no DB snapshot available — starting with empty universe")
    return [], [], None


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    global _registry_build_done

    # Step 0
    try:
        await gate_config_store.load()
        log.info("[startup] gate_config_store loaded")
    except Exception as exc:  # noqa: BLE001
        log.error("[startup] gate_config_store.load() failed: %s", exc)

    # Step 1
    try:
        await validate_ingestion_config()
    except Exception as exc:  # noqa: BLE001
        log.warning("[startup] validate_ingestion_config failed: %s", exc)

    # Step 2
    all_symbols, stream_symbols, snapshot_id = await _resolve_startup_universe_fast()

    # Step 3
    init_registry()

    # Step 4 (FIX-LOAD-FROM-DB-ARG)
    try:
        await registry.load_from_db(snapshot_id)
        log.info("[startup] registry.load_from_db complete (snapshot=%s)", snapshot_id)
    except Exception as exc:  # noqa: BLE001
        log.error("[startup] registry.load_from_db failed: %s", exc)

    # Step 5: yield — server live
    _registry_build_done = asyncio.Event()
    stream_task = asyncio.create_task(stream_options_flow(registry))
    stream_task_ref = [stream_task]

    yield

    # Step 6: background tasks
    build_task = asyncio.create_task(
        _background_build_and_upsert(_registry_build_done, stream_symbols)
    )
    universe_resolve_task = asyncio.create_task(
        _background_universe_resolve(stream_task_ref, stream_symbols)
    )
    refresh_task = asyncio.create_task(registry.refresh_loop())
    prewarm_task = asyncio.create_task(_registry_prewarm_loop())
    flow_writer_task = asyncio.create_task(start_flow_writer())
    signal_writer_task = asyncio.create_task(start_signal_writer())
    universe_refresh_task = asyncio.create_task(_universe_refresh_loop())
    chain_task = asyncio.create_task(
        _chain_refresh_after_build(_registry_build_done, stream_symbols)
    )
    keepalive_task = asyncio.create_task(_self_ping_worker())
    gate_config_refresh_task = asyncio.create_task(
        gate_config_store.start_refresh_loop(300)
    )
    lookback_task = asyncio.create_task(start_lookback_worker())

    log.info("[startup] all background tasks launched")

    try:
        await asyncio.Future()
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass

    # Shutdown: cancel stream first (STREAM-5)
    current_stream_task = stream_task_ref[0]
    if current_stream_task and not current_stream_task.done():
        current_stream_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(current_stream_task), timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    for task in [
        build_task, universe_resolve_task, refresh_task, prewarm_task,
        flow_writer_task, signal_writer_task, universe_refresh_task,
        chain_task, keepalive_task, gate_config_refresh_task, lookback_task,
    ]:
        task.cancel()

    await asyncio.gather(
        build_task, universe_resolve_task, refresh_task, prewarm_task,
        flow_writer_task, signal_writer_task, universe_refresh_task,
        chain_task, keepalive_task, gate_config_refresh_task, lookback_task,
        return_exceptions=True,
    )
    log.info("[shutdown] all tasks cancelled")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Cipher", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(flows.router)
app.include_router(signals.router)
app.include_router(symbols.router)
app.include_router(snapshots.router)
app.include_router(tiers.router)
app.include_router(universe.router)
app.include_router(admin.router)
app.include_router(ingestion_config.router)
app.include_router(signal_config.router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "registry_ready": registry.is_ready(),
        "registry_epoch": registry.epoch,
        "registry_contracts": len(registry._registry),  # noqa: SLF001
        "build_done": _registry_build_done.is_set() if _registry_build_done else False,
        "ts": time.time(),
    }
