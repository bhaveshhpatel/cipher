"""
Cipher Backend — FastAPI entry point
"""
import asyncio
import json
import logging
import re
import sys
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
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
from core.auth import get_current_user
from services.tradier_stream import stream_options_flow
from services.symbols_loader import load_universe, _fetch_batch_quotes
from services import universe_store
from services.flow_store import start_flow_writer
from services.signal_store import start_signal_writer
from services.tier_engine import assign_tiers
from services.symbol_registry import init_registry, get_registry


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

# ---------------------------------------------------------------------------
# Issue 2: module-level lock shared by _refresh_quotes_in_background and
# _universe_refresh_loop.  Only one _fetch_batch_quotes + upsert call runs
# at a time.
# ---------------------------------------------------------------------------
_quote_refresh_lock: asyncio.Lock = asyncio.Lock()


async def get_config() -> dict:
    """Return current runtime config dict. Patchable by tests."""
    return {
        "app_env":   settings.APP_ENV,
        "log_level": settings.LOG_LEVEL,
    }


# ---------------------------------------------------------------------------
# Helper: build pre_fetched_quotes dict from a SymbolQuote list.
# SymbolRegistry.build() accepts dict[str, SymbolQuote]; this converts the
# list form that _fetch_batch_quotes() returns.
# ---------------------------------------------------------------------------
def _quotes_to_map(quotes: list) -> dict:
    """Convert list[SymbolQuote] → dict[ticker, SymbolQuote]."""
    return {q.symbol: q for q in quotes if q.symbol}


