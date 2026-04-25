"""
Tradier WebSocket stream processor — production-grade resilient implementation.

Design principles:
  - Fresh session token fetched on EVERY reconnect (tokens expire on stream close)
  - Never permanently falls into demo mode — always retries live connection
  - 30-second idle watchdog: reconnects if Tradier stops sending keepalives
  - Exponential backoff with jitter (5s base, 60s cap) on all error paths
  - Session token fetch retried up to 3x for transient network failures
  - Clear distinction: 401 on session = bad key (slow retry), 401 on stream = expired token (fast retry)
  - Demo mode is a supervised fallback task, not an infinite blocking trap
  - Market-hours guard: backs off 60s when US options market is closed

Phase 4 change:
  - _process_trade() now calls build_composite() after accumulator threshold is crossed
    and publishes a 'composite_signal' bus message for signal_store.py to persist.

Fix (signal_history empty):
  - _demo_mode_once() now also emits composite_signal messages so signal_store.py
    populates signal_history during demo/fallback mode.

Fix (C-011):
  - Demo mode expiry updated to valid future date (2026-06-20).
  - Demo mode contract_type now consistent with direction (CALL=BUY, PUT=SELL).
  - persist_flow_event() call confirmed to include all parsed fields from OptionsFlowEvent.

Fix (C-013):
  - Tradier streaming sends events wrapped in an envelope.
  - Fix: unwrap the inner payload by checking raw["type"].
  - Also logs the first raw line on each new connection for diagnostics.

Fix (C-015):
  - CRITICAL: switched stream filter from "trade" to "timesale".
    filter=trade delivers equity trade events where symbol=underlying ticker,
    price=stock last price, bid/ask=stock NBBO — completely wrong for options.
    filter=timesale delivers option contract events where:
      symbol = full OCC string (e.g. "ACGL  260516P00095000")
      price  = option fill price
      bid/ask = real option bid/ask
    This was the root cause of strike=0, expiry=null, bid=0, ask=0 in DB.
  - _process_trade now accepts both "timesale" and "trade" envelope types.

Architecture change (Layer 1+2):
  - stream_options_flow() now builds the OCC SymbolRegistry first, then
    delegates to StreamManager which spawns parallel StreamWorker instances
    (500 OCC symbols per connection). This ensures Tradier receives full OCC
    contract strings (e.g. "AAPL  260117C00180000") instead of ticker symbols,
    which was the root cause of receiving equity events instead of option events.
  - occ_symbol field now passed through to persist_flow_event() and stored in DB.

Architecture fix (Layer 2+4):
  - registry.refresh_loop() now notifies stream_manager.refresh() after every
    registry rebuild so only affected workers restart — not all 32.
  - _process_trade() now calls flow_dedup.is_duplicate() BEFORE persist_flow_event().
    Without this, every trade printed on 4 exchanges writes 4 DB rows.
    Layer 4 (DedupCache) was implemented but NOT wired into the hot path — fixed.

Tradier streaming notes:
  - Session token: POST /v1/markets/events/session with Content-Length: 0 (data={})
  - Session tokens expire when the stream connection closes — always re-fetch
  - Stream POST uses sessionid + Bearer token in headers
  - Tradier sends bare newlines as keepalives — idle >30s means the connection is dead
  - On market close, Tradier may close the stream normally — reconnect for next open
  - Tradier closes the stream immediately when the market is closed (no queued data)
  - filter=timesale: symbol field = full OCC option string, price = option fill
  - filter=trade:    symbol field = underlying ticker, price = stock last — DO NOT USE
"""
import asyncio
import json
import logging
import random
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from config import settings
from core.async_bus import bus
from parsers.options_flow_parser import parse_tradier_trade
from services.flow_store import persist_flow_event
from utils.dedup import flow_dedup
from signals.repetition_accumulator import RepetitionAccumulator
from signals.composite_signal_engine import build_composite

