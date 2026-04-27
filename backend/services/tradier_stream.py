"""
Tradier WebSocket stream processor — production-grade resilient implementation.

Design principles:
  - Fresh session token fetched on EVERY reconnect (tokens expire on stream close)
  - Never permanently falls into demo mode — always retries live connection
  - 30-second idle watchdog: reconnects if Tradier stops sending keepalives
  - Exponential backoff with jitter (5s base, 60s cap) on all error paths
  - Session token fetch retried up to 3x for transient network failures
  - Clear distinction: 401 on session = bad key (slow retry), 401 on stream = expired token (fast retry)
  - Demo mode is a supervised fallback task, NOT an automatic fallback (disabled 2026-04-25)
  - Market-hours guard: backs off 60s when US options market is closed

CHANGELOG (key fixes, details in git history):
  C-015 — switched filter=trade -> filter=timesale (root cause of strike=0/bid=0)
  C-019 — dedup actively wired into _process_trade (was instantiated but never called)
  C-020 — dedup clock mismatch fixed (monotonic vs wall-clock TTL comparison)
  C-002 — persist gate moved AFTER accumulator.ingest_tick() (not before)
  C-003 — retroactive sweep upgrade via upgrade_to_sweep_in_db()
  C-007 — RepetitionAccumulator cooldown added (5-min per episode)
  C-008 — persist tier and signal tier decoupled (independent ingest_tick/get_signal calls)
  B-008 — last_tick_at / last_reconnect_at / uptime_seconds added to get_stats()
  Issue#6 — composite_errors counter separate from generic errors
  B4-001 — stream_options_flow now accepts optional registry kwarg (ignored internally;
             stream builds its own registry) so main.py can pass (symbols, registry)
             without a TypeError.

Tradier streaming notes:
  - Session token: POST /v1/markets/events/session with Content-Length: 0 (data={})
  - Session tokens expire when the stream connection closes — always re-fetch
  - filter=timesale: symbol = full OCC string, price = option fill price
  - filter=trade:    symbol = underlying ticker, price = stock last — DO NOT USE
  - exch field codes: C=CBOE M=MIAX Q=NASDAQ N=NYSE X=PHLX B=BATO

Demo mode:
  - Automatic fallback DISABLED as of 2026-04-25.
  - Use admin panel (/admin) to run the demo engine manually.
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
# Compat stubs — previously-exported names that tests import.
# ---------------------------------------------------------------------------
async def _demo_mode_once(tickers: list | None = None) -> None:
    """
    Emit synthetic demo signals for each ticker in a loop until cancelled.
    Tests call: asyncio.create_task(_demo_mode_once(["AAPL", "TSLA"]))
    """
    tickers = tickers or []
    while True:
        for ticker in tickers:
            signal = {
                "type": "signal",
                "data": {
                    "ticker": ticker,
                    "recommendation": "WATCH",
                    "composite_score": 0.5,
                },
            }
            await bus.publish_all(signal)
        await asyncio.sleep(0.1)


async def _guarded_lines(resp):
    """
    Async generator that wraps resp.aiter_lines() with an idle timeout.
    Raises asyncio.TimeoutError if no line arrives within _IDLE_TIMEOUT seconds.
    """
    async for line in resp.aiter_lines():
        yield line
        # Re-apply per-line timeout by wrapping next iteration inline.
        # The outer asyncio.wait_for on the whole generator handles the guard.


# Override to enforce the per-line idle watchdog properly:
_original_guarded_lines = _guarded_lines


async def _guarded_lines(resp):  # noqa: F811  (intentional redefinition)
    """
    Async generator with idle-timeout watchdog.
    Raises asyncio.TimeoutError if a line takes longer than _IDLE_TIMEOUT.
    """
    aiter = resp.aiter_lines().__aiter__()
    while True:
        try:
            line = await asyncio.wait_for(aiter.__anext__(), timeout=_IDLE_TIMEOUT)
        except StopAsyncIteration:
            return
        yield line


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
                    "Tradier session 401 — TRADIER_API_KEY rejected. "
                    "Verify the key in Railway env vars. "
                    "(attempt %d/%d)", attempt + 1, _SESSION_RETRY_MAX
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
# Main streaming entry point
# ---------------------------------------------------------------------------
async def stream_options_flow(symbols: list[str], registry=None):
    """
    Start the live Tradier WebSocket stream for the given OCC symbol list.

    Parameters
    ----------
    symbols:
        Underlying tickers to subscribe to (e.g. ["AAPL", "SPY"]).
    registry:
        Optional pre-built SymbolRegistry passed from main.py lifespan.
        Accepted here for API compatibility (B4-001) but ignored — the
        stream builds and manages its own registry internally via
        init_registry() + StreamManager.
    """
    _stats["active_symbols"] = len(symbols)
    _stats["mode"] = "starting"

    if not settings.TRADIER_API_KEY:
        log.warning("TRADIER_API_KEY not set — stream idle. Use admin panel to start demo engine.")
        _stats["mode"] = "idle"
        return

    from services.symbol_registry import init_registry
    from services.stream_manager import StreamManager

    log.info("[stream] Building OCC registry for %d tickers...", len(symbols))
    registry = init_registry(watchlist=symbols)

    try:
        occ_count = await registry.build()
    except Exception as e:
        log.error(
            "[stream] OCC registry build failed: %s — stream idle. "
            "Use admin panel to start demo engine.", e
        )
        _stats["mode"] = "idle"
        return

    _stats["active_symbols"] = occ_count

    if occ_count == 0:
        log.warning("[stream] OCC registry is empty — stream idle. Use admin panel to start demo engine.")
        _stats["mode"] = "idle"
        return

    log.info("[stream] OCC registry ready: %d contracts — starting stream manager", occ_count)
    _stats["mode"] = "live"

    asyncio.create_task(registry.refresh_loop())

    manager = StreamManager(registry=registry, process_fn=_process_trade)
    await manager.run()


# B4-001: alias so tests and any future callers resolve without breaking main.py
start_stream = stream_options_flow


# ---------------------------------------------------------------------------
# Trade processor
# ---------------------------------------------------------------------------
async def _process_trade(raw: dict):
    """
    Process a raw Tradier stream event (filter=timesale).

    C-008 — Decoupled persist/signal tiers:
      persist_ep = await accumulator.ingest_tick(ev)       <- above threshold?
      sig_ep     = await accumulator.get_signal(ts, ep)    <- cooldown passed?

      persist_flow_event() on persist_ep — every qualifying tick.
      bus.publish_all()   on sig_ep     — only when cooldown passes.
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
    # Deduplication (C-019 + C-020)
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
            "[dedup] dropped duplicate: %s size=%d fill=%s exch=%s",
            occ_symbol, ev.size, ev.fill_price, exchange,
        )

        exch_count = flow_dedup.get_exchange_count(occ_symbol, ev.size, ev.fill_price)
        if exch_count == flow_dedup._sweep_min:
            dispatch_key = f"{occ_symbol}|{ev.size}|{ev.fill_price:.2f}"
            if dispatch_key not in _sweep_upgrade_dispatched:
                _sweep_upgrade_dispatched.add(dispatch_key)
                log.info(
                    "[sweep] threshold just crossed — retroactive upgrade: "
                    "%s size=%d fill=%s exchanges=%d",
                    occ_symbol, ev.size, ev.fill_price, exch_count,
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
        "[flow] %s %s $%.2f %s dte=%d | fill=%s bid=%s ask=%s size=%d "
        "iv=%.1f%% premium=$%.0f exch=%s",
        ev.trade_type, occ_symbol, ev.strike, ev.option_type if hasattr(ev, 'option_type') else ev.contract_type,
        ev.dte, ev.fill_price, ev.bid, ev.ask, ev.size,
        (ev.implied_volatility if hasattr(ev, 'implied_volatility') else ev.iv or 0) * 100,
        ev.premium, exchange,
    )

    # ------------------------------------------------------------------
    # Composite signal
    # ------------------------------------------------------------------
    comp = None
    try:
        comp = build_composite(ev)
    except Exception as e:
        _stats["composite_errors"] += 1
        log.debug("[composite] build failed for %s: %s", occ_symbol, e)

    # ------------------------------------------------------------------
    # Persist + signal (C-008 decoupled tiers)
    # ------------------------------------------------------------------
    try:
        persist_ep = await accumulator.ingest_tick(ev)
        if persist_ep:
            ev_dict = {
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
                "is_synthetic_quote": ev.is_synthetic_quote,
            }
            try:
                await asyncio.wait_for(
                    persist_flow_event(ev_dict),
                    timeout=_PERSIST_TIMEOUT,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "[stream] persist_flow_event timed out after %.1fs for %s",
                    _PERSIST_TIMEOUT, occ_symbol,
                )
                _stats["errors"] += 1

        ts = _time.time()
        sig_ep = await accumulator.get_signal(ts, persist_ep)
        if sig_ep:
            _stats["signals"] += 1
            # Build a plain dict signal and publish — avoids kwargs mismatch
            # when tests wrap bus.publish_all with a simple positional capture fn.
            signal_msg = {
                "type": "composite_signal",
                "data": {
                    "signal":  comp or {},
                    "episode": sig_ep if isinstance(sig_ep, dict) else vars(sig_ep),
                },
            }
            await bus.publish_all(signal_msg)

    except Exception as e:
        _stats["errors"] += 1
        log.error("[stream] error processing trade %s: %s", occ_symbol, e, exc_info=True)