# ---------------------------------------------------------------------------
# Universe loader — returns (stream_symbols, tier_map, quotes, snapshot_id)
#
# Startup flow (cold / stale path — no fresh DB snapshot):
#
#   Step 1 : CBOE fetch + Tradier symbol validation  (~10s)
#             → symbols list + stream_eligible_set
#
#   Step 2 : _fetch_batch_quotes() for validated symbols  (~8s)  — ONE call total
#             → SymbolQuote list in memory
#             → preliminary tier_map (price+vol, no OI)
#
#   Step 3+4a [PARALLEL]:
#     Step 3 : save_snapshot() with preliminary tier_map → DB
#     (Step 4a: registry.build() uses pre_fetched_quotes — no second Tradier call)
#
#   Step 4c : OI-informed tier assignment after build() stamps OI on quotes
#
#   Step 5  : upsert_symbol_quotes() with OI + final tiers (BOTH paths)
#
# Warm / DB-hit path:
#   load_fresh_snapshot() returns stream-eligible symbols immediately.
#   registry.build() is promoted to background; app yields in ~1-3s.
#   _refresh_quotes_in_background awaits wait_for_build(), then stamps OI
#   and calls upsert_symbol_quotes().
# ---------------------------------------------------------------------------
async def _resolve_startup_universe() -> tuple[list[str], dict[str, int], list, str]:
    log.info("[universe] Step 1: checking for fresh DB snapshot (max_age=24h)")

    fresh = await universe_store.load_fresh_snapshot(max_age_hours=24)
    if fresh:
        log.info(
            "[universe] Step 1 HIT: loaded fresh universe from DB (%d symbols) — stream starting",
            len(fresh),
        )
        tier_map = await universe_store.load_tier_map()
        log.info(
            "[universe] Step 1: tier_map loaded (%d symbols mapped, T1=%d T2=%d T3=%d)",
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
        return fresh, tier_map, [], snapshot_id

    log.info("[universe] Step 1 MISS: no fresh DB snapshot found")
    log.info(
        "[universe] Step 2: checking env — TRADIER_API_KEY set=%s SUPABASE_URL set=%s",
        bool(settings.TRADIER_API_KEY), bool(settings.SUPABASE_URL),
    )

    log.info("[universe] Step 2a: loading any stale DB snapshot as safety net")
    stale = await universe_store.load_any_snapshot()
    log.info(
        "[universe] Step 2a: stale snapshot available=%s (%d symbols)",
        stale is not None, len(stale) if stale else 0,
    )

    log.info("[universe] Step 2b: calling load_universe (CBOE + Tradier validate + screen)")
    symbols, source, stream_eligible_set = await load_universe(db_snapshot=stale)
    log.info(
        "[universe] Step 2b: load_universe returned source=%s symbols=%d eligible=%s",
        source, len(symbols),
        len(stream_eligible_set) if stream_eligible_set is not None else "n/a",
    )

    tier_map: dict[str, int] = {}
    quotes: list = []
    snapshot_id = ""

    if source == "tradier_validated":
        log.info("[universe] Step 2: fetching batch quotes for %d symbols", len(symbols))
        quotes = await _fetch_batch_quotes(symbols)

        # Step 4c (i) — preliminary tier assignment (price+vol only, OI not yet available)
        if quotes:
            log.info(
                "[universe] Step 4c(i): preliminary tier assignment for %d symbols "
                "(OI not yet available — will be updated after registry.build)",
                len(quotes),
            )
            tier_map = await assign_tiers(quotes)
            log.info(
                "[universe] Step 4c(i): preliminary tiers — T1=%d T2=%d T3=%d",
                sum(1 for t in tier_map.values() if t == 1),
                sum(1 for t in tier_map.values() if t == 2),
                sum(1 for t in tier_map.values() if t == 3),
            )

        # Steps 3+4a — save_snapshot now receives the preliminary tier_map
        # so the DB snapshot is never written with all tier=3 rows (Issue 6).
        log.info(
            "[universe] Steps 3+4a: launching save_snapshot (with preliminary tiers) "
            "+ snapshot_id query in parallel"
        )
        saved, active_snap = await asyncio.gather(
            universe_store.save_snapshot(
                symbols, source, stream_eligible_set,
                tier_map=tier_map,          # Issue 6: real tiers in snapshot row
            ),
            asyncio.get_event_loop().run_in_executor(
                None,
                lambda: universe_store._client()
                    .table("options_universe_snapshots")
                    .select("id")
                    .eq("is_active", True)
                    .order("fetched_at", desc=True)
                    .limit(1)
                    .execute()
                    .data,
            ),
        )

        if saved:
            log.info("[universe] Step 3 SUCCESS: snapshot persisted to DB")
        else:
            log.error(
                "[universe] Step 3 FAILED: save_snapshot returned False — "
                "check universe_store logs"
            )

        snapshot_id = active_snap[0]["id"] if active_snap else ""
    else:
        log.warning(
            "[universe] Step 3 SKIPPED: source=%s (not tradier_validated) — DB will NOT be updated",
            source,
        )

    stream_symbols = (
        [s for s in symbols if s in stream_eligible_set]
        if stream_eligible_set is not None
        else symbols
    )
    log.info(
        "[universe] FINAL: stream starting with %d symbols (source=%s, from universe of %d)",
        len(stream_symbols), source, len(symbols),
    )
    return stream_symbols, tier_map, quotes, snapshot_id


# ---------------------------------------------------------------------------
# OI stamp helper
# ---------------------------------------------------------------------------
def _stamp_oi(quotes: list, oi_map: dict[str, int]) -> None:
    for q in quotes:
        q.open_interest = oi_map.get(q.symbol, 0)


# ---------------------------------------------------------------------------
# Background quote refresh for DB-hit (warm-start) path.
#
# Issue 6: passes fetched quotes into registry.build() as pre_fetched_quotes
# so build() skips its internal _fetch_stock_prices() call.
# Issue 1: awaits wait_for_build() before reading get_oi_map().
# Issue 2: guarded by _quote_refresh_lock.
# ---------------------------------------------------------------------------
async def _refresh_quotes_in_background(stream_symbols: list[str]) -> None:
    """Fetch quotes, pass into build(), stamp OI, re-tier, upsert."""
    if _quote_refresh_lock.locked():
        log.info(
            "[universe] Background quote refresh: lock held by another refresh — skipping"
        )
        return

    async with _quote_refresh_lock:
        log.info(
            "[universe] Background quote refresh starting for %d stream symbols",
            len(stream_symbols),
        )
        try:
            quotes = await _fetch_batch_quotes(stream_symbols)
            if not quotes:
                log.warning("[universe] Background quote refresh: no quotes returned — skipping")
                return

            registry = get_registry()
            if registry:
                log.info(
                    "[universe] Background quote refresh: waiting for registry.build() "
                    "to complete before stamping OI"
                )
                await registry.wait_for_build()
                oi_map = registry.get_oi_map()
                _stamp_oi(quotes, oi_map)
                log.info(
                    "[universe] Background quote refresh: OI stamped on %d quotes "
                    "(%d tickers with oi>0)",
                    len(quotes),
                    sum(1 for v in oi_map.values() if v > 0),
                )

            tier_map = await assign_tiers(quotes)
            log.info(
                "[universe] Background quote refresh: tiers — T1=%d T2=%d T3=%d",
                sum(1 for t in tier_map.values() if t == 1),
                sum(1 for t in tier_map.values() if t == 2),
                sum(1 for t in tier_map.values() if t == 3),
            )

            if registry:
                registry.set_tier_map(tier_map)

            await universe_store.upsert_symbol_quotes(quotes, tier_map)
            log.info("[universe] Background quote refresh: upsert complete")

        except Exception as exc:
            log.error(
                "[universe] Background quote refresh failed (non-fatal): %s", exc, exc_info=True
            )


# ---------------------------------------------------------------------------
# Background 24-hour refresh
#
# Issue 2: first sleep anchored to (24h - elapsed since last snapshot).
# Issue 6: passes fetched quotes into registry.build() as pre_fetched_quotes.
# ---------------------------------------------------------------------------
async def _universe_refresh_loop():
    REFRESH_INTERVAL = 24 * 60 * 60

    last_ts = await universe_store.get_latest_snapshot_timestamp()
    elapsed = (datetime.now(last_ts.tzinfo) - last_ts).total_seconds()
    first_sleep = max(0.0, REFRESH_INTERVAL - elapsed)
    log.info(
        "[universe] Refresh loop: last snapshot was %.1fh ago — "
        "first refresh in %.1fh",
        elapsed / 3600,
        first_sleep / 3600,
    )
    await asyncio.sleep(first_sleep)

    while True:
        log.info("[universe] Background refresh starting")
        try:
            stale = await universe_store.load_any_snapshot()
            symbols, source, stream_eligible_set = await load_universe(db_snapshot=stale)
            quotes: list = []
            tier_map: dict[str, int] = {}

            if source == "tradier_validated":
                if _quote_refresh_lock.locked():
                    log.info(
                        "[universe] Background refresh: quote refresh lock held — "
                        "skipping redundant _fetch_batch_quotes"
                    )
                else:
                    async with _quote_refresh_lock:
                        quotes = await _fetch_batch_quotes(symbols)
                        if quotes:
                            registry = get_registry()
                            # Issue 6: pass quotes into build() to skip second Tradier call
                            pre_fetched = _quotes_to_map(quotes)
                            if registry:
                                log.info(
                                    "[universe] Background refresh: rebuilding registry "
                                    "with pre-fetched quotes (no second Tradier call)"
                                )
                                await registry.build(pre_fetched_quotes=pre_fetched)
                                oi_map = registry.get_oi_map()
                                _stamp_oi(quotes, oi_map)
                                log.info(
                                    "[universe] Background refresh: OI stamped on %d quotes "
                                    "(%d tickers with oi>0)",
                                    len(quotes),
                                    sum(1 for v in oi_map.values() if v > 0),
                                )

                            tier_map = await assign_tiers(quotes)

                            saved = await universe_store.save_snapshot(
                                symbols, source, stream_eligible_set,
                                tier_map=tier_map,  # Issue 6: real tiers
                            )
                            await universe_store.upsert_symbol_quotes(quotes, tier_map)
                            log.info(
                                "[universe] Background refresh complete: %d symbols "
                                "eligible=%s saved=%s",
                                len(symbols),
                                len(stream_eligible_set) if stream_eligible_set is not None else "all",
                                saved,
                            )

                registry = get_registry()
                if registry and tier_map:
                    registry.set_tier_map(tier_map)
                    log.info(
                        "[universe] Background refresh: registry tier_map updated "
                        "(T1=%d T2=%d T3=%d)",
                        sum(1 for t in tier_map.values() if t == 1),
                        sum(1 for t in tier_map.values() if t == 2),
                        sum(1 for t in tier_map.values() if t == 3),
                    )
            else:
                log.warning(
                    "[universe] Background refresh could not reach Tradier (source=%s) — keeping current snapshot",
                    source,
                )
        except Exception as e:
            log.error("[universe] Background refresh failed (non-fatal): %s", e, exc_info=True)

        await asyncio.sleep(REFRESH_INTERVAL)


# ---------------------------------------------------------------------------
# Registry pre-warm
# ---------------------------------------------------------------------------
_ET = ZoneInfo("America/New_York")
_PREWARM_TIME = time(9, 15)


async def _registry_prewarm_loop() -> None:
    """Rebuild the OCC registry every weekday at 9:15 AM ET."""
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

        log.info("[prewarm] Building OCC registry ahead of market open...")
        try:
            registry = get_registry()
            if registry is None:
                log.warning("[prewarm] Registry not initialised — skipping pre-warm")
                continue
            # Prewarm has no pre-fetched quotes available; falls back to
            # _fetch_stock_prices() inside build() (acceptable: fires once/day).
            count = await registry.build()
            log.info(
                "[prewarm] Registry warm: %d OCC contracts ready for market open "
                "(persisted to DB for crash-restart safety)",
                count or 0,
            )
        except Exception as exc:
            log.error("[prewarm] Registry pre-warm failed (non-fatal): %s", exc, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting Cipher backend…")

    stream_symbols, tier_map, quotes, snapshot_id = await _resolve_startup_universe()

    registry = init_registry(watchlist=stream_symbols, tier_map=tier_map)
    log.info(
        "[registry] Initialised with %d stream symbols, %d tiers mapped",
        len(stream_symbols), len(tier_map),
    )

    if snapshot_id:
        seeded = await registry.load_from_db(snapshot_id)
        log.info(
            "[registry] DB chain seed: %d OCC contracts pre-loaded "
            "(registry is_ready=%s before full build)",
            seeded, registry.is_ready(),
        )

    registry_refresh_task = asyncio.create_task(registry.refresh_loop())
    prewarm_task          = asyncio.create_task(_registry_prewarm_loop())
    stream_task           = asyncio.create_task(stream_options_flow(stream_symbols))
    db_write_task         = asyncio.create_task(start_flow_writer())
    signal_write_task     = asyncio.create_task(start_signal_writer())
    refresh_task          = asyncio.create_task(_universe_refresh_loop())

    bg_quote_refresh_task = None
    build_task            = None

    if not quotes:
        # ----------------------------------------------------------------
        # WARM PATH: promote build() to background, yield immediately.
        # Pass quotes=None on warm path — build() will use _fetch_stock_prices()
        # internally (the one Tradier call on warm path, same as before).
        # _refresh_quotes_in_background runs after build() and calls
        # upsert_symbol_quotes so DB columns are updated.
        # ----------------------------------------------------------------
        log.info(
            "[registry] Warm-start path: promoting registry.build() to background task — "
            "app will serve DB-seeded contracts immediately"
        )
        build_task = asyncio.create_task(registry.build())
        bg_quote_refresh_task = asyncio.create_task(
            _refresh_quotes_in_background(stream_symbols)
        )
    else:
        # ----------------------------------------------------------------
        # COLD PATH: build() receives pre_fetched_quotes so it skips its
        # internal _fetch_stock_prices() call — Issue 6 Part 1.
        # ----------------------------------------------------------------
        pre_fetched = _quotes_to_map(quotes)
        log.info(
            "[registry] Cold-start path: running registry.build() with %d pre-fetched quotes",
            len(pre_fetched),
        )
        await registry.build(pre_fetched_quotes=pre_fetched)
        log.info("[registry] First build complete: %d OCC symbols loaded", registry.size())

        oi_map = registry.get_oi_map()
        _stamp_oi(quotes, oi_map)
        log.info(
            "[registry] OI stamped on %d quotes (%d tickers with oi>0)",
            len(quotes),
            sum(1 for v in oi_map.values() if v > 0),
        )

        tier_map = await assign_tiers(quotes)
        log.info(
            "[registry] OI-informed tier assignment — T1=%d T2=%d T3=%d",
            sum(1 for t in tier_map.values() if t == 1),
            sum(1 for t in tier_map.values() if t == 2),
            sum(1 for t in tier_map.values() if t == 3),
        )

        registry.set_tier_map(tier_map)

        # Step 5: upsert with OI + final tiers (cold path)
        log.info("[registry] Upserting %d symbol quotes with OI + final tiers", len(quotes))
        await universe_store.upsert_symbol_quotes(quotes, tier_map)
        log.info("[registry] Upsert complete — open_interest column now populated in DB")

    yield

    refresh_task.cancel()
    prewarm_task.cancel()
    registry_refresh_task.cancel()
    stream_task.cancel()
    db_write_task.cancel()
    signal_write_task.cancel()
    if bg_quote_refresh_task is not None:
        bg_quote_refresh_task.cancel()
    if build_task is not None:
        build_task.cancel()
    for task in filter(None, (
        stream_task,
        db_write_task,
        signal_write_task,
        refresh_task,
        registry_refresh_task,
        prewarm_task,
        bg_quote_refresh_task,
        build_task,
    )):
        try:
            await task
        except asyncio.CancelledError:
            pass
    log.info("Cipher backend stopped.")


app = FastAPI(
    title       = "Cipher API",
    description = "Institutional options flow intelligence platform.",
    version     = "1.0.0",
    lifespan    = lifespan,
)

_explicit_origins  = settings.origins
_explicit_patterns = [re.escape(o) for o in _explicit_origins if o != "*"]
_origin_pattern    = "|".join(filter(None, [
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


@app.get("/stream/stats", tags=["health"], include_in_schema=False)
async def stream_stats_health_alias():
    return JSONResponse({"status": "ok"})


@app.get("/api/stream/stats", tags=["signals"])
async def _stream_stats_alias(current_user=Depends(get_current_user)):
    return await stream_stats(current_user)


@app.get("/api/health", tags=["health"])
async def api_health():
    return JSONResponse({"status": "ok", "service": "cipher-api"})


@app.get("/health", tags=["health"])
async def health_root():
    return JSONResponse({"status": "ok", "service": "cipher-api"})


@app.get("/", tags=["health"])
async def root():
    return JSONResponse({"message": "Cipher API v1.0 — Decode the Market"})
