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
  - stream_options_flow() now accepts an optional `registry` parameter so
    main.py lifespan can pass the already-initialised registry, avoiding
    a duplicate init_registry() + build() call (fix D-001).
  - When a registry is provided, stream_options_flow() waits until
    registry.is_ready() before spawning StreamManager workers, instead
    of building its own copy from scratch.
  - occ_symbol field now passed through to persist_flow_event() and stored in DB.

Fix D-001 (2026-04-27) — Duplicate build():
  stream_options_flow() was calling init_registry() + registry.build() every
  time, creating a SECOND SymbolRegistry instance independent of the one in
  main.py lifespan. Both builds ran concurrently at startup, doubling chain
  API calls and producing two separate in-memory registries. Fixed by passing
  the existing registry from lifespan; stream waits for is_ready().

Fix D-002 (2026-04-27) — Double refresh_loop():
  stream_options_flow() also spawned asyncio.create_task(registry.refresh_loop()),
  duplicating the one already launched in main.py lifespan. Both loops competed
  for _build_lock and doubled scheduled refresh frequency. The create_task call
  is removed; lifecycle ownership stays in main.py.

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
  - Double-dispatch guard: _sweep_upgrade_dispatched set ensures only one
    create_task fires even when concurrent workers see count==sweep_min.

Fix (C-007) — Signal Spam:
  - ingest() returned ep on every post-threshold call. 32 workers x N ticks
    = signal flood on the bus and duplicate flow_episodes rows in DB.
  - Fix: RepetitionAccumulator tracks last_signal_at per episode with a
    5-minute cooldown. ingest() returns None during cooldown window.

Fix (C-008) — Decouple Persist Tier from Signal Tier:
  - With C-007 cooldown active, ingest() returned None during cooldown —
    persist_flow_event() was also suppressed. Ticks 4-N never wrote to
    flow_events, creating a backtesting gap.
  - Fix: _process_trade now calls accumulator.ingest_tick(ev) for the
    persist gate and accumulator.get_signal(ev.timestamp, persist_ep) for
    the bus/signal gate independently.
  - persist_flow_event() fires on every qualifying tick (above threshold).
  - bus.publish_all() fires only when cooldown passes.
  - Full episode tick history now recoverable from flow_events.

B-008 — Stream Health:
  - _stats gains last_tick_at (float epoch, updated on every classified tick)
    and last_reconnect_at (float epoch, updated on every reconnect attempt).
  - _stream_start_at records process start time for uptime_seconds calculation.
  - get_stats() exposes all counters + timestamps for GET /health/stream.

B4-001 — start_stream alias:
  - Tests reference tradier_stream.start_stream([...]); the canonical entry
    point is stream_options_flow(). Added alias at module level so tests and
    any future callers resolve without breaking main.py lifespan usage.

