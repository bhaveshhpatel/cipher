"""
Cipher Backend — FastAPI entry point

Startup sequence (post all fixes):
  1. _resolve_startup_universe()     — load symbols from DB (<2s)
  2. init_registry()                 — in-memory init (instant)
  3. registry.load_from_db()         — seed OCC chains from DB via P1 fallback
  4. yield                           — SERVER IS LIVE, health probe passes
  5. Parallel background tasks launched:
     a. _background_build_and_upsert — incremental/full OCC build (P4)
     b. registry.refresh_loop()      — scheduled 30-min rebuilds
     c. _registry_prewarm_loop()     — 9:15 AM ET daily pre-warm
     d. stream_options_flow()        — waits for is_ready(), then streams
     e. start_flow_writer()          — DB flush loop
     f. start_signal_writer()        — signal DB writer
     g. _universe_refresh_loop()     — 24h universe refresh

Key architectural fixes:
  P1 (chain_store)    — snapshot-agnostic fallback load
  P2 (lifespan)       — non-blocking startup via background build task
  P3 (tradier_client) — dedicated bulk semaphore for build()
  P4 (symbol_registry)— incremental warm-restart build
  D-001 (tradier_stream) — pass registry to stream_options_flow(), no duplicate build
  D-002 (tradier_stream) — remove extra refresh_loop() create_task from stream
  RC-3 (ingestion_config) — validate_ingestion_config() at startup warns on missing DB rows
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
from services.ingestion_config import validate_ingestion_config


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
        log.info(
            "[universe] Step 3: persisting tradier_validated snapshot (%d symbols, %d eligible) to DB",
            len(symbols),
            len(stream_eligible_set) if stream_eligible_set is not None else len(symbols),
        )
        saved = await universe_store.save_snapshot(symbols, source, stream_eligible_set)
        if saved:
            log.info("[universe] Step 3 SUCCESS: snapshot persisted to DB")
        else:
            log.error("[universe] Step 3 FAILED: save_snapshot returned False")

        log.info("[universe] Step 3b: fetching batch quotes for %d symbols", len(symbols))
        quotes = await _fetch_batch_quotes(symbols)
        if quotes:
            log.info("[universe] Step 3b: preliminary tier assignment for %d symbols", len(quotes))
            tier_map = await assign_tiers(quotes)
            log.info(
                "[universe] Step 3b: preliminary tiers — T1=%d T2=%d T3=%d",
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


def _stamp_oi(quotes: list, oi_map: dict[str, int]) -> None:
    for q in quotes:
        q.open_interest = oi_map.get(q.symbol, 0)


async def _post_build_upsert(registry, stream_symbols: list[str]) -> None:
    try:
        log.info("[post_build] Fetching quotes for %d symbols", len(stream_symbols))
        quotes = await _fetch_batch_quotes(stream_symbols)
        if not quotes:
            log.warning("[post_build] No quotes returned — skipping upsert")
            return

        oi_map = registry.get_oi_map()
        _stamp_oi(quotes, oi_map)
        log.info(
            "[post_build] OI stamped on %d quotes (%d tickers with oi>0)",
            len(quotes), sum(1 for v in oi_map.values() if v > 0),
        )

        tier_map = await assign_tiers(quotes)
        registry.set_tier_map(tier_map)
        log.info(
            "[post_build] Tier map updated — T1=%d T2=%d T3=%d",
            sum(1 for t in tier_map.values() if t == 1),
            sum(1 for t in tier_map.values() if t == 2),
            sum(1 for t in tier_map.values() if t == 3),
        )

        await universe_store.upsert_symbol_quotes(quotes, tier_map)
        log.info("[post_build] upsert_symbol_quotes complete — DB columns now populated")
    except Exception as exc:
        log.error("[post_build] error (non-fatal): %s", exc, exc_info=True)


async def _background_build_and_upsert(registry, stream_symbols: list[str]) -> None:
    log.info("[registry] Background build starting (server already live)")
    try:
        await registry.build()
        log.info("[registry] Background build complete: %d OCC symbols", registry.size())
    except Exception as exc:
        log.error("[registry] Background build failed: %s", exc, exc_info=True)
        return
    await _post_build_upsert(registry, stream_symbols)


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


_ET = ZoneInfo("America/New_York")
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

        log.info("[prewarm] Building OCC registry ahead of market open...")
        try:
            registry = get_registry()
            if registry is None:
                log.warning("[prewarm] Registry not initialised — skipping pre-warm")
                continue
            count = await registry.build()
            log.info(
                "[prewarm] Registry warm: %d OCC contracts ready for market open",
                count or 0,
            )
        except Exception as exc:
            log.error("[prewarm] Registry pre-warm failed (non-fatal): %s", exc, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting Cipher backend…")

    # RC-3: validate ingestion config rows at startup — warns on missing DB keys
    await validate_ingestion_config()

    stream_symbols, tier_map, _quotes, snapshot_id = await _resolve_startup_universe()

    registry = init_registry(watchlist=stream_symbols, tier_map=tier_map)
    log.info(
        "[registry] Initialised with %d stream symbols, %d tiers mapped",
        len(stream_symbols), len(tier_map),
    )

    if snapshot_id:
        seeded = await registry.load_from_db(snapshot_id)
        log.info(
            "[registry] DB chain seed: %d OCC contracts pre-loaded "
            "(registry is_ready=%s)",
            seeded, registry.is_ready(),
        )

    log.info(
        "[registry] Server starting (is_ready=%s, %d OCC contracts seeded). "
        "Background build will refresh in the background.",
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

    yield

    build_task.cancel()
    refresh_task.cancel()
    prewarm_task.cancel()
    registry_refresh_task.cancel()
    stream_task.cancel()
    db_write_task.cancel()
    signal_write_task.cancel()
    for task in (
        build_task,
        stream_task,
        db_write_task,
        signal_write_task,
        refresh_task,
        registry_refresh_task,
        prewarm_task,
    ):
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
