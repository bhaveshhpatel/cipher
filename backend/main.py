"""
Cipher Backend — FastAPI entry point
"""
import asyncio
import logging
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
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
# Build allowed origins: always include localhost, plus whatever is in
# ALLOWED_ORIGINS env var.  If the env var contains "*" we go full wildcard.
_configured_origins = settings.origins
_use_wildcard = "*" in _configured_origins

if not _use_wildcard:
    # Always ensure local dev origins are present
    _base = [
        "http://localhost:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3000",
    ]
    _allow_origins = list(dict.fromkeys(_base + _configured_origins))  # deduplicated
else:
    _allow_origins = ["*"]

log.info("CORS allowed origins: %s", _allow_origins)

app.add_middleware(
    CORSMiddleware,
    allow_origins     = _allow_origins,
    allow_credentials = not _use_wildcard,   # credentials incompatible with wildcard
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