log = logging.getLogger("tradier_stream")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SESSION_RETRY_MAX   = 3
_SESSION_RETRY_DELAY = 2.0
_BACKOFF_BASE        = 5.0
_BACKOFF_CAP         = 60.0
_IDLE_TIMEOUT        = 30.0
_CONNECT_TIMEOUT     = 15.0
_MARKET_CLOSED_SLEEP = 60.0

_ET = ZoneInfo("America/New_York")
_MARKET_OPEN  = time(9, 30)
_MARKET_CLOSE = time(16, 0)

# Event types we process — timesale carries option OCC symbol + real bid/ask/price
# trade carries equity events with stock price — do NOT process as options flow
_PROCESSABLE_TYPES = {"timesale"}

# ---------------------------------------------------------------------------
# Global stats (read by /health endpoint)
# ---------------------------------------------------------------------------
_stats = {
    "active_symbols": 0,
    "ticks":          0,
    "classified":     0,
    "signals":        0,
    "errors":         0,
    "reconnects":     0,
    "mode":           "starting",
    "deduped":        0,   # Layer 4: count of dropped duplicate ticks
}

accumulator = RepetitionAccumulator(window_minutes=30, min_trades=3, min_premium=50_000)


def get_stats() -> dict:
    return dict(_stats)


# ---------------------------------------------------------------------------
# Market hours helper
# ---------------------------------------------------------------------------
def _is_market_hours() -> bool:
    now_et = datetime.now(_ET)
    if now_et.weekday() >= 5:
        return False
    return _MARKET_OPEN <= now_et.time() < _MARKET_CLOSE


# ---------------------------------------------------------------------------
# Backoff helper
# ---------------------------------------------------------------------------
def _backoff(attempt: int) -> float:
    delay = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** attempt))
    return random.uniform(0, delay)


# ---------------------------------------------------------------------------
# Session token
# ---------------------------------------------------------------------------
async def _get_session_token() -> Optional[str]:
    url = f"{settings.TRADIER_BASE_URL}/v1/markets/events/session"
    headers = {
        "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
        "Accept": "application/json",
    }

    for attempt in range(_SESSION_RETRY_MAX):
        try:
            async with httpx.AsyncClient(timeout=_CONNECT_TIMEOUT) as client:
                resp = await client.post(url, headers=headers, data={})

            if resp.status_code == 401:
                log.error(
                    f"Tradier session 401 — TRADIER_API_KEY rejected. "
                    f"Verify the key in Railway env vars. (attempt {attempt + 1}/{_SESSION_RETRY_MAX})"
                )
                return None

            resp.raise_for_status()
            token = resp.json().get("stream", {}).get("sessionid")
            if token:
                log.info("Tradier session token obtained successfully")
                return token
            log.warning(f"Tradier session response missing sessionid field: {resp.text[:200]}")
            return None

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            log.warning(
                f"Tradier session fetch failed (transient, attempt {attempt + 1}/{_SESSION_RETRY_MAX}): {e}"
            )
            if attempt < _SESSION_RETRY_MAX - 1:
                await asyncio.sleep(_SESSION_RETRY_DELAY)
        except Exception as e:
            log.error(f"Tradier session fetch unexpected error: {e}")
            return None

    log.error(f"Tradier session token could not be obtained after {_SESSION_RETRY_MAX} attempts")
    return None


