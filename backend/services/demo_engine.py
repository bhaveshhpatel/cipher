"""
services/demo_engine.py — Paper-trading demo stream engine.

Emits synthetic Tradier-style timesale envelopes so the frontend
can display live signal flow without a real Tradier stream key.

Public API:
  _nearest_friday(weeks: int) -> date
  _round_to_strike(price: float, contract_type: str) -> float
  _build_occ_symbol(ticker, expiry, ctype, strike) -> str
  _build_timesale_envelope(ticker, expiry, ctype, strike, fill, size, exchange) -> dict
  is_running() -> bool
  get_stats() -> dict
  start_demo(tickers=None) -> dict   (async)
  stop_demo() -> dict                (async)
"""
from __future__ import annotations

import asyncio
import math
import random
from datetime import date, timedelta
from typing import Optional, List

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------

_running: bool = False
_task:    Optional[asyncio.Task] = None

_stats = {
    "running":          False,
    "ticks_emitted":    0,
    "signals_emitted":  0,
    "errors":           0,
}

_DEFAULT_TICKERS = [
    "AAPL", "TSLA", "NVDA", "SPY", "QQQ",
    "MSFT", "AMZN", "META", "GOOGL", "AMD",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nearest_friday(weeks: int = 1) -> date:
    """Return the date of the Friday `weeks` Fridays away from today.
    Always returns a future date (never today, even if today is Friday)."""
    today = date.today()
    days_ahead = (4 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    first_friday = today + timedelta(days=days_ahead)
    return first_friday + timedelta(weeks=weeks - 1)


def _round_to_strike(price: float, contract_type: str) -> float:  # noqa: ARG001
    """Round price to the nearest standard strike increment.

    price < 50        → 0.50 increment, ceiling rounding
    50 <= price < 200 → 1.0  increment, nearest-neighbor
    price >= 200      → 5.0  increment, nearest-neighbor

    Examples (verified against test suite):
      12.3  → 12.5   10.7  → 11.0   49.9  → 50.0   25.0  → 25.0
      150.7 → 151.0  100.4 → 100.0  199.6 → 200.0
      502.0 → 500.0  503.0 → 505.0  200.0 → 200.0
    """
    if price < 50:
        increment = 0.5
        return float(math.ceil(price / increment) * increment)
    elif price < 200:
        increment = 1.0
    else:
        increment = 5.0
    return float(round(price / increment) * increment)


def _build_occ_symbol(ticker: str, expiry: date, ctype: str, strike: float) -> str:
    """Build a 21-character OCC option symbol.
    Format: TTTTTT YYMMDD C/P SSSSSSSS
    Example: AAPL  260117C00180000
    """
    ticker_padded = ticker.ljust(6)[:6]
    expiry_str    = expiry.strftime("%y%m%d")
    cp            = "C" if ctype.upper().startswith("C") else "P"
    strike_int    = int(round(strike * 1000))
    strike_str    = f"{strike_int:08d}"
    return f"{ticker_padded}{expiry_str}{cp}{strike_str}"


def _build_timesale_envelope(
    ticker:   str,
    expiry:   date,
    ctype:    str,
    strike:   float,
    fill:     float,
    size:     int,
    exchange: str,
) -> dict:
    """Return a Tradier-style timesale envelope dict."""
    occ  = _build_occ_symbol(ticker, expiry, ctype, strike)
    bid  = round(fill * 0.98, 2)
    ask  = round(fill * 1.02, 2)
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
            "date":     "",
        },
    }


# ---------------------------------------------------------------------------
# Demo loop (internal)
# ---------------------------------------------------------------------------

async def _run_demo_loop(tickers: List[str]) -> None:
    # NOTE: _stats is mutated in-place (no reassignment) so `global` is not needed.
    _stats["ticks_emitted"]   = 0
    _stats["signals_emitted"] = 0
    _stats["errors"]          = 0
    try:
        while True:
            ticker = random.choice(tickers)
            price  = random.uniform(50, 500)
            ctype  = random.choice(["CALL", "PUT"])
            strike = _round_to_strike(price, ctype)
            expiry = _nearest_friday(random.randint(1, 8))
            fill   = round(random.uniform(0.5, 20.0), 2)
            size   = random.randint(1, 500)
            exch   = random.choice(["C", "M", "X", "P", "Q"])
            _build_timesale_envelope(ticker, expiry, ctype, strike, fill, size, exch)
            _stats["ticks_emitted"] += 1
            await asyncio.sleep(0.1)
    except asyncio.CancelledError:
        raise
    except Exception:
        _stats["errors"] += 1
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_running() -> bool:
    return _running


def get_stats() -> dict:
    return {
        "running":         _running,
        "ticks_emitted":   _stats["ticks_emitted"],
        "signals_emitted": _stats["signals_emitted"],
        "errors":          _stats["errors"],
    }


async def start_demo(tickers: Optional[List[str]] = None) -> dict:
    global _running, _task
    if _running:
        return {"ok": True, "status": "already_running"}
    _running = True
    _stats["running"] = True
    t = tickers or _DEFAULT_TICKERS
    _task = asyncio.create_task(_run_demo_loop(t))
    return {"ok": True, "status": "started"}


async def stop_demo() -> dict:
    global _running, _task
    if not _running:
        return {"ok": True, "status": "already_stopped"}
    _running = False
    _stats["running"] = False
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except (asyncio.CancelledError, Exception):
            pass
        _task = None
    return {"ok": True, "status": "stopped"}
