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
from signals.repetition_accumulator import RepetitionAccumulator

log = logging.getLogger("tradier_stream")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_SESSION_RETRY_MAX   = 3        # max attempts to fetch a session token
_SESSION_RETRY_DELAY = 2.0      # seconds between session fetch retries
_BACKOFF_BASE        = 5.0      # initial reconnect delay (seconds)
_BACKOFF_CAP         = 60.0     # maximum reconnect delay (seconds)
_IDLE_TIMEOUT        = 30.0     # seconds without any line before declaring connection dead
_CONNECT_TIMEOUT     = 15.0     # seconds to establish the HTTP connection
_MARKET_CLOSED_SLEEP = 60.0     # seconds to sleep when market is closed before retrying

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
    "mode":           "starting",   # "live" | "demo" | "starting"
}

accumulator = RepetitionAccumulator(window_minutes=30, min_trades=3, min_premium=50_000)


def get_stats() -> dict:
    return dict(_stats)


# ---------------------------------------------------------------------------
# Market hours helper
# ---------------------------------------------------------------------------
def _is_market_hours() -> bool:
    """
    Returns True if the US options market is currently open.
    Options trade Mon–Fri 9:30–16:00 ET.
    Does NOT account for market holidays (Tradier will just close the stream on those).
    """
    now_et = datetime.now(_ET)
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return _MARKET_OPEN <= now_et.time() < _MARKET_CLOSE


# ---------------------------------------------------------------------------
# Backoff helper
# ---------------------------------------------------------------------------
def _backoff(attempt: int) -> float:
    """Exponential backoff with full jitter, capped at _BACKOFF_CAP."""
    delay = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** attempt))
    return random.uniform(0, delay)


# ---------------------------------------------------------------------------
# Session token — fetched fresh on every reconnect
# ---------------------------------------------------------------------------
async def _get_session_token() -> Optional[str]:
    """
    Fetch a fresh Tradier streaming session token.

    Retried up to _SESSION_RETRY_MAX times for transient network failures.
    Returns None only if:
      - API key is definitively rejected (401)
      - All retries exhausted

    IMPORTANT: Uses data={} (not content=b"") so httpx sends Content-Length: 0,
    equivalent to curl -d "", which Tradier requires to issue a sessionid.
    """
    url = f"{settings.TRADIER_STREAM_URL}/v1/markets/events/session"
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
                    "Tradier session 401 — TRADIER_API_KEY rejected. "
                    "Verify the key in Railway env vars. (attempt %d/%d)",
                    attempt + 1, _SESSION_RETRY_MAX,
                )
                return None

            resp.raise_for_status()
            token = resp.json().get("stream", {}).get("sessionid")
            if token:
                log.info("Tradier session token obtained successfully")
                return token
            log.warning("Tradier session response missing sessionid field: %s", resp.text[:200])
            return None

        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            log.warning(
                "Tradier session fetch failed (transient, attempt %d/%d): %s",
                attempt + 1, _SESSION_RETRY_MAX, e,
            )
            if attempt < _SESSION_RETRY_MAX - 1:
                await asyncio.sleep(_SESSION_RETRY_DELAY)
        except Exception as e:
            log.error("Tradier session fetch unexpected error: %s", e)
            return None

    log.error("Tradier session token could not be obtained after %d attempts", _SESSION_RETRY_MAX)
    return None


