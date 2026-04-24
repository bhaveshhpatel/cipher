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

Tradier streaming notes:
  - Session token: POST /v1/markets/events/session with Content-Length: 0 (data={})
  - Session tokens expire when the stream connection closes — always re-fetch
  - Stream POST uses sessionid + Bearer token in headers
  - Tradier sends bare newlines as keepalives — idle >30s means the connection is dead
  - On market close, Tradier may close the stream normally — reconnect for next open
  - Tradier closes the stream immediately when the market is closed (no queued data)
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
# Main streaming loop
# ---------------------------------------------------------------------------
async def stream_options_flow(symbols: list[str]):
    _stats["active_symbols"] = len(symbols)
    _stats["mode"] = "starting"

    if not settings.TRADIER_API_KEY:
        log.warning("TRADIER_API_KEY not set — running in demo mode")
        _stats["mode"] = "demo"
        await _demo_mode(symbols)
        return

    url = f"{settings.TRADIER_STREAM_URL}/v1/markets/events"
    stream_headers = {
        "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
        "Accept": "application/json",
    }

    consecutive_stream_401s = 0
    reconnect_attempt = 0
    demo_task: Optional[asyncio.Task] = None

    while True:
        # --- 0. Market hours guard ---
        if not _is_market_hours():
            now_et = datetime.now(_ET)
            log.info(
                f"Market closed (ET: {now_et.strftime('%H:%M %Z %a')}) "
                f"— sleeping {int(_MARKET_CLOSED_SLEEP)}s before next check"
            )
            _stats["mode"] = "market_closed"
            await asyncio.sleep(_MARKET_CLOSED_SLEEP)
            continue

        # --- 1. Fetch fresh session token ---
        session_token = await _get_session_token()

        if not session_token:
            _stats["errors"] += 1
            backoff = _backoff(min(reconnect_attempt, 7))
            log.warning(
                f"No session token — backing off {backoff:.1f}s before retry (attempt {reconnect_attempt + 1})"
            )
            if demo_task is None or demo_task.done():
                log.info("Starting demo mode as fallback while waiting for live connection")
                _stats["mode"] = "demo"
                demo_task = asyncio.create_task(_demo_mode_once(symbols))
            reconnect_attempt += 1
            await asyncio.sleep(backoff)
            continue

        if demo_task and not demo_task.done():
            demo_task.cancel()
            try:
                await demo_task
            except asyncio.CancelledError:
                pass
            demo_task = None

        payload = {
            "sessionid": session_token,
            "symbols":   ",".join(symbols),
            "filter":    "trade",
            "linebreak": "true",
        }

        session_ticks = 0

        # --- 2. Open stream ---
        try:
            timeout = httpx.Timeout(connect=_CONNECT_TIMEOUT, read=None, write=10.0, pool=10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, headers=stream_headers, data=payload) as resp:

                    if resp.status_code == 401:
                        consecutive_stream_401s += 1
                        log.warning(
                            f"Tradier stream 401 (session expired) — re-fetching token "
                            f"(consecutive: {consecutive_stream_401s})"
                        )
                        _stats["errors"] += 1
                        if consecutive_stream_401s >= 5:
                            backoff = _backoff(min(consecutive_stream_401s, 7))
                            log.error(
                                f"5+ consecutive stream 401s — possible bad API key. "
                                f"Backing off {backoff:.1f}s"
                            )
                            await asyncio.sleep(backoff)
                        else:
                            await asyncio.sleep(1.0)
                        reconnect_attempt += 1
                        continue

                    consecutive_stream_401s = 0
                    _stats["mode"] = "live"
                    log.info(f"Tradier stream connected — monitoring {len(symbols)} symbols")

                    async for line in _iter_lines_with_watchdog(resp):
                        if not line.strip():
                            continue
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        session_ticks += 1
                        await _process_trade(raw)

                    log.info("Tradier stream closed cleanly — reconnecting")

        except asyncio.TimeoutError:
            _stats["errors"] += 1
            log.warning(f"Tradier stream idle for {int(_IDLE_TIMEOUT)}s — reconnecting")

        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as e:
            _stats["errors"] += 1
            log.warning(f"Tradier stream network error: {e} — reconnecting")

        except Exception as e:
            _stats["errors"] += 1
            log.error(f"Tradier stream unexpected error: {e} — reconnecting")

        _stats["reconnects"] += 1
        _stats["mode"] = "reconnecting"

        if session_ticks > 0:
            reconnect_attempt = 0
        else:
            reconnect_attempt += 1

        backoff = _backoff(min(reconnect_attempt, 7))
        log.info(f"Reconnecting in {backoff:.1f}s (attempt {reconnect_attempt + 1})")
        await asyncio.sleep(backoff)


