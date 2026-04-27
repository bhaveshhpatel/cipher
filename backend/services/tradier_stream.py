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

Fix (C-018) — Synthetic Quote Tagging:
  - is_synthetic_quote is now forwarded from OptionsFlowEvent through
    _process_trade() into the persist_flow_event() dict.
  - Rows where bid=ask=0 and spread was synthesised are tagged in the DB.

Fix (C-019) — Dedup TTL & Sweep Overhaul (Layer 4):
  - flow_dedup (DedupCache) is now actively called in _process_trade().
    Previously the singleton was instantiated in utils/dedup.py but never
    imported here — dedup was completely inert in production.
  - exchange code read from trade_payload with "exch"/"exchange" fallback
    to handle both real Tradier feed ("exch") and demo engine ("exchange").
  - If flow_dedup.is_duplicate() returns True -> event dropped, _stats["deduped"]
    incremented. No DB write, no accumulator ingest.
  - If is_sweep() returns True after canonical pass -> ev.trade_type upgraded to
    "SWEEP" and ev.exchange_count set to the real unique-exchange count.
  - _stats now includes "deduped" counter exposed via /health endpoint.
  - DedupCache.dedup_stats() merged into get_stats() for full observability.

Fix (C-020) — Dedup Clock Mismatch:
  - arrival_ts was set using _time.monotonic() but DedupCache stores first_seen
    as time.time() (wall-clock). The TTL check (now - first_seen) < 5.0 used
    monotonic (~8431s) minus wall-clock (~1.77e9) = always a large negative,
    which is always < 5.0. Result: cache entries NEVER expired via the hot path
    and every re-print of the same OCC/size/fill was permanently deduped.
  - Fix: arrival_ts = _time.time() so both sides of the TTL comparison are
    wall-clock epoch values.

Fix (C-002) — Persist Gate:
  - persist_flow_event() was called BEFORE accumulator.ingest(), writing every
    dedup-passing tick to DB regardless of whether it crossed the episode
    threshold. Sub-threshold retail noise polluted flow_events and burned write
    capacity.
  - Fix: persist_flow_event() is now called AFTER accumulator.ingest(). Only
    ticks that are part of a qualifying episode (>=3 trades, >=$50K premium)
    are written to flow_events. Sub-threshold ticks return early with no DB write.

Fix (C-003) — Retroactive Sweep Upgrade:
  - When the canonical print was already written to flow_events as 'BTO',
    subsequent duplicate ticks from MIAX/PHLX confirming sweep threshold
    (3+ exchanges) were silently dropped. The DB row stayed as 'BTO' forever.
  - Fix: on the DUPLICATE path, call get_exchange_count() after dedup returns
    True. If count == sweep_min_exchanges exactly (threshold just crossed),
    fire upgrade_to_sweep_in_db() as a background asyncio.create_task().
    This issues a PATCH to flow_events retroactively setting trade_type='SWEEP'.
    The count==sweep_min guard prevents repeated UPDATE calls for 4th, 5th
    exchange echoes.

B-008 — Stream Health:
  - _stats gains last_tick_at (float epoch, updated on every classified tick)
    and last_reconnect_at (float epoch, updated on every reconnect attempt).
  - _stream_start_at records process start time for uptime_seconds calculation.
  - get_stats() exposes all counters + timestamps for GET /health/stream.

B4-001 — start_stream alias:
  - Tests reference tradier_stream.start_stream([...]); the canonical entry
    point is stream_options_flow(). Added alias at module level so tests and
    any future callers resolve without breaking main.py lifespan usage.

Tradier streaming notes:
  - Session token: POST /v1/markets/events/session with Content-Length: 0 (data={})
  - Session tokens expire when the stream connection closes — always re-fetch
  - Stream POST uses sessionid + Bearer token in headers
  - Tradier sends bare newlines as keepalives — idle >30s means the connection is dead
  - On market close, Tradier may close the stream normally — reconnect for next open
  - Tradier closes the stream immediately when the market is closed (no queued data)
  - filter=timesale: symbol field = full OCC option string, price = option fill
  - filter=trade:    symbol field = underlying ticker, price = stock last — DO NOT USE
  - exch field in timesale payload: single char exchange code (C=CBOE, M=MIAX,
    Q=NASDAQ, N=NYSE, X=PHLX, B=BATO). Used for sweep detection in DedupCache.

NOTE (2026-04-25):
  - Automatic _demo_mode fallback in stream_options_flow() is DISABLED.
  - The _demo_mode/_demo_mode_once functions are preserved below for future use.
  - To re-enable: uncomment the _demo_mode(...) call sites in stream_options_flow().
  - Use the admin panel (/admin) to run the demo engine manually instead.
