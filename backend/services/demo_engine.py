"""
services/demo_engine.py — Realistic 6-Layer Demo Engine

Generates synthetic options flow events in the EXACT format that Tradier
timesale streaming produces, feeding them through the full pipeline:

  Layer 1: OCC SymbolRegistry lookup (real OCC symbol strings generated)
  Layer 2: StreamManager queue format (timesale envelope)
  Layer 3: parse_tradier_trade() — same parser as live data
  Layer 4: Deduplication (C-019) — 5s TTL, 8s sweep window, no bucket
           boundary bug. Each trade burst prints on 2-4 exchanges within
           a 50-300ms window (widened from 20-80ms to cover TTL boundary
           testing). Exchange code passed as "exch" in inner timesale
           dict — matching real Tradier field name — so sweep detection
           in DedupCache correctly counts unique exchanges.
  Layer 5: Batched DB writes via persist_flow_event() — same path
  Layer 6: Supabase Realtime broadcast — same bus.publish_all()

The demo engine is fully toggle-able at runtime via:
  start_demo()  — begins emitting synthetic ticks
  stop_demo()   — cancels the task immediately
  demo_status() — returns current state + stats

Each "scenario" picks a real-format OCC symbol (e.g. TSLA  260620C00375000),
generates a coordinated burst of 3-8 ticks on different exchanges to trigger
the repetition accumulator threshold (min_trades=3, min_premium=$50K),
then lets the full signal engine produce a composite signal.

Admin API: routers/admin.py exposes POST /api/admin/demo/on|off

C-019 changes:
  - _build_timesale_envelope() now uses "exch" as the primary exchange key
    in the inner timesale dict (matching real Tradier field name). "exchange"
    kept as an alias for human readability / backward compat.
  - Inter-exchange delay widened from 20-80ms to 50-300ms to properly
    exercise the 5s TTL window and simulate MIAX/PHLX lag realistically.
  - get_stats() now includes dedup_stats() from flow_dedup for observability.

B-021 note:
  The demo engine bypasses StreamManager/StreamWorker entirely — it calls
  _process_trade() directly. The 200ms worker stagger is therefore not
  simulated or applied here. This is correct behaviour: the demo engine
  exists to test the signal pipeline, not the connection layer.
"""
import asyncio
import logging
import random
import time
from datetime import datetime, date, timedelta
from typing import Optional

from utils.dedup import flow_dedup  # C-019: dedup stats in demo reporting

log = logging.getLogger("demo_engine")

# ---------------------------------------------------------------------------
# Global state — toggled by admin API
# ---------------------------------------------------------------------------
_demo_task:  Optional[asyncio.Task] = None
_demo_running = False
_demo_stats = {
    "ticks_emitted":    0,
    "signals_generated": 0,
    "last_ticker":      None,
    "started_at":       None,
}

# Exchanges Tradier reports — real exchange codes seen in live data
_EXCHANGES = ["N", "C", "M", "Q", "X", "P", "W", "Z"]

# Representative tickers with realistic stock prices (updated periodically)
_SCENARIOS = [
    {"ticker": "TSLA",  "price": 245.0,  "iv": 0.68},
    {"ticker": "AAPL",  "price": 195.0,  "iv": 0.28},
    {"ticker": "NVDA",  "price": 875.0,  "iv": 0.52},
    {"ticker": "SPY",   "price": 527.0,  "iv": 0.17},
    {"ticker": "QQQ",   "price": 452.0,  "iv": 0.20},
    {"ticker": "MSFT",  "price": 415.0,  "iv": 0.25},
    {"ticker": "META",  "price": 585.0,  "iv": 0.32},
    {"ticker": "AMZN",  "price": 202.0,  "iv": 0.30},
    {"ticker": "GOOGL", "price": 172.0,  "iv": 0.27},
    {"ticker": "AMD",   "price": 165.0,  "iv": 0.48},
    {"ticker": "PLTR",  "price": 28.0,   "iv": 0.72},
    {"ticker": "COIN",  "price": 240.0,  "iv": 0.78},
]


def _nearest_friday(weeks_out: int = 4) -> date:
    """Return the nearest Friday N weeks from today — realistic expiry."""
    today = date.today()
    days_until_friday = (4 - today.weekday()) % 7
    if days_until_friday == 0:
        days_until_friday = 7
    return today + timedelta(days=days_until_friday + (weeks_out - 1) * 7)


def _round_to_strike(price: float, direction: str) -> float:
    """
    Round to nearest standard strike increment.
    Stocks < $50: $0.50 increments
    Stocks $50-$200: $1 increments
    Stocks > $200: $5 increments
    """
    if price < 50:
        inc = 0.5
    elif price < 200:
        inc = 1.0
    else:
        inc = 5.0

    if direction == "CALL":
        offset = random.choice([0, 1, 2, 3]) * inc   # ATM to slightly OTM
    else:
        offset = -random.choice([0, 1, 2, 3]) * inc  # ATM to slightly OTM

    base = round(price / inc) * inc
    return round(base + offset, 2)