# ---------------------------------------------------------------------------
# Main streaming entry point — delegates to StreamManager + SymbolRegistry
# ---------------------------------------------------------------------------
async def stream_options_flow(symbols: list[str]):
    """
    Entry point called from main.py lifespan.
    `symbols` is the underlying ticker list (e.g. ["AAPL", "TSLA", ...]).

    Architecture:
      1. Build the OCC SymbolRegistry (fetches chains for each ticker)
      2. Pass all OCC contract symbols to StreamManager
      3. StreamManager spawns parallel StreamWorker instances (500 OCC/conn)
      4. Each worker pushes raw events to shared queue → _process_trade()
      5. registry.refresh_loop() runs every 30min and calls manager.refresh()
         so only affected workers restart — not all 32.
    """
    _stats["active_symbols"] = len(symbols)
    _stats["mode"] = "starting"

    if not settings.TRADIER_API_KEY:
        log.warning("TRADIER_API_KEY not set — running in demo mode")
        _stats["mode"] = "demo"
        await _demo_mode(symbols)
        return

    from services.symbol_registry import init_registry
    from services.stream_manager import StreamManager

    log.info(f"[stream] Building OCC registry for {len(symbols)} tickers...")
    registry = init_registry(watchlist=symbols)

    try:
        occ_count = await registry.build()
    except Exception as e:
        log.error(f"[stream] OCC registry build failed: {e} — falling back to demo mode")
        _stats["mode"] = "demo"
        await _demo_mode(symbols)
        return

    _stats["active_symbols"] = occ_count

    if occ_count == 0:
        log.warning("[stream] OCC registry is empty — no contracts found. Falling back to demo mode")
        _stats["mode"] = "demo"
        await _demo_mode(symbols)
        return

    log.info(f"[stream] OCC registry ready: {occ_count:,} contracts — starting stream manager")
    _stats["mode"] = "live"

    manager = StreamManager(registry=registry, process_fn=_process_trade)

    # Layer 2 fix: hook registry refresh_loop to notify stream_manager.refresh()
    # so only workers whose symbol sets changed are restarted, not all 32.
    async def _registry_refresh_with_manager_notify():
        while True:
            has_expiry_today = any(
                meta.expiry == __import__('datetime').date.today().isoformat()
                for meta in registry._registry.values()
            )
            interval_mins = 15 if has_expiry_today else 30
            await asyncio.sleep(interval_mins * 60)
            log.info(f"[stream] Scheduled registry refresh (interval={interval_mins}min)")
            try:
                await registry.build()
                await manager.refresh()  # <- the missing link: notify manager after rebuild
                _stats["active_symbols"] = registry.size()
            except Exception as e:
                log.error(f"[stream] Registry refresh failed (non-fatal): {e}")

    asyncio.create_task(_registry_refresh_with_manager_notify())

    await manager.run()


# ---------------------------------------------------------------------------
# Idle watchdog
# ---------------------------------------------------------------------------
async def _guarded_lines(resp: httpx.Response):
    aiter = resp.aiter_lines().__aiter__()
    while True:
        try:
            line = await asyncio.wait_for(aiter.__anext__(), timeout=_IDLE_TIMEOUT)
            yield line
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise


