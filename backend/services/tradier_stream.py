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
_PERSIST_TIMEOUT     = 2.0   # max seconds to wait for persist_flow_event()

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

# Guard against double-dispatch of retroactive sweep upgrade tasks.
# Key = "occ_symbol|size|fill_price"; entry added before create_task.
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
async def stream_options_flow(symbols: list[str]):
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

        # C-003: retroactive sweep upgrade
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

    # Canonical print — inline sweep upgrade if pattern already established
    if flow_dedup.is_sweep(occ_symbol, ev.size, ev.fill_price):
        real_exch_count = flow_dedup.get_exchange_count(occ_symbol, ev.size, ev.fill_price)
        if ev.trade_type != "SWEEP":
            ev.trade_type = "SWEEP"
        ev.exchange_count = real_exch_count

    _stats["classified"] += 1
    _stats["last_tick_at"] = _time.time()

    log.debug(
        "[flow] %s %s $%.2f %s dte=%d | fill=%s bid=%s ask=%s size=%d "
        "| prem=$%,.0f | ba=%s aggressive=%s | type=%s exch=%s exch_count=%d "
        "| sentiment=%s tier=%s | conviction=%s occ=%s | synthetic_quote=%s",
        ev.ticker, ev.contract_type, ev.strike, ev.expiry, ev.dte,
        ev.fill_price, ev.bid, ev.ask, ev.size,
        ev.premium,
        ev.bid_ask_class, ev.is_aggressive,
        ev.trade_type, exchange, ev.exchange_count,
        ev.sentiment, ev.influence_tier,
        ev.conviction_score, occ_symbol,
        ev.is_synthetic_quote,
    )

    # ------------------------------------------------------------------
    # C-008: Decoupled persist tier / signal tier
    # ------------------------------------------------------------------
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
            "[stream] persist_flow_event timed out after %.1fs for %s — tick dropped. "
            "Check Supabase latency.",
            _PERSIST_TIMEOUT, ev.ticker,
        )
        return

    if not sig_ep:
        return

    alert_level = accumulator.get_alert_level(sig_ep)

    log.info(
        "[signal] %s %s | alert=%s | trades=%d | total_prem=$%,.0f | accel=%s | %s",
        sig_ep.ticker, sig_ep.contract_type,
        alert_level, sig_ep.trade_count, sig_ep.total_premium,
        sig_ep.is_accelerating, sig_ep.summary_str(),
    )

    try:
        composite = build_composite(sig_ep, accumulator)
    except Exception as e:
        _stats["composite_errors"] += 1
        log.error("[signal] build_composite failed for %s: %s", sig_ep.ticker, e)
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