def _build_occ_symbol(ticker: str, expiry: date, ctype: str, strike: float) -> str:
    """
    Build OCC option symbol string exactly as Tradier sends it:
      AAPL  260620C00195000
      TSLA  260620P00245000
      SPY   260620C00527000

    Format: {TICKER padded to 6}  {YYMMDD}{C|P}{strike * 1000 zero-padded to 8}
    """
    ticker_padded = ticker.ljust(6)
    date_str = expiry.strftime("%y%m%d")
    cp = "C" if ctype == "CALL" else "P"
    strike_int = int(round(strike * 1000))
    strike_str = str(strike_int).zfill(8)
    return f"{ticker_padded}{date_str}{cp}{strike_str}"


def _build_timesale_envelope(occ_symbol: str, ticker: str, fill: float,
                              bid: float, ask: float, size: int,
                              exchange: str, ts_ms: int) -> dict:
    """
    Build the EXACT Tradier timesale event envelope format:

      {
        "type": "timesale",
        "timesale": {
          "symbol":   "TSLA  260620C00375000",
          "last":     3.45,          <- fill price (NOT "price")
          "bid":      3.40,
          "ask":      3.50,
          "size":     50,
          "date":     1745529600000, <- epoch ms
          "open":     3.20,
          "high":     3.55,
          "low":      3.15,
          "close":    3.45,
          "exch":     "C",           <- PRIMARY: matches real Tradier field (C-019)
          "exchange": "C",           <- ALIAS: human-readable backward compat
          "vwap":     3.42
        }
      }

    C-019: "exch" is now the primary exchange field, matching the real Tradier
    timesale payload. _process_trade() reads trade_payload.get("exch") first,
    then falls back to trade_payload.get("exchange", ""). Both are set here
    so the envelope works with any consumer.
    """
    return {
        "type": "timesale",
        "timesale": {
            "symbol":   occ_symbol,
            "last":     fill,          # PRIMARY fill price field for timesale
            "bid":      bid,
            "ask":      ask,
            "size":     size,
            "date":     ts_ms,
            "open":     round(fill * random.uniform(0.97, 1.0), 2),
            "high":     round(fill * random.uniform(1.0, 1.03), 2),
            "low":      round(fill * random.uniform(0.94, 1.0), 2),
            "close":    fill,
            "exch":     exchange,      # C-019: primary — matches real Tradier field
            "exchange": exchange,      # alias — backward compat / human readable
            "vwap":     round(fill * random.uniform(0.995, 1.005), 2),
        },
    }