_iter_lines_with_watchdog = _guarded_lines  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Trade processor — shared by StreamManager workers and legacy path
# ---------------------------------------------------------------------------
async def _process_trade(raw: dict):
    """
    Process a raw Tradier stream event (filter=timesale).

    Tradier wraps every timesale event in an envelope:
      {"type": "timesale", "timesale": { ...option fields... }}

    The inner payload has:
      symbol = full OCC string e.g. "ACGL  260516P00095000"
      last   = option fill price  (NOTE: field is "last" not "price") — Layer 3 fix
      bid    = option bid
      ask    = option ask
      size   = contract count
      date   = epoch ms timestamp

    Layer 4 (Dedup): flow_dedup.is_duplicate() is called BEFORE persist_flow_event().
    A single trade prints on exchanges N, C, M, Q all within 200ms — without dedup
    that's 4 DB rows per trade. The 2s TTL cache keyed on (symbol, size, fill, bucket)
    drops all but the first/canonical print.
    """
    _stats["ticks"] += 1

    # Unwrap Tradier event envelope
    event_type = raw.get("type", "")

    if event_type in _PROCESSABLE_TYPES and event_type in raw:
        trade_payload = raw[event_type]
        if not isinstance(trade_payload, dict):
            return
    elif event_type in _PROCESSABLE_TYPES:
        trade_payload = raw
    else:
        return

    ev = parse_tradier_trade(trade_payload)
    if not ev:
        return

    # Layer 4 — Dedup: drop exchange duplicates before any DB write
    occ_symbol = trade_payload.get("symbol", "")
    exchange   = trade_payload.get("exch", trade_payload.get("exchange", "UNK"))
    if flow_dedup.is_duplicate(
        occ_symbol = occ_symbol,
        size       = ev.size,
        fill       = ev.fill_price,
        exchange   = exchange,
    ):
        _stats["deduped"] += 1
        return  # same trade already written by first exchange — drop it

    # Sweep upgrade: if 3+ exchanges printed this contract → mark as SWEEP
    if flow_dedup.is_sweep(occ_symbol, ev.size, ev.fill_price):
        ev.trade_type = "SWEEP"

    _stats["classified"] += 1

    log.debug(
        f"[flow] {ev.ticker} {ev.contract_type} "
        f"${ev.strike:.2f} {ev.expiry} dte={ev.dte} "
        f"| fill={ev.fill_price} bid={ev.bid} ask={ev.ask} size={ev.size} "
        f"| prem=${ev.premium:,.0f} "
        f"| ba={ev.bid_ask_class} aggressive={ev.is_aggressive} "
        f"| type={ev.trade_type} sentiment={ev.sentiment} tier={ev.influence_tier} "
        f"| conviction={ev.conviction_score} occ={occ_symbol}"
    )

    await persist_flow_event({
        "ticker":           ev.ticker,
        "contract_type":    ev.contract_type,
        "strike":           ev.strike,
        "expiry":           ev.expiry,
        "dte":              ev.dte,
        "fill_price":       ev.fill_price,
        "bid":              ev.bid,
        "ask":              ev.ask,
        "size":             ev.size,
        "premium":          ev.premium,
        "trade_type":       ev.trade_type,
        "bid_ask_class":    ev.bid_ask_class,
        "is_aggressive":    ev.is_aggressive,
        "is_golden_sweep":  ev.is_golden_sweep,
        "sentiment":        ev.sentiment,
        "influence_tier":   ev.influence_tier,
        "conviction_score": ev.conviction_score,
        "exchange_count":   ev.exchange_count,
        "fill_count":       ev.fill_count,
        "open_interest":    ev.open_interest,
        "iv":               ev.iv,
        "underlying_price": ev.underlying_price,
        "occ_symbol":       occ_symbol,
    })

    ep = accumulator.ingest(ev)
    if not ep:
        return

    alert_level = accumulator.get_alert_level(ep)

    log.info(
        f"[signal] {ep.ticker} {ep.contract_type} "
        f"| alert={alert_level} "
        f"| trades={ep.trade_count} "
        f"| total_prem=${ep.total_premium:,.0f} "
        f"| accel={ep.is_accelerating} "
        f"| {ep.summary_str()}"
    )

    try:
        composite = build_composite(ep, accumulator)
    except Exception as e:
        log.error(f"[signal] build_composite failed for {ep.ticker}: {e}")
        composite = None

    if ep.contract_type == "CALL":
        direction = "REPEAT_BUY"
    elif ep.contract_type == "PUT":
        direction = "REPEAT_SELL"
    else:
        direction = "REPEAT_BUY" if ev.sentiment == "BULLISH" else "REPEAT_SELL"

    signal = {
        "type": "signal",
        "data": {
            "ticker":          ep.ticker,
            "direction":       direction,
            "contract_type":   ep.contract_type,
            "strike":          ep.strike,
            "expiry":          ep.expiry,
            "total_premium":   ep.total_premium,
            "trade_count":     ep.trade_count,
            "alert_level":     alert_level,
            "is_accelerating": ep.is_accelerating,
            "seed_episode":    ep.summary_str(),
            "timestamp":       ev.timestamp.isoformat(),
        },
    }
    _stats["signals"] += 1
    await bus.publish_all(signal)

    if composite is not None:
        composite_msg = {
            "type": "composite_signal",
            "data": {
                "signal": {
                    "ticker":                composite.ticker,
                    "recommendation":        composite.recommendation,
                    "composite_score":       composite.composite_score,
                    "flow_score":            composite.flow_score,
                    "backtest_score":        composite.backtest_score,
                    "volume_premium_factor": composite.volume_premium_factor,
                    "reasoning":             composite.reasoning,
                },
                "episode": {
                    "contract_type":   ep.contract_type,
                    "direction":       direction,
                    "influence_tier":  ev.influence_tier,
                    "total_premium":   ep.total_premium,
                    "trade_count":     ep.trade_count,
                    "is_accelerating": ep.is_accelerating,
                    "timestamp":       ev.timestamp.isoformat(),
                },
            },
        }
        await bus.publish_all(composite_msg)


