"""
Cipher Backend — FastAPI entry point

Startup sequence:
  0. gate_config_store.load()          — load tier gate config from DB into memory  [ING-010]
  1. load_all_gate_states()            — load symbol/expiry gate overrides from DB   [ING-011]
  2. _resolve_startup_universe_fast()  — build ticker universe from DB snapshot      [B-009]
  3. universe_store.load()             — populate in-memory ticker set
  4. registry.load_from_db()           — pre-seed OCC registry from DB snapshot
  5. _start_stream_worker()            — launch Tradier SSE stream + signal pipeline

All environment variables are read from Railway (prod) or .env (dev).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.auth import get_current_user
from backend.routers import (
    admin,
    alerts,
    gates,
    health,
    signals,
    stream_control,
    universe,
)
from backend.services.gate_config_store import gate_config_store
from backend.services.gate_state_store import load_all_gate_states
from backend.services.signal_store import signal_store
from backend.services.stream_worker import (
    StreamWorker,
    get_stream_stats,
    stream_stats,
)
from backend.services.symbol_registry import registry
from backend.services.universe_store import universe_store

log = logging.getLogger(__name__)

# ───────────────────────────────────────────────────────────────────────────────
# startup helpers
# ───────────────────────────────────────────────────────────────────────────────

async def _resolve_startup_universe_fast() -> tuple[list[str], Optional[str]]:
    """
    Fast startup path: attempt to load the ticker universe from the most
    recent DB snapshot rather than hitting the Tradier API cold.

    Returns
    -------
    tickers     : list of ticker strings (may be empty on DB error)
    snapshot_id : UUID of the snapshot used, or None
    """
    snapshot_id: Optional[str] = None
    tickers: list[str] = []

    try:
        snapshot_id = await universe_store.get_latest_snapshot_id()
        if snapshot_id:
            tickers = await universe_store.load_tickers_from_snapshot(snapshot_id)
            log.info(
                "[startup] universe fast-path: loaded %d tickers from snapshot %s",
                len(tickers), snapshot_id,
            )
        else:
            log.info("[startup] universe fast-path: no snapshot found — will cold-build")
    except Exception as exc:  # noqa: BLE001
        log.warning("[startup] universe fast-path error: %s — falling back to cold build", exc)
        snapshot_id = None
        tickers = []

    return tickers, snapshot_id


async def _start_stream_worker(tickers: list[str]) -> None:
    """Instantiate and launch the StreamWorker background task."""
    worker = StreamWorker(tickers=tickers)
    asyncio.create_task(worker.run(), name="stream_worker")
    log.info("[startup] stream worker launched with %d tickers", len(tickers))


# ───────────────────────────────────────────────────────────────────────────────
# lifespan
# ───────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── STARTUP ─────────────────────────────────────────────────────────────────
    log.info("[startup] Cipher backend starting ————————————————————")
    t0 = time.monotonic()

    # Step 0 — gate config
    try:
        await gate_config_store.load()
        log.info("[startup] Step 0: gate_config_store loaded")
    except Exception as exc:  # noqa: BLE001
        log.error("[startup] Step 0 FAILED: %s", exc)

    # Step 1 — gate state overrides
    try:
        await load_all_gate_states()
        log.info("[startup] Step 1: gate state overrides loaded")
    except Exception as exc:  # noqa: BLE001
        log.error("[startup] Step 1 FAILED: %s", exc)

    # Step 2 — resolve ticker universe (fast path from DB snapshot)
    try:
        tickers, snapshot_id = await _resolve_startup_universe_fast()
        log.info("[startup] Step 2: universe resolved — %d tickers", len(tickers))
    except Exception as exc:  # noqa: BLE001
        log.error("[startup] Step 2 FAILED: %s", exc)
        tickers, snapshot_id = [], None

    # Step 3 — populate universe_store in-memory set
    try:
        await universe_store.load()
        log.info("[startup] Step 3: universe_store.load() complete")
    except Exception as exc:  # noqa: BLE001
        log.error("[startup] Step 3 FAILED: %s", exc)

    # Step 4 — pre-seed OCC registry from DB snapshot
    try:
        seeded = await registry.load_from_db(snapshot_id)
        log.info("[startup] Step 4: registry pre-seeded with %d contracts", seeded)
    except Exception as exc:  # noqa: BLE001
        log.error("[startup] Step 4 FAILED: %s", exc)

    # Step 5 — launch stream worker
    try:
        await _start_stream_worker(tickers)
        log.info("[startup] Step 5: stream worker launched")
    except Exception as exc:  # noqa: BLE001
        log.error("[startup] Step 5 FAILED: %s", exc)

    log.info("[startup] boot complete in %.2fs", time.monotonic() - t0)

    yield

    # ── SHUTDOWN ───────────────────────────────────────────────────────────────
    log.info("[shutdown] Cipher backend stopping")


# ───────────────────────────────────────────────────────────────────────────────
# FastAPI app
# ───────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Cipher Backend",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://cipher-frontend.vercel.app",
        os.getenv("FRONTEND_URL", ""),
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routers
app.include_router(admin.router,          prefix="/api/admin",    tags=["admin"])
app.include_router(alerts.router,         prefix="/api/alerts",   tags=["alerts"])
app.include_router(gates.router,          prefix="/api/gates",    tags=["gates"])
app.include_router(health.router,         prefix="/api/health",   tags=["health"])
app.include_router(signals.router,        prefix="/api/signals",  tags=["signals"])
app.include_router(stream_control.router, prefix="/api/stream",   tags=["stream"])
app.include_router(universe.router,       prefix="/api/universe", tags=["universe"])


@app.get("/")
async def root():
    return {"status": "ok", "service": "cipher-backend"}


@app.get("/stream-stats")
async def get_stream_stats(current_user=Depends(get_current_user)):
    return stream_stats