"""
import asyncio
import logging
import random
import time as _time
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from config import settings
from core.async_bus import bus
from parsers.options_flow_parser import parse_tradier_trade
from services.flow_store import persist_flow_event, upgrade_to_sweep_in_db
from signals.repetition_accumulator import RepetitionAccumulator
from signals.composite_signal_engine import build_composite
from utils.dedup import flow_dedup   # C-019: Layer 4 dedup now active

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
# Global stats (read by /health/stream endpoint)
# B-008: added last_tick_at, last_reconnect_at; _stream_start_at for uptime
# ---------------------------------------------------------------------------
_stream_start_at: float = _time.time()

_stats = {
    "active_symbols":    0,
    "ticks":             0,
    "classified":        0,
    "deduped":           0,   # C-019: events dropped by dedup cache
    "signals":           0,
    "errors":            0,
    "reconnects":        0,
    "mode":              "starting",
    "last_tick_at":      None,       # B-008: epoch float, set on each classified tick
    "last_reconnect_at": None,       # B-008: epoch float, set on each reconnect attempt
}

accumulator = RepetitionAccumulator(window_minutes=30, min_trades=3, min_premium=50_000)


def get_stats() -> dict:
    stats = dict(_stats)
    stats["uptime_seconds"] = round(_time.time() - _stream_start_at, 1)  # B-008
    stats.update(flow_dedup.dedup_stats())  # C-019: merge dedup counters
    return stats


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
# Main streaming entry point — now delegates to StreamManager + SymbolRegistry
# ---------------------------------------------------------------------------
async def stream_options_flow(symbols: list[str]):
    """
    Entry point called from main.py lifespan.
    `symbols` is the underlying ticker list (e.g. ["AAPL", "TSLA", ...]).

    Automatic demo-mode fallback is DISABLED (2026-04-25).
    If Tradier key is missing or OCC registry is empty, the stream simply
    idles and logs a warning instead of emitting synthetic data.
    To re-enable demo fallback: uncomment the _demo_mode(...) call sites below.
    Use the admin panel to start the demo engine manually.
    """
    _stats["active_symbols"] = len(symbols)
    _stats["mode"] = "starting"

    if not settings.TRADIER_API_KEY:
        log.warning("TRADIER_API_KEY not set — stream idle. Use admin panel to start demo engine.")
        _stats["mode"] = "idle"
        return

    from services.symbol_registry import init_registry
    from services.stream_manager import StreamManager

    log.info(f"[stream] Building OCC registry for {len(symbols)} tickers...")
    registry = init_registry(watchlist=symbols)

    try:
        occ_count = await registry.build()
    except Exception as e:
        log.error(f"[stream] OCC registry build failed: {e} — stream idle. Use admin panel to start demo engine.")
        _stats["mode"] = "idle"
        return

    _stats["active_symbols"] = occ_count

    if occ_count == 0:
        log.warning("[stream] OCC registry is empty — stream idle. Use admin panel to start demo engine.")
        _stats["mode"] = "idle"
        return

    log.info(f"[stream] OCC registry ready: {occ_count:,} contracts — starting stream manager")
    _stats["mode"] = "live"

    asyncio.create_task(registry.refresh_loop())

    manager = StreamManager(registry=registry, process_fn=_process_trade)
    await manager.run()


start_stream = stream_options_flow


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


# ---------------------------------------------------------------------------
# Trade processor — shared by StreamManager workers and demo engine
# ---------------------------------------------------------------------------
async def _process_trade(raw: dict):
    """
    Process a raw Tradier stream event (filter=timesale).

    C-003 — Retroactive Sweep Upgrade:
      On the DUPLICATE path (is_duplicate=True), call get_exchange_count().
      If the count equals sweep_min_exchanges exactly (default 3), this is
      the tick that just pushed the exchange count over the sweep threshold
      for the first time. Fire upgrade_to_sweep_in_db() as a background
      task so the canonical row already in DB gets patched to trade_type='SWEEP'.
      The count==3 guard prevents repeated UPDATE calls on 4th+ exchange echoes.
    """
    _stats["ticks"] += 1

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

    # ------------------------------------------------------------------
    # Layer 4: Deduplication (C-019 + C-020)
    # ------------------------------------------------------------------
    occ_symbol = trade_payload.get("symbol", "")
    exchange   = trade_payload.get("exch") or trade_payload.get("exchange", "")
    arrival_ts = _time.time()

    if flow_dedup.is_duplicate(
        occ_symbol=occ_symbol,
        size=ev.size,
        fill=ev.fill_price,
        exchange=exchange,
        ts=arrival_ts,
    ):
        _stats["deduped"] += 1
        log.debug(
            f"[dedup] dropped duplicate: {occ_symbol} size={ev.size} "
            f"fill={ev.fill_price} exch={exchange}"
        )

        # ------------------------------------------------------------------
        # C-003: Retroactive sweep upgrade.
        # Check if THIS duplicate tick is the one that just crossed the sweep
        # threshold (exchange_count transitions to exactly sweep_min_exchanges).
        # Fire an async DB UPDATE to patch the canonical row from 'BTO' -> 'SWEEP'.
        # count > sweep_min on 4th/5th echoes — guard prevents duplicate UPDATEs.
        # ------------------------------------------------------------------
        exch_count = flow_dedup.get_exchange_count(occ_symbol, ev.size, ev.fill_price)
        if exch_count == flow_dedup._sweep_min:
            log.info(
                f"[sweep] threshold just crossed — retroactive upgrade: "
                f"{occ_symbol} size={ev.size} fill={ev.fill_price} "
                f"exchanges={exch_count}"
            )
            asyncio.create_task(
                upgrade_to_sweep_in_db(
                    occ_symbol=occ_symbol,
                    fill_price=ev.fill_price,
                    size=ev.size,
                )
            )

        return

    # Canonical print — check for sweep upgrade (pattern already established)
    if flow_dedup.is_sweep(occ_symbol, ev.size, ev.fill_price):
        real_exch_count = flow_dedup.get_exchange_count(occ_symbol, ev.size, ev.fill_price)
        if ev.trade_type != "SWEEP":
            log.debug(
                f"[dedup] sweep upgrade: {occ_symbol} "
                f"{real_exch_count} exchanges — {ev.trade_type} -> SWEEP"
            )
            ev.trade_type = "SWEEP"
        ev.exchange_count = real_exch_count
    # ------------------------------------------------------------------

    _stats["classified"] += 1
    _stats["last_tick_at"] = _time.time()

    log.debug(
        f"[flow] {ev.ticker} {ev.contract_type} "
        f"${ev.strike:.2f} {ev.expiry} dte={ev.dte} "
        f"| fill={ev.fill_price} bid={ev.bid} ask={ev.ask} size={ev.size} "
        f"| prem=${ev.premium:,.0f} "
        f"| ba={ev.bid_ask_class} aggressive={ev.is_aggressive} "
        f"| type={ev.trade_type} exch={exchange} exch_count={ev.exchange_count} "
        f"| sentiment={ev.sentiment} tier={ev.influence_tier} "
        f"| conviction={ev.conviction_score} occ={occ_symbol} "
        f"| synthetic_quote={ev.is_synthetic_quote}"
    )

    # C-002: Gate DB write on accumulator threshold
    ep = accumulator.ingest(ev)
    if not ep:
        return

    await persist_flow_event({
        "ticker":               ev.ticker,
        "contract_type":        ev.contract_type,
        "strike":               ev.strike,
        "expiry":               ev.expiry,
        "dte":                  ev.dte,
        "fill_price":           ev.fill_price,
        "bid":                  ev.bid,
        "ask":                  ev.ask,
        "size":                 ev.size,
        "premium":              ev.premium,
        "trade_type":           ev.trade_type,
        "bid_ask_class":        ev.bid_ask_class,
        "is_aggressive":        ev.is_aggressive,
        "is_golden_sweep":      ev.is_golden_sweep,
        "sentiment":            ev.sentiment,
        "influence_tier":       ev.influence_tier,
        "conviction_score":     ev.conviction_score,
        "exchange_count":       ev.exchange_count,
        "fill_count":           ev.fill_count,
        "open_interest":        ev.open_interest,
        "iv":                   ev.iv,
        "underlying_price":     ev.underlying_price,
        "occ_symbol":           occ_symbol,
        "is_synthetic_quote":   ev.is_synthetic_quote,
    })

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
# Demo mode — DISABLED as automatic fallback (2026-04-25)
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
            _stats["last_tick_at"] = _time.time()
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

            _ = (bid, ask, size, dte)
    except asyncio.CancelledError:
        log.info("Demo mode cancelled — live stream connection established")
        raise


async def _demo_mode(symbols: list[str]):
    _stats["mode"] = "demo"
    await _demo_mode_once(symbols)
