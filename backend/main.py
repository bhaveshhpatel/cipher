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
from core.auth import get_current_user
from fastapi import Depends
from services.tradier_stream import stream_options_flow
from services.symbols_loader import load_universe, SEED_SYMBOLS
from services import universe_store


# ── Structured JSON log formatter ────────────────────────────────────────────
class _JsonFormatter(logging.Formatter):
    """
    Emit one JSON object per log line so Railway's log collector reads
    `severity` from the structured field instead of regex-guessing it
    from the raw message string.
    """
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
# Universe loader — resolves symbols at startup with full fallback chain
# ---------------------------------------------------------------------------
async def _resolve_startup_universe() -> list[str]:
    """
    Priority:
      1. Fresh DB snapshot (< 24h old) — stream starts instantly
      2. Tradier fetch + validate     — saves to DB, then starts
      3. Any DB snapshot (stale)      — fallback if Tradier is down
      4. SEED_SYMBOLS                 — last resort
    """
    # Step 1 — try fresh DB snapshot first (fast path)
    fresh = universe_store.load_fresh_snapshot(max_age_hours=24)
    if fresh:
        log.info("Startup: loaded fresh universe from DB (%d symbols)", len(fresh))
        return fresh

    log.info("Startup: no fresh DB snapshot — fetching from Tradier")

    # Step 2 — fetch from Tradier, with stale DB as fallback arg
    stale = universe_store.load_any_snapshot()
    symbols, source = await load_universe(db_snapshot=stale)

    # Persist to DB if we got a live validated universe
    if source == "tradier_validated":
        saved = universe_store.save_snapshot(symbols, source)
        if not saved:
            log.warning("Startup: failed to persist universe snapshot to DB")

    log.info("Startup: universe resolved — %d symbols (source=%s)", len(symbols), source)
    return symbols


# ---------------------------------------------------------------------------
# Background 24-hour refresh
# ---------------------------------------------------------------------------
async def _universe_refresh_loop():
    """
    Refreshes the universe every 24 hours in the background.
    Never cancels the main stream — stream keeps running with current symbols.
    On failure, logs a warning and keeps the existing snapshot active.
    """
    REFRESH_INTERVAL = 24 * 60 * 60  # 24 hours in seconds
    while True:
        await asyncio.sleep(REFRESH_INTERVAL)
        log.info("Background universe refresh starting")
        try:
            stale = universe_store.load_any_snapshot()
            symbols, source = await load_universe(db_snapshot=stale)
            if source == "tradier_validated":
                saved = universe_store.save_snapshot(symbols, source)
                log.info(
                    "Background refresh complete: %d symbols saved=%s",
                    len(symbols), saved,
                )
            else:
                log.warning(
                    "Background refresh could not reach Tradier — keeping current snapshot "
                    "(source=%s)", source,
                )
        except Exception as e:
            log.error("Background universe refresh failed (non-fatal): %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting Cipher backend…")

    # Resolve universe (DB → Tradier → stale DB → seed)
    symbols = await _resolve_startup_universe()

    # Start stream with resolved universe
    stream_task = asyncio.create_task(stream_options_flow(symbols))

    # Start background 24-hour refresh
    refresh_task = asyncio.create_task(_universe_refresh_loop())

    yield

    refresh_task.cancel()
    stream_task.cancel()
    for task in (stream_task, refresh_task):
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

# ── CORS ──────────────────────────────────────────────────────────────────
_configured_origins = settings.origins
_use_wildcard = "*" in _configured_origins

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

# ── Routers ───────────────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(flow.router)
app.include_router(simulation.router)
app.include_router(ws.router)
app.include_router(smart_signals.router)

@app.get("/api/stream/stats", tags=["signals"])
async def _stream_stats_alias(current_user=Depends(get_current_user)):
    return await stream_stats(current_user)

@app.get("/health", tags=["health"])
async def health():
    return JSONResponse({"status": "ok", "service": "cipher-api"})

@app.get("/", tags=["health"])
async def root():
    return JSONResponse({"message": "Cipher API v1.0 — Decode the Market"})