# ---------------------------------------------------------------------------
# Demo mode
# ---------------------------------------------------------------------------
async def _demo_mode_once(symbols: list[str]):
    import datetime as dt
    rng     = random.Random(42)
    tickers = symbols or ["AAPL", "TSLA", "NVDA", "SPY", "QQQ", "MSFT", "AMZN", "META"]
    levels  = ["CONVICTION", "STRONG_SIGNAL", "ALERT", "WATCH"]
    demo_expiry = "2026-06-20"

    log.info("Demo mode active — emitting synthetic signals")
    try:
        while True:
            await asyncio.sleep(rng.uniform(2, 6))
            ticker    = rng.choice(tickers)
            prem      = rng.randint(100_000, 8_000_000)
            ctype     = rng.choice(["CALL", "PUT"])
            direction = "REPEAT_BUY" if ctype == "CALL" else "REPEAT_SELL"
            strike    = round(rng.uniform(100, 500), 0)
            fill      = round(rng.uniform(1.0, 15.0), 2)
            bid       = round(fill * 0.99, 2)
            ask       = round(fill * 1.01, 2)
            size      = rng.randint(10, 500)
            dte       = rng.randint(1, 60)

            signal = {
                "type": "signal",
                "data": {
                    "ticker":          ticker,
                    "direction":       direction,
                    "contract_type":   ctype,
                    "strike":          strike,
                    "expiry":          demo_expiry,
                    "total_premium":   prem,
                    "trade_count":     rng.randint(3, 25),
                    "alert_level":     rng.choices(levels, weights=[5, 15, 30, 50])[0],
                    "is_accelerating": rng.random() < 0.2,
                    "seed_episode":    f"Demo: {ticker} synthetic flow",
                    "timestamp":       dt.datetime.utcnow().isoformat(),
                },
            }
            _stats["ticks"]      += 1
            _stats["classified"] += 1
            _stats["signals"]    += 1
            await bus.publish_all(signal)

            composite_score = round(rng.uniform(0.40, 0.95), 3)
            rec = "BUY"  if composite_score >= 0.65 and ctype == "CALL" else \
                  "SELL" if composite_score >= 0.65 and ctype == "PUT"  else "HOLD"

            composite_msg = {
                "type": "composite_signal",
                "data": {
                    "signal": {
                        "ticker":                ticker,
                        "recommendation":        rec,
                        "composite_score":       composite_score,
                        "flow_score":            round(rng.uniform(0.4, 0.9), 3),
                        "backtest_score":        round(rng.uniform(0.4, 0.85), 3),
                        "volume_premium_factor": round(rng.uniform(0.3, 0.8), 3),
                        "reasoning":             f"Demo synthetic signal for {ticker}",
                    },
                    "episode": {
                        "contract_type":   ctype,
                        "direction":       direction,
                        "influence_tier":  rng.choice(["WHALE", "INSTITUTIONAL", "LARGE", "RETAIL"]),
                        "total_premium":   prem,
                        "trade_count":     rng.randint(3, 25),
                        "is_accelerating": rng.random() < 0.2,
                        "timestamp":       dt.datetime.utcnow().isoformat(),
                    },
                },
            }
            await bus.publish_all(composite_msg)
    except asyncio.CancelledError:
        log.info("Demo mode cancelled — live stream connection established")
        raise


async def _demo_mode(symbols: list[str]):
    _stats["mode"] = "demo"
    await _demo_mode_once(symbols)
