"""
services/demo_engine.py — Controlled demo data engine for Cipher.

Provides a fully controllable synthetic options flow generator that can be
started/stopped via the admin API without touching the live Tradier stream.

Public API:
  is_running()        -> bool
  get_stats()         -> dict
  await start_demo()  -> dict   (idempotent)
  await stop_demo()   -> dict   (idempotent)

Internal helpers (used by tests):
  _nearest_friday(weeks)                          -> date
  _round_to_strike(price, contract_type)          -> float
  _build_occ_symbol(ticker, expiry, ctype, strike)-> str
  _build_timesale_envelope(...)                   -> dict
  _run_demo_loop(tickers)                         -> coroutine
"""
import asyncio
import logging
import random
from datetime import date, timedelta
from typing import Optional

log = logging.getLogger("demo_engine")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_running: bool = False
_task: Optional[asyncio.Task] = None
_demo_stats: dict = {
    "ticks_emitted": 0,
    "signals_emitted": 0,
    "errors": 0,
}

_DEFAULT_TICKERS = [
    "AAPL", "TSLA", "NVDA", "SPY", "QQQ",
    "MSFT", "AMZN", "META", "GOOGL", "NFLX",
]
_LEVELS    = ["CONVICTION", "STRONG_SIGNAL", "ALERT", "WATCH"]
_TIERS     = ["WHALE", "INSTITUTIONAL", "LARGE", "RETAIL"]
_EXCHANGES = ["C", "M", "Q", "X", "N", "B"]


# ---------------------------------------------------------------------------
# Pure helpers — deterministic, no I/O
# ---------------------------------------------------------------------------

def _nearest_friday(weeks: int = 1) -> date:
    """Return the date of the nearest future Friday, offset by `weeks` additional weeks."""
    today = date.today()
    days_ahead = (4 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    base_friday = today + timedelta(days=days_ahead)
    return base_friday + timedelta(weeks=weeks - 1)


def _round_to_strike(price: float, contract_type: str) -> float:  # noqa: ARG001
    """Round price to realistic strike increments based on price level."""
    if price < 50:
        increment = 0.50
    elif price < 200:
        increment = 1.0
    else:
        increment = 5.0
    return round(round(price / increment) * increment, 2)


def _build_occ_symbol(ticker: str, expiry: date, ctype: str, strike: float) -> str:
    ticker_padded = ticker.ljust(6)
    expiry_str    = expiry.strftime("%y%m%d")
    cp            = "C" if ctype.upper() == "CALL" else "P"
    strike_int    = int(round(strike * 1000))
    strike_str    = str(strike_int).zfill(8)
    return f"{ticker_padded}{expiry_str}{cp}{strike_str}"


def _build_timesale_envelope(
    ticker: str,
    expiry: date,
    ctype: str,
    strike: float,
    fill: float,
    size: int,
    exchange: str = "C",
) -> dict:
    import time as _time
    occ  = _build_occ_symbol(ticker, expiry, ctype, strike)
    bid  = round(fill * 0.995, 2)
    ask  = round(fill * 1.005, 2)
    return {
        "type": "timesale",
        "timesale": {
            "symbol":   occ,
            "last":     fill,
            "bid":      bid,
            "ask":      ask,
            "size":     size,
            "exch":     exchange,
            "exchange": exchange,
            "date":     int(_time.time() * 1000),
        },
    }


# ---------------------------------------------------------------------------
# Demo loop
# ---------------------------------------------------------------------------

async def _run_demo_loop(tickers: list[str]) -> None:
    """Core async loop — emits synthetic timesale envelopes on the bus."""
    rng = random.Random()

    _demo_stats["ticks_emitted"]   = 0
    _demo_stats["signals_emitted"] = 0
    _demo_stats["errors"]          = 0

    log.info("[demo_engine] Demo loop started with %d tickers", len(tickers))

    try:
        while True:
            await asyncio.sleep(rng.uniform(1.5, 4.0))

            ticker   = rng.choice(tickers)
            ctype    = rng.choice(["CALL", "PUT"])
            price    = rng.uniform(50, 600)
            strike   = _round_to_strike(price, ctype)
            expiry   = _nearest_friday(rng.randint(1, 8))
            fill     = round(rng.uniform(0.50, 20.0), 2)
            size     = rng.randint(5, 500)
            exchange = rng.choice(_EXCHANGES)

            envelope = _build_timesale_envelope(
                ticker=ticker, expiry=expiry, ctype=ctype,
                strike=strike, fill=fill, size=size, exchange=exchange,
            )

            try:
                from services.tradier_stream import _process_trade
                await _process_trade(envelope)
                _demo_stats["ticks_emitted"] += 1
            except Exception as e:
                _demo_stats["errors"] += 1
                log.warning("[demo_engine] _process_trade error: %s", e)

    except asyncio.CancelledError:
        log.info("[demo_engine] Demo loop cancelled cleanly")
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_running() -> bool:
    return _running and _task is not None and not _task.done()


def get_stats() -> dict:
    return {
        "running":          is_running(),
        "ticks_emitted":    _demo_stats["ticks_emitted"],
        "signals_emitted":  _demo_stats["signals_emitted"],
        "errors":           _demo_stats["errors"],
    }


async def start_demo(tickers: list[str] = None) -> dict:
    global _running, _task
    if is_running():
        log.info("[demo_engine] start_demo called but already running")
        return {"ok": True, "status": "already_running"}
    _tickers = tickers or _DEFAULT_TICKERS
    _running = True
    _task    = asyncio.create_task(_run_demo_loop(_tickers))
    log.info("[demo_engine] Demo engine started")
    return {"ok": True, "status": "started"}


async def stop_demo() -> dict:
    global _running, _task
    if not is_running():
        log.info("[demo_engine] stop_demo called but not running")
        return {"ok": True, "status": "already_stopped"}
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _running = False
    _task    = None
    log.info("[demo_engine] Demo engine stopped")
    return {"ok": True, "status": "stopped"}