# ---------------------------------------------------------------------------
# Idle watchdog
# ---------------------------------------------------------------------------
async def _iter_lines_with_watchdog(resp: httpx.Response):
    async for line in resp.aiter_lines():
        yield line


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
# Trade processor
#
# Phase 4 addition:
#   After build_composite() is called on a qualifying episode, publish
#   a 'composite_signal' bus message so signal_store.py can persist it
#   to the signal_history table.
#
# FIX 1: persist_flow_event() is called for every classified tick.
# FIX 2: per-tick log downgraded to DEBUG to avoid Railway rate-limit drops.
# ---------------------------------------------------------------------------
async def _process_trade(raw: dict):
    _stats["ticks"] += 1
    ev = parse_tradier_trade(raw)
    if not ev:
        return

    _stats["classified"] += 1

    log.debug(
        f"[flow] {ev.ticker} {ev.contract_type} "
        f"${ev.strike:.0f} {ev.expiry} "
        f"| prem=${ev.premium:,.0f} "
        f"| type={ev.trade_type or 'UNKNOWN'} "
        f"| sentiment={ev.sentiment or 'UNKNOWN'} "
        f"| tier={ev.influence_tier or 'UNKNOWN'}"
    )

    await persist_flow_event({
        "ticker":           ev.ticker,
        "contract_type":    ev.contract_type,
        "strike":           ev.strike,
        "expiry":           ev.expiry,
        "premium":          ev.premium,
        "trade_type":       ev.trade_type,
        "sentiment":        ev.sentiment,
        "influence_tier":   ev.influence_tier,
        "conviction_score": ev.conviction_score,
        "is_golden_sweep":  ev.is_golden_sweep,
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

    # Build composite signal (Phase 3 scoring)
    try:
        composite = build_composite(ep, accumulator)
    except Exception as e:
        log.error(f"[signal] build_composite failed for {ep.ticker}: {e}")
        composite = None

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

    # Phase 4: publish composite_signal for signal_store.py to persist
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
    import datetime
    rng     = random.Random(42)
    tickers = symbols or ["AAPL", "TSLA", "NVDA", "SPY", "QQQ", "MSFT", "AMZN", "META"]
    ctypes  = ["CALL", "PUT"]
    levels  = ["CONVICTION", "STRONG_SIGNAL", "ALERT", "WATCH"]

    log.info("Demo mode active — emitting synthetic signals")
    try:
        while True:
            await asyncio.sleep(rng.uniform(2, 6))
            ticker    = rng.choice(tickers)
            prem      = rng.randint(100_000, 8_000_000)
            ctype     = rng.choice(ctypes)
            direction = rng.choice(["REPEAT_BUY", "REPEAT_SELL"])
            signal = {
                "type": "signal",
                "data": {
                    "ticker":          ticker,
                    "direction":       direction,
                    "contract_type":   ctype,
                    "strike":          round(rng.uniform(100, 500), 0),
                    "expiry":          "2025-01-17",
                    "total_premium":   prem,
                    "trade_count":     rng.randint(3, 25),
                    "alert_level":     rng.choices(levels, weights=[5, 15, 30, 50])[0],
                    "is_accelerating": rng.random() < 0.2,
                    "seed_episode":    f"Demo: {ticker} synthetic flow",
                    "timestamp":       datetime.datetime.utcnow().isoformat(),
                },
            }
            _stats["ticks"]      += 1
            _stats["classified"] += 1
            _stats["signals"]    += 1
            await bus.publish_all(signal)

            # Emit composite_signal so signal_store.py populates signal_history
            composite_score = round(rng.uniform(0.40, 0.95), 3)
            flow_score      = round(rng.uniform(0.40, 0.90), 3)
            backtest_score  = round(rng.uniform(0.40, 0.85), 3)
            vwp_factor      = round(rng.uniform(0.30, 0.80), 3)
            trade_count     = rng.randint(3, 25)
            is_accel        = rng.random() < 0.2
            rec = "BUY" if composite_score >= 0.65 and direction == "REPEAT_BUY" else \
                  "SELL" if composite_score >= 0.65 else "HOLD"
            # After await bus.publish_all(signal) in demo loop, add:
            composite_msg = {
                "type": "composite_signal",
                "data": {
                    "signal": {
                        "ticker":                ticker,
                        "recommendation":        rng.choice(["BUY", "SELL", "HOLD"]),
                        "composite_score":       round(rng.uniform(0.4, 0.95), 3),
                        "flow_score":            round(rng.uniform(0.4, 0.9), 3),
                        "backtest_score":        round(rng.uniform(0.4, 0.85), 3),
                        "volume_premium_factor": round(rng.uniform(0.3, 0.8), 3),
                        "reasoning":             f"Demo synthetic signal for {ticker}",
                    },
                    "episode": {
                        "contract_type":   rng.choice(["CALL", "PUT"]),
                        "direction":       rng.choice(["REPEAT_BUY", "REPEAT_SELL"]),
                        "influence_tier":  rng.choice(["INSTITUTIONAL", "RETAIL"]),
                        "total_premium":   prem,
                        "trade_count":     rng.randint(3, 25),
                        "is_accelerating": rng.random() < 0.2,
                        "timestamp":       datetime.datetime.utcnow().isoformat(),
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
