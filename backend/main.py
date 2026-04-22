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
from services.tradier_stream import stream_options_flow

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
log = logging.getLogger("main")

# Default symbols to stream (expand via env or DB in production)
DEFAULT_SYMBOLS = [
    "AAPL","TSLA","NVDA","SPY","QQQ","MSFT","AMZN","META",
    "GOOGL","AMD","PLTR","SOFI","HOOD","RIVN","CRWD","NET",
]

# ── Lifespan: start stream on startup ─────────────────────────────────────
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

# ── App ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title       = "Cipher API",
    description = "Institutional options flow intelligence platform.",
    version     = "1.0.0",
    lifespan    = lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins     = settings.origins,
    allow_credentials = True,
    allow_methods     = ["*"],
    allow_headers     = ["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(flow.router)
app.include_router(simulation.router)
app.include_router(ws.router)
app.include_router(smart_signals.router)

# Alias /api/stream/stats → handled inside smart_signals router
# (already mounted under /api/signals prefix; frontend uses /api/stream/stats)
from routers.smart_signals import stream_stats
from core.auth import get_current_user
from fastapi import Depends
@app.get("/api/stream/stats", tags=["signals"])
async def _stream_stats_alias(current_user=Depends(get_current_user)):
    return await stream_stats(current_user)

# ── Health ────────────────────────────────────────────────────────────────
@app.get("/health", tags=["health"])
async def health():
    return JSONResponse({"status": "ok", "service": "cipher-api"})

@app.get("/", tags=["health"])
async def root():
    return JSONResponse({"message": "Cipher API v1.0 — Decode the Market"})
