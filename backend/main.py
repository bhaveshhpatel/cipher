"""
Cipher Backend — FastAPI entry point
"""
import asyncio
import json
import logging
import re
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from routers import auth, flow, simulation, ws, smart_signals
from routers.smart_signals import stream_stats
from routers import history
from routers import admin
from routers import health          # B-008: stream health router
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
        "app_env":  settings.APP_ENV,
        "log_level": settings.LOG_LEVEL,
    }


# ---------------------------------------------------------------------------
# Universe loader — returns (stream_symbols, tier_map, quotes)
# ---------------------------------------------------------------------------
async def _resolve_startup_universe() -> tuple[list[str], dict[str, int], list]:
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
        return fresh, tier_map, []

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
    return stream_symbols, tier_map, quotes


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting Cipher backend…")

    stream_symbols, tier_map, quotes = await _resolve_startup_universe()

    registry = init_registry(watchlist=stream_symbols, tier_map=tier_map)
    log.info(
        "[registry] Initialised with %d stream symbols, %d tiers mapped",
        len(stream_symbols), len(tier_map),
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

    stream_task       = asyncio.create_task(stream_options_flow(stream_symbols))
    db_write_task     = asyncio.create_task(start_flow_writer())
    signal_write_task = asyncio.create_task(start_signal_writer())
    refresh_task      = asyncio.create_task(_universe_refresh_loop())

    yield

    refresh_task.cancel()
    registry_refresh_task.cancel()
    stream_task.cancel()
    db_write_task.cancel()
    signal_write_task.cancel()
    for task in (stream_task, db_write_task, signal_write_task, refresh_task, registry_refresh_task):
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
