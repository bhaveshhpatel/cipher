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
from routers import health          # B-008: stream health router
from routers import apex_gate       # Apex Phase 1: hard/soft gate config
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
# get_config — module-level so tests can patch('main.get_config', ...)
# ---------------------------------------------------------------------------
async def get_config() -> dict:
    """Return current runtime config dict. Patchable by tests."""
    return {
        "app_env":           settings.APP_ENV,
        "log_level":         settings.LOG_LEVEL,
        "ingestion_enabled": settings.INGESTION_ENABLED,
    }


# ---------------------------------------------------------------------------
# Universe loader — returns (stream_symbols, tier_map, quotes, snapshot_id)
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
        # Resolve the active snapshot_id so registry can load from DB cache
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
            log.error("[universe] Step 3 FAILED: save_snapshot returned False — check universe_store logs")

        # Fetch quotes separately now that load_universe no longer returns them
        log.info("[universe] Step 3b: fetching batch quotes for %d symbols", len(symbols))
        quotes = await _fetch_batch_quotes(symbols)
        if quotes:
            log.info("[universe] Step 3b: preliminary tier assignment for %d symbols (OI not yet available)", len(quotes))
            tier_map = await assign_tiers(quotes)
            log.info(
                "[universe] Step 3b: preliminary tiers — T1=%d T2=%d T3=%d",
                sum(1 for t in tier_map.values() if t == 1),
                sum(1 for t in tier_map.values() if t == 2),
                sum(1 for t in tier_map.values() if t == 3),
            )

        # Resolve the new snapshot_id
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


# ---------------------------------------------------------------------------
# OI stamp helper
# ---------------------------------------------------------------------------
def _stamp_oi(quotes: list, oi_map: dict[str, int]) -> None:
    for q in quotes:
        q.open_interest = oi_map.get(q.symbol, 0)


# ---------------------------------------------------------------------------
# Background 24-hour refresh
# ---------------------------------------------------------------------------
async def _universe_refresh_loop():
    REFRESH_INTERVAL = 24 * 60 * 60
    while True:
        await asyncio.sleep(REFRESH_INTERVAL)
        log.info("[universe] Background refresh starting")
        try:
            stale = await universe_store.load_any_snapshot()
            symbols, source, stream_eligible_set = await load_universe(db_snapshot=stale)
            quotes: list = []
            if source == "tradier_validated":
                saved = await universe_store.save_snapshot(symbols, source, stream_eligible_set)
                tier_map: dict[str, int] = {}

                # Fetch quotes separately
                quotes = await _fetch_batch_quotes(symbols)
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
                            len(quotes),
                            sum(1 for v in oi_map.values() if v > 0),
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
                    "[universe] Background refresh complete: %d symbols eligible=%s saved=%s",
                    len(symbols),
                    len(stream_eligible_set) if stream_eligible_set is not None else "all",
                    saved,
                )
            else:
                log.warning(
                    "[universe] Background refresh could not reach Tradier (source=%s) — keeping current snapshot",
                    source,
                )
        except Exception as e:
            log.error("[universe] Background refresh failed (non-fatal): %s", e, exc_info=True)


# ---------------------------------------------------------------------------
# Registry pre-warm — rebuilds OCC registry at 9:15 AM ET every weekday
# so workers connect instantly at 9:30 with no cold-start delay.
# ---------------------------------------------------------------------------
_ET = ZoneInfo("America/New_York")
_PREWARM_TIME = time(9, 15)


