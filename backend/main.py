"""
Cipher Backend — FastAPI entry point
"""
import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import settings
from routers import auth, flow, simulation, ws, smart_signals
from routers.smart_signals import stream_stats
from routers import history
from routers import admin
from core.auth import get_current_user
from fastapi import Depends
from services.tradier_stream import stream_options_flow
from services.symbols_loader import load_universe, SEED_SYMBOLS
from services import universe_store
from services.flow_store import start_flow_writer
from services.signal_store import start_signal_writer


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
# Universe loader
# ---------------------------------------------------------------------------
async def _resolve_startup_universe() -> list[str]:
    """
    Priority:
      1. Fresh DB snapshot (< 24h old) — stream starts instantly
      2. Tradier fetch + validate + screen — saves to DB, then starts
      3. Any DB snapshot (stale)          — fallback if Tradier is down
      4. SEED_SYMBOLS                     — last resort
    """
    log.info("[universe] Step 1: checking for fresh DB snapshot (max_age=24h)")

    fresh = await universe_store.load_fresh_snapshot(max_age_hours=24)
    if fresh:
        log.info(
            "[universe] Step 1 HIT: loaded fresh universe from DB (%d symbols) — stream starting",
            len(fresh),
        )
        return fresh

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
    symbols, source, stream_eligible_set, quotes = await load_universe(db_snapshot=stale)
    log.info(
        "[universe] Step 2b: load_universe returned source=%s symbols=%d eligible=%s",
        source, len(symbols),
        len(stream_eligible_set) if stream_eligible_set is not None else "n/a",
    )

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

        if quotes:
            log.info("[universe] Step 3b: upserting %d symbol quotes (last_price, volume)", len(quotes))
            await universe_store.upsert_symbol_quotes(quotes)
            log.info("[universe] Step 3b SUCCESS: symbol quotes persisted")
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
    return stream_symbols


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
            symbols, source, stream_eligible_set, quotes = await load_universe(db_snapshot=stale)
            if source == "tradier_validated":
                saved = await universe_store.save_snapshot(symbols, source, stream_eligible_set)
                if saved and quotes:
                    await universe_store.upsert_symbol_quotes(quotes)
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

    symbols             = await _resolve_startup_universe()
    stream_task         = asyncio.create_task(stream_options_flow(symbols))
    db_write_task       = asyncio.create_task(start_flow_writer())
    signal_write_task   = asyncio.create_task(start_signal_writer())   # Phase 4
    refresh_task        = asyncio.create_task(_universe_refresh_loop())

    yield

    refresh_task.cancel()
    stream_task.cancel()
    db_write_task.cancel()
    signal_write_task.cancel()
    for task in (stream_task, db_write_task, signal_write_task, refresh_task):
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

_configured_origins = settings.origins
_use_wildcard       = "*" in _configured_origins

if not _use_wildcard:
    _base = [
        "http://localhost:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3000",
    ]
    _allow_origins = list(dict.fromkeys(_base + _configured_origins))
else:
    _allow_origins = ["*"]

log.info("CORS allowed origins: %s", _allow_origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = _allow_origins,
    allow_credentials = not _use_wildcard,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
    expose_headers    = ["*"],
)

app.include_router(auth.router)
app.include_router(flow.router)
app.include_router(simulation.router)
app.include_router(ws.router)
app.include_router(smart_signals.router)
app.include_router(history.router)       # Phase 4: signal history endpoint
app.include_router(admin.router)         # Phase 5B: admin demo toggle

@app.get("/api/stream/stats", tags=["signals"])
async def _stream_stats_alias(current_user=Depends(get_current_user)):
    return await stream_stats(current_user)

@app.get("/health", tags=["health"])
async def health():
    return JSONResponse({"status": "ok", "service": "cipher-api"})

@app.get("/", tags=["health"])
async def root():
    return JSONResponse({"message": "Cipher API v1.0 — Decode the Market"})