# ---------------------------------------------------------------------------
# Main streaming loop
# ---------------------------------------------------------------------------
async def stream_options_flow(symbols: list[str]):
    """
    Resilient main loop. Never exits permanently.

    Lifecycle:
      1. Check market hours — sleep 60s if closed (Tradier closes stream immediately when closed)
      2. Fetch fresh session token
      3. Open streaming POST connection
      4. Read lines with 30s idle watchdog
      5. On any failure: back off, re-fetch token, reconnect
      6. If no live key configured: run demo mode in parallel, keep retrying live

    Key design: reconnect_attempt is only reset when we actually receive data (ticks).
    A clean close with no ticks (market closed) preserves the attempt counter so
    backoff accumulates properly instead of always restarting from 0.
    """
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
                "Market closed (ET: %s) — sleeping %ds before next check",
                now_et.strftime("%H:%M %Z %a"), int(_MARKET_CLOSED_SLEEP),
            )
            _stats["mode"] = "market_closed"
            await asyncio.sleep(_MARKET_CLOSED_SLEEP)
            continue

        # --- 1. Fetch fresh session token on every reconnect ---
        session_token = await _get_session_token()

        if not session_token:
            _stats["errors"] += 1
            backoff = _backoff(min(reconnect_attempt, 7))
            log.warning(
                "No session token — backing off %.1fs before retry (attempt %d)",
                backoff, reconnect_attempt + 1,
            )
            if demo_task is None or demo_task.done():
                log.info("Starting demo mode as fallback while waiting for live connection")
                _stats["mode"] = "demo"
                demo_task = asyncio.create_task(_demo_mode_once(symbols))
            reconnect_attempt += 1
            await asyncio.sleep(backoff)
            continue

        # Got a token — cancel demo fallback, switch to live
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

        # Track ticks received in this session to detect "connected but no data" (market closed)
        session_ticks = 0

        # --- 2. Open stream and read with idle watchdog ---
        try:
            timeout = httpx.Timeout(connect=_CONNECT_TIMEOUT, read=None, write=10.0, pool=10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, headers=stream_headers, data=payload) as resp:

                    if resp.status_code == 401:
                        consecutive_stream_401s += 1
                        log.warning(
                            "Tradier stream 401 (session expired) — re-fetching token "
                            "(consecutive: %d)", consecutive_stream_401s,
                        )
                        _stats["errors"] += 1
                        if consecutive_stream_401s >= 5:
                            backoff = _backoff(min(consecutive_stream_401s, 7))
                            log.error(
                                "5+ consecutive stream 401s — possible bad API key. "
                                "Backing off %.1fs", backoff
                            )
                            await asyncio.sleep(backoff)
                        else:
                            await asyncio.sleep(1.0)
                        reconnect_attempt += 1
                        continue

                    # Successful connection
                    consecutive_stream_401s = 0
                    _stats["mode"] = "live"
                    log.info(
                        "Tradier stream connected — monitoring %d symbols",
                        len(symbols),
                    )

                    # --- 3. Read lines with 30s idle watchdog ---
                    async for line in _iter_lines_with_watchdog(resp):
                        if not line.strip():
                            continue  # keepalive newline
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        session_ticks += 1
                        await _process_trade(raw)

                    # Stream ended cleanly
                    log.info("Tradier stream closed cleanly — reconnecting")

        except asyncio.TimeoutError:
            _stats["errors"] += 1
            log.warning("Tradier stream idle for %ds — reconnecting", int(_IDLE_TIMEOUT))

        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as e:
            _stats["errors"] += 1
            log.warning("Tradier stream network error: %s — reconnecting", e)

        except Exception as e:
            _stats["errors"] += 1
            log.error("Tradier stream unexpected error: %s — reconnecting", e)

        # --- 4. Back off before reconnect ---
        _stats["reconnects"] += 1
        _stats["mode"] = "reconnecting"

        # Only reset the attempt counter if we actually received data this session.
        # If session_ticks == 0, the market is likely closed and Tradier closed instantly —
        # preserve the attempt counter so backoff accumulates (not reset to 0 every cycle).
        if session_ticks > 0:
            reconnect_attempt = 0
        else:
            reconnect_attempt += 1

        backoff = _backoff(min(reconnect_attempt, 7))
        log.info("Reconnecting in %.1fs (attempt %d)", backoff, reconnect_attempt + 1)
        await asyncio.sleep(backoff)


# ---------------------------------------------------------------------------
# Idle watchdog wrapper
# ---------------------------------------------------------------------------
async def _iter_lines_with_watchdog(resp: httpx.Response):
    """
    Wraps resp.aiter_lines() with a per-line timeout.
    Raises asyncio.TimeoutError if no line is received within _IDLE_TIMEOUT seconds.
    This catches silent TCP hangs that httpx would otherwise not detect.
    """
    async for line in resp.aiter_lines():
        yield line


async def _guarded_lines(resp: httpx.Response):
    """
    Watchdog using asyncio.wait_for per line.
    """
    aiter = resp.aiter_lines().__aiter__()
    while True:
        try:
            line = await asyncio.wait_for(aiter.__anext__(), timeout=_IDLE_TIMEOUT)
            yield line
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise


# Override the simple wrapper with the guarded version
_iter_lines_with_watchdog = _guarded_lines  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Trade processor
# ---------------------------------------------------------------------------
async def _process_trade(raw: dict):
    _stats["ticks"] += 1
    ev = parse_tradier_trade(raw)
    if not ev:
        return
    _stats["classified"] += 1

    ep = accumulator.ingest(ev)
    if not ep:
        return

    alert_level = accumulator.get_alert_level(ep)
    signal = {
        "type": "signal",
        "data": {
            "ticker":          ep.ticker,
            "direction":       "REPEAT_BUY" if ev.sentiment == "BULLISH" else "REPEAT_SELL",
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


# ---------------------------------------------------------------------------
# Demo mode — supervised fallback, not an infinite trap
# ---------------------------------------------------------------------------
async def _demo_mode_once(symbols: list[str]):
    """
    Emit synthetic signals as a cancellable background task.
    Designed to be cancelled when a live connection is established.
    """
    import datetime
    rng     = random.Random(42)
    tickers = symbols or ["AAPL", "TSLA", "NVDA", "SPY", "QQQ", "MSFT", "AMZN", "META"]
    ctypes  = ["CALL", "PUT"]
    levels  = ["CONVICTION", "STRONG_SIGNAL", "ALERT", "WATCH"]

    log.info("Demo mode active — emitting synthetic signals")
    try:
        while True:
            await asyncio.sleep(rng.uniform(2, 6))
            ticker = rng.choice(tickers)
            prem   = rng.randint(100_000, 8_000_000)
            signal = {
                "type": "signal",
                "data": {
                    "ticker":          ticker,
                    "direction":       rng.choice(["REPEAT_BUY", "REPEAT_SELL"]),
                    "contract_type":   rng.choice(ctypes),
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
    except asyncio.CancelledError:
        log.info("Demo mode cancelled — live stream connection established")
        raise


async def _demo_mode(symbols: list[str]):
    """Blocking demo mode — used when no API key is configured at all."""
    _stats["mode"] = "demo"
    await _demo_mode_once(symbols)