async def _registry_prewarm_loop() -> None:
    """Rebuild the OCC registry every weekday at 9:15 AM ET.

    The lifespan already performs one build on startup. This loop handles
    every subsequent trading day, ensuring the registry is fresh and fully
    loaded 15 minutes before market open at 9:30 AM.

    After each build, the registry is persisted to DB via chain_store so that
    a crash/restart between 9:15 and 9:30 can fast-seed from the DB instead
    of re-fetching all chains from Tradier.
    """
    while True:
        now = datetime.now(_ET)

        if now.weekday() >= 5:  # Saturday=5, Sunday=6
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
            # Advance past any weekend
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
                "[prewarm] Registry warm: %d OCC contracts ready for market open "
                "(persisted to DB for crash-restart safety)",
                count or 0,
            )
        except Exception as exc:
            log.error("[prewarm] Registry pre-warm failed (non-fatal): %s", exc, exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting Cipher backend…")

    # ── Ingestion kill-switch (Option C) ─────────────────────────────────────
    # When INGESTION_ENABLED=false (preview/staging), skip all stream + DB
    # write tasks entirely so preview cannot pollute the shared production
    # Supabase tables with duplicate flow_events / smart_signals.
    #
    # TODO(parallel-schema): Remove this guard once Option A/B parallel schema
    # isolation is implemented — replace with schema-scoped writes instead.
    if not settings.INGESTION_ENABLED:
        log.warning(
            "[ingestion] INGESTION_ENABLED=false — stream, flow_writer, and "
            "signal_writer are DISABLED. Preview mode: read-only API only."
        )
        yield
        log.info("Cipher backend stopped (ingestion disabled).")
        return

    stream_symbols, tier_map, quotes, snapshot_id = await _resolve_startup_universe()

    registry = init_registry(watchlist=stream_symbols, tier_map=tier_map)
    log.info(
        "[registry] Initialised with %d stream symbols, %d tiers mapped",
        len(stream_symbols), len(tier_map),
    )

    # On HIT path: fast-seed from DB chain cache so lookup() works immediately
    if snapshot_id:
        seeded = await registry.load_from_db(snapshot_id)
        log.info(
            "[registry] DB chain seed: %d OCC contracts pre-loaded "
            "(registry is_ready=%s before full build)",
            seeded, registry.is_ready(),
        )

    log.info("[registry] Running first build (blocking startup until OI available)")
    await registry.build()
    log.info("[registry] First build complete: %d OCC symbols loaded", registry.size())

    if quotes:
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

        log.info("[registry] Upserting %d symbol quotes with OI + final tiers", len(quotes))
        await universe_store.upsert_symbol_quotes(quotes, tier_map)
        log.info("[registry] Upsert complete — open_interest column now populated in DB")

    registry_refresh_task = asyncio.create_task(registry.refresh_loop())
    prewarm_task          = asyncio.create_task(_registry_prewarm_loop())

    stream_task       = asyncio.create_task(stream_options_flow(stream_symbols))
    db_write_task     = asyncio.create_task(start_flow_writer())
    signal_write_task = asyncio.create_task(start_signal_writer())
    refresh_task      = asyncio.create_task(_universe_refresh_loop())

    yield

    refresh_task.cancel()
    prewarm_task.cancel()
    registry_refresh_task.cancel()
    stream_task.cancel()
    db_write_task.cancel()
    signal_write_task.cancel()
    for task in (
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

# ---------------------------------------------------------------------------
# CORS — use allow_origin_regex so all Vercel preview URLs are accepted.
#
# Pattern covers:
#   - https://*.vercel.app          (all Vercel production + preview deploys)
#   - http://localhost:3000/3001    (local dev)
#   - http://127.0.0.1:3000        (local dev alternative)
#   - Any explicit origins from CORS_ALLOWED_ORIGINS env var (escaped + OR'd in)
#
# We never use allow_origins=["*"] because that breaks allow_credentials=True.
# ---------------------------------------------------------------------------
_explicit_origins = settings.origins  # list[str] from env var

# Build regex alternation from any explicit non-wildcard origins in env
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
app.include_router(apex_gate.router)

# ---------------------------------------------------------------------------
# Aliases for legacy / health-check paths
# ---------------------------------------------------------------------------

# Railway health check probe is configured to hit /stream/stats (no /api prefix).
# Return a simple 200 so the probe passes without requiring auth.
@app.get("/stream/stats", tags=["health"], include_in_schema=False)
async def stream_stats_health_alias():
    return JSONResponse({"status": "ok"})

# /api/stream/stats — authenticated alias kept for backwards compat
@app.get("/api/stream/stats", tags=["signals"])
async def _stream_stats_alias(current_user=Depends(get_current_user)):
    return await stream_stats(current_user)

# /api/health — used by test_main_app.test_app_health_endpoint_exists
@app.get("/api/health", tags=["health"])
async def api_health():
    return JSONResponse({"status": "ok", "service": "cipher-api"})

@app.get("/health", tags=["health"])
async def health_root():
    return JSONResponse({"status": "ok", "service": "cipher-api"})

@app.get("/", tags=["health"])
async def root():
    return JSONResponse({"message": "Cipher API v1.0 — Decode the Market"})
