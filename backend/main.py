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


# ── Structured JSON log formatter ────────────────────────────────────────────
class _JsonFormatter(logging.Formatter):
    """
    Emit one JSON object per log line so Railway's log collector reads
    `severity` from the structured field instead of regex-guessing it
    from the raw message string.

    Railway maps the `severity` field directly to its log level UI —
    so INFO stays INFO, WARNING stays WARNING, ERROR stays ERROR.
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
    """
    Replace the root handler with a JSON formatter.
    Suppress httpx's INFO chatter — it logs every HTTP request at INFO
    which Railway mis-classifies as errors when the line doesn't parse cleanly.
    We keep WARNING+ from httpx so auth failures and timeouts still surface.
    """
    root = logging.getLogger()
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    # httpx logs every successful request at INFO — noisy and mis-classified
    # by Railway's text scanner.  Raise its floor to WARNING so only real
    # problems (timeouts, connection errors) come through.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


_configure_logging()
log = logging.getLogger("main")

DEFAULT_SYMBOLS = [
    "AAPL","TSLA","NVDA","SPY","QQQ","MSFT","AMZN","META",
    "GOOGL","AMD","PLTR","SOFI","HOOD","RIVN","CRWD","NET",
]

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Starting Cipher backend…")
    stream_task = asyncio.create_task(stream_options_flow(DEFAULT_SYMBOLS))
    yield
    stream_task.cancel()
    try:
        await stream_task
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