Fix (concurrent safety, issues #1-#5):
  - ingest_tick() and get_signal() are now async (per-key asyncio.Lock).
    All call sites in _process_trade updated with await.
  - persist_flow_event() wrapped in asyncio.wait_for(timeout=2.0) so a
    slow DB insert cannot stall the event loop for the worker coroutine.
  - _sweep_upgrade_dispatched: set[str] guards the C-003 retroactive upgrade
    against double create_task when concurrent workers see count==sweep_min.

Fix (issue #6 — composite_errors observability):
  - _stats gains composite_errors counter (separate from generic errors).
  - build_composite failures increment composite_errors, NOT errors, so
    DB/timeout errors and signal-engine failures are independently queryable
    at /health/stream without one masking the other.

Fix (issue #3 gap — sub-threshold episode eviction):
  - RepetitionAccumulator.ingest_tick() now evicts the episode key from
    _episodes whenever post-prune events list empties. This bounds memory
    to contracts active within the last window_minutes.

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
from typing import Optional, Set
from zoneinfo import ZoneInfo

import httpx

from config import settings
from core.async_bus import bus
from parsers.options_flow_parser import parse_tradier_trade
from services.flow_store import persist_flow_event, upgrade_to_sweep_in_db
from signals.repetition_accumulator import RepetitionAccumulator
from signals.composite_signal_engine import build_composite
from utils.dedup import flow_dedup

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
_PERSIST_TIMEOUT     = 2.0

# How long to wait for the background build() to complete before
# streaming starts. We poll every 500ms up to this limit.
_REGISTRY_READY_TIMEOUT_S = 1800.0  # 30 min (full cold-start upper bound)
_REGISTRY_READY_POLL_S    = 0.5

_ET = ZoneInfo("America/New_York")
_MARKET_OPEN  = time(9, 30)
_MARKET_CLOSE = time(16, 0)

_PROCESSABLE_TYPES = {"timesale"}

# ---------------------------------------------------------------------------
# Global stats
# ---------------------------------------------------------------------------
_stream_start_at: float = _time.time()

_stats = {
    "active_symbols":    0,
    "ticks":             0,
    "classified":        0,
    "deduped":           0,
    "signals":           0,
    "errors":            0,
    "composite_errors":  0,
    "reconnects":        0,
    "mode":              "starting",
    "last_tick_at":      None,
    "last_reconnect_at": None,
}

accumulator = RepetitionAccumulator(window_minutes=30, min_trades=3, min_premium=50_000)

_sweep_upgrade_dispatched: Set[str] = set()


def get_stats() -> dict:
    stats = dict(_stats)
    stats["uptime_seconds"] = round(_time.time() - _stream_start_at, 1)
    stats.update(flow_dedup.dedup_stats())
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
# Main streaming entry point
# ---------------------------------------------------------------------------
async def stream_options_flow(
    symbols: list[str],
    registry=None,   # D-001: accept pre-built registry from lifespan
):
    """
    Main entry point for the Tradier options stream.

    D-001: When `registry` is provided (passed from main.py lifespan), skip
    init_registry() and build() entirely. Instead, wait until
    registry.is_ready() is True (background build complete) before spawning
    StreamManager workers. This eliminates the duplicate build() that was
    causing double chain API calls and two independent SymbolRegistry instances.

    When `registry` is None (standalone / test usage), fall back to the
    original behaviour of building a fresh registry inline.
    """
    _stats["active_symbols"] = len(symbols)
    _stats["mode"] = "starting"

    if not settings.TRADIER_API_KEY:
        log.warning("TRADIER_API_KEY not set — stream idle. Use admin panel to start demo engine.")
        _stats["mode"] = "idle"
        return

    from services.stream_manager import StreamManager

    if registry is not None:
        # D-001: registry owned and built by lifespan — just wait for it
        log.info(
            "[stream] Registry provided by lifespan (is_ready=%s, %d OCC symbols). "
            "Waiting for background build to complete before spawning workers...",
            registry.is_ready(), registry.size(),
        )
        waited = 0.0
        while not registry.is_ready() and waited < _REGISTRY_READY_TIMEOUT_S:
            await asyncio.sleep(_REGISTRY_READY_POLL_S)
            waited += _REGISTRY_READY_POLL_S

        if not registry.is_ready():
            log.error(
                "[stream] Registry still not ready after %.0fs — "
                "stream idle. Use admin panel to start demo engine.",
                _REGISTRY_READY_TIMEOUT_S,
            )
            _stats["mode"] = "idle"
            return

        log.info(
            "[stream] Registry ready: %d OCC contracts (waited=%.1fs) — "
            "starting stream manager",
            registry.size(), waited,
        )
    else:
        # Standalone / test path — build our own registry
        from services.symbol_registry import init_registry as _init_registry
        log.info(f"[stream] Building OCC registry for {len(symbols)} tickers...")
        registry = _init_registry(watchlist=symbols)
        try:
            occ_count = await registry.build()
        except Exception as e:
            log.error(
                f"[stream] OCC registry build failed: {e} — "
                "stream idle. Use admin panel to start demo engine."
            )
            _stats["mode"] = "idle"
            return

        if occ_count == 0:
            log.warning("[stream] OCC registry is empty — stream idle. Use admin panel to start demo engine.")
            _stats["mode"] = "idle"
            return

        log.info(f"[stream] OCC registry ready: {occ_count:,} contracts — starting stream manager")
        # D-002: only spawn refresh_loop here in standalone mode.
        # When registry comes from lifespan, lifespan already owns refresh_loop.
        asyncio.create_task(registry.refresh_loop())

    _stats["active_symbols"] = registry.size()
    _stats["mode"] = "live"

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
# Trade processor
# ---------------------------------------------------------------------------
async def _process_trade(raw: dict):
    """
    Process a raw Tradier stream event (filter=timesale).

    C-008 — Decoupled persist/signal tiers:
      persist_ep = await accumulator.ingest_tick(ev)       <- above threshold?
      sig_ep     = await accumulator.get_signal(ts, ep)    <- cooldown passed?

      persist_flow_event() called on persist_ep (every qualifying tick),
      wrapped in asyncio.wait_for(timeout=2.0) so a slow DB insert cannot
      stall the hot path (issue #4).

      bus.publish_all() called on sig_ep (only when cooldown passes).

      Issue #6: build_composite failures increment composite_errors (not
      errors) so DB and signal-engine failures are independently observable.
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

        exch_count = flow_dedup.get_exchange_count(occ_symbol, ev.size, ev.fill_price)
        if exch_count == flow_dedup._sweep_min:
            dispatch_key = f"{occ_symbol}|{ev.size}|{ev.fill_price:.2f}"
            if dispatch_key not in _sweep_upgrade_dispatched:
                _sweep_upgrade_dispatched.add(dispatch_key)
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

    if flow_dedup.is_sweep(occ_symbol, ev.size, ev.fill_price):
        real_exch_count = flow_dedup.get_exchange_count(occ_symbol, ev.size, ev.fill_price)
        if ev.trade_type != "SWEEP":
            ev.trade_type = "SWEEP"
        ev.exchange_count = real_exch_count

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

    persist_ep = await accumulator.ingest_tick(ev)
    sig_ep     = await accumulator.get_signal(ev.timestamp, persist_ep)

    if not persist_ep:
        return

    try:
        await asyncio.wait_for(
            persist_flow_event({
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
            }),
            timeout=_PERSIST_TIMEOUT,
        )
    except asyncio.TimeoutError:
        _stats["errors"] += 1
        log.warning(
            f"[stream] persist_flow_event timed out after {_PERSIST_TIMEOUT}s "
            f"for {ev.ticker} — tick dropped. Check Supabase latency."
        )
        return

    if not sig_ep:
        return

    alert_level = accumulator.get_alert_level(sig_ep)

    log.info(
        f"[signal] {sig_ep.ticker} {sig_ep.contract_type} "
        f"| alert={alert_level} "
        f"| trades={sig_ep.trade_count} "
        f"| total_prem=${sig_ep.total_premium:,.0f} "
        f"| accel={sig_ep.is_accelerating} "
        f"| {sig_ep.summary_str()}"
    )

    try:
        composite = build_composite(sig_ep, accumulator)
    except Exception as e:
        _stats["composite_errors"] += 1
        log.error(f"[signal] build_composite failed for {sig_ep.ticker}: {e}")
        composite = None

    if sig_ep.contract_type == "CALL":
        direction = "REPEAT_BUY"
    elif sig_ep.contract_type == "PUT":
        direction = "REPEAT_SELL"
    else:
        direction = "REPEAT_BUY" if ev.sentiment == "BULLISH" else "REPEAT_SELL"

    signal = {
        "type": "signal",
        "data": {
            "ticker":          sig_ep.ticker,
            "direction":       direction,
            "contract_type":   sig_ep.contract_type,
            "strike":          sig_ep.strike,
            "expiry":          sig_ep.expiry,
            "total_premium":   sig_ep.total_premium,
            "trade_count":     sig_ep.trade_count,
            "alert_level":     alert_level,
            "is_accelerating": sig_ep.is_accelerating,
            "seed_episode":    sig_ep.summary_str(),
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
                    "contract_type":   sig_ep.contract_type,
                    "direction":       direction,
                    "influence_tier":  ev.influence_tier,
                    "total_premium":   sig_ep.total_premium,
                    "trade_count":     sig_ep.trade_count,
                    "is_accelerating": sig_ep.is_accelerating,
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