async def _run_demo_loop():
    """
    Core demo loop. For each scenario:
    1. Pick a ticker, expiry, strike, contract type
    2. Emit 3-8 ticks on different exchanges (same symbol, size, fill)
       within a 50-300ms window — replicating multi-exchange reporting
       with realistic MIAX/PHLX lag (C-019: widened from 20-80ms)
    3. Wait 2-6s before next scenario

    All ticks go through _process_trade() exactly like live data:
      → parse_tradier_trade()
      → flow_dedup.is_duplicate()   ← C-019: dedup now active
      → persist_flow_event()
      → RepetitionAccumulator.ingest()
      → build_composite() if threshold crossed
      → bus.publish_all()

    Dedup behaviour in demo:
      - First exchange tick for a burst → canonical, passes through
      - 2nd-4th exchange ticks (same occ+size+fill within 5s) → dropped
      - If 3+ unique exchanges report → trade_type upgraded to SWEEP
      - burst_fill uses ±0.5% variation per burst; with fill:.1f key
        rounding, fills like $3.45 and $3.46 both round to $3.5 and
        are correctly deduplicated as the same trade.

    B-021 note:
      Demo engine bypasses StreamWorker — _process_trade() is called
      directly. The 200ms worker stagger is not replicated here.
    """
    global _demo_running, _demo_stats
    from services.tradier_stream import _process_trade

    rng = random.Random()
    _demo_stats["started_at"] = datetime.utcnow().isoformat()
    log.info("[demo_engine] Demo engine started — emitting realistic Tradier timesale events")
    log.info("[demo_engine] Note: B-021 worker stagger not simulated (demo bypasses StreamWorker)")

    try:
        while _demo_running:
            scenario = rng.choice(_SCENARIOS)
            ticker   = scenario["ticker"]
            price    = scenario["price"] * rng.uniform(0.97, 1.03)  # small drift
            iv       = scenario["iv"]
            ctype    = rng.choice(["CALL", "PUT"])

            # Pick an expiry 2-8 weeks out (realistic DTE)
            weeks_out = rng.randint(2, 8)
            expiry    = _nearest_friday(weeks_out)
            strike    = _round_to_strike(price, ctype)

            occ_symbol = _build_occ_symbol(ticker, expiry, ctype, strike)

            # Realistic fill price around mid of synthetic spread
            fill_pct = rng.uniform(0.01, 0.08)
            fill     = round(price * fill_pct, 2)
            if fill < 0.05:
                fill = 0.05
            spread   = round(fill * 0.015, 2)  # ~1.5% spread
            bid      = round(fill - spread, 2)
            ask      = round(fill + spread, 2)

            # Size: target $100K-$5M premium
            target_prem = rng.randint(100_000, 5_000_000)
            size = max(1, int(target_prem / (fill * 100)))
            size = min(size, 5000)  # cap at 5000 contracts

            # Number of exchanges this trade prints on (dedup scenario)
            # C-019: 2-4 exchanges report same trade within 50-300ms window
            num_exchanges = rng.randint(2, 4)
            exchanges = rng.sample(_EXCHANGES, num_exchanges)

            # Number of tick bursts (1-3 bursts per scenario)
            num_bursts = rng.randint(1, 3)

            _demo_stats["last_ticker"] = f"{ticker} {ctype} ${strike}"

            for burst_idx in range(num_bursts):
                # Small fill variation per burst (same contract, slightly different fill)
                burst_fill = round(fill * rng.uniform(0.995, 1.005), 2)
                burst_bid  = round(burst_fill - spread, 2)
                burst_ask  = round(burst_fill + spread, 2)

                # Emit one tick per exchange
                # C-019: widened to 50-300ms to simulate real MIAX/PHLX lag
                base_ts_ms = int(time.time() * 1000)
                for ex_idx, exchange in enumerate(exchanges):
                    ts_ms = base_ts_ms + (ex_idx * rng.randint(50, 300))

                    envelope = _build_timesale_envelope(
                        occ_symbol=occ_symbol,
                        ticker=ticker,
                        fill=burst_fill,
                        bid=burst_bid,
                        ask=burst_ask,
                        size=size,
                        exchange=exchange,
                        ts_ms=ts_ms,
                    )

                    try:
                        await _process_trade(envelope)
                        _demo_stats["ticks_emitted"] += 1
                    except Exception as e:
                        log.warning(f"[demo_engine] _process_trade error: {e}")

                    # C-019: delay 50-300ms between exchange reports
                    await asyncio.sleep(rng.uniform(0.05, 0.30))

                # Delay between bursts within same scenario (0.5-2s)
                if burst_idx < num_bursts - 1:
                    await asyncio.sleep(rng.uniform(0.5, 2.0))

            # Inter-scenario delay (2-8 seconds)
            delay = rng.uniform(2.0, 8.0)
            log.info(
                f"[demo_engine] Emitted {num_bursts} bursts x {num_exchanges} exchanges "
                f"for {ticker} {ctype} ${strike} exp={expiry} "
                f"fill=${fill} size={size} prem=${fill*size*100:,.0f} "
                f"| next in {delay:.1f}s"
            )
            await asyncio.sleep(delay)

    except asyncio.CancelledError:
        log.info("[demo_engine] Demo engine cancelled")
        raise
    finally:
        _demo_running = False
        log.info(f"[demo_engine] Stopped. Total ticks emitted: {_demo_stats['ticks_emitted']}")


# ---------------------------------------------------------------------------
# Public API — called by admin router
# ---------------------------------------------------------------------------

def is_running() -> bool:
    return _demo_running and _demo_task is not None and not _demo_task.done()


def get_stats() -> dict:
    stats = {
        "running":           is_running(),
        "ticks_emitted":     _demo_stats["ticks_emitted"],
        "signals_generated": _demo_stats["signals_generated"],
        "last_ticker":       _demo_stats["last_ticker"],
        "started_at":        _demo_stats["started_at"],
    }
    stats.update(flow_dedup.dedup_stats())  # C-019: live dedup counters
    return stats


async def start_demo() -> dict:
    """Start the demo engine. Idempotent — no-op if already running."""
    global _demo_task, _demo_running, _demo_stats

    if is_running():
        return {"ok": False, "message": "Demo engine already running", "stats": get_stats()}

    _demo_stats = {
        "ticks_emitted":    0,
        "signals_generated": 0,
        "last_ticker":      None,
        "started_at":       None,
    }
    _demo_running = True
    _demo_task = asyncio.create_task(_run_demo_loop(), name="demo-engine")
    log.info("[demo_engine] Demo engine task created")
    return {"ok": True, "message": "Demo engine started", "stats": get_stats()}


async def stop_demo() -> dict:
    """Stop the demo engine immediately. Idempotent — no-op if not running."""
    global _demo_task, _demo_running

    if not is_running():
        return {"ok": False, "message": "Demo engine not running", "stats": get_stats()}

    _demo_running = False
    if _demo_task and not _demo_task.done():
        _demo_task.cancel()
        try:
            await _demo_task
        except asyncio.CancelledError:
            pass
    _demo_task = None
    log.info("[demo_engine] Demo engine stopped via admin API")
    return {"ok": True, "message": "Demo engine stopped", "stats": get_stats()}
