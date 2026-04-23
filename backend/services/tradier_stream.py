"""
Tradier WebSocket stream processor.
Connects to Tradier's streaming API, parses option trades,
runs them through the signal pipeline, and publishes to the event bus.
"""
import asyncio
import json
import logging
from typing import Optional
import httpx
from config import settings
from core.async_bus import bus
from parsers.options_flow_parser import parse_tradier_trade
from signals.repetition_accumulator import RepetitionAccumulator

log = logging.getLogger("tradier_stream")

# Global stats
_stats = {
    "active_symbols": 0,
    "ticks":          0,
    "classified":     0,
    "signals":        0,
    "errors":         0,
}

accumulator = RepetitionAccumulator(window_minutes=30, min_trades=3, min_premium=50_000)


def get_stats() -> dict:
    return dict(_stats)


async def _get_session_token() -> Optional[str]:
    """Obtain a streaming session token from Tradier."""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{settings.TRADIER_BASE_URL}/v1/markets/events/session",
                headers={
                    "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
                    "Accept": "application/json",
                },
                content=b"",
            )
            if resp.status_code == 401:
                log.error(
                    "Tradier session token request returned 401 — "
                    "check TRADIER_API_KEY in Railway env vars"
                )
                return None
            resp.raise_for_status()
            return resp.json().get("stream", {}).get("sessionid")
    except Exception as e:
        log.error("Failed to get session token: %s", e)
        return None


async def stream_options_flow(symbols: list[str]):
    """
    Main streaming loop. Connects to Tradier, processes trades,
    and publishes signals to the async bus.
    """
    session_token = await _get_session_token()
    if not session_token:
        log.warning("No Tradier session token — running in demo mode")
        await _demo_mode(symbols)
        return

    _stats["active_symbols"] = len(symbols)
    url     = f"{settings.TRADIER_STREAM_URL}/v1/markets/events"
    headers = {
        "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
        "Accept":        "application/json",
    }
    payload = {
        "sessionid": session_token,
        "symbols":   ",".join(symbols),
        "filter":    "trade",
        "linebreak": "true",
    }

    while True:
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("POST", url, headers=headers, data=payload) as resp:
                    if resp.status_code == 401:
                        log.error(
                            "Tradier stream returned 401 — session token rejected. "
                            "Falling back to demo mode."
                        )
                        _stats["errors"] += 1
                        await _demo_mode(symbols)
                        return
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        await _process_trade(raw)
        except Exception as e:
            _stats["errors"] += 1
            log.error("Stream error: %s — reconnecting in 5s", e)
            await asyncio.sleep(5)


async def _process_trade(raw: dict):
    _stats["ticks"] += 1
    ev = parse_tradier_trade(raw)
    if not ev:
        return
    _stats["classified"] += 1

    ep = accumulator.ingest(ev)
    if ep:
        alert_level = accumulator.get_alert_level(ep)
        signal      = {
            "type":          "signal",
            "data": {
                "ticker":        ep.ticker,
                "direction":     "REPEAT_BUY" if ev.sentiment == "BULLISH" else "REPEAT_SELL",
                "contract_type": ep.contract_type,
                "strike":        ep.strike,
                "expiry":        ep.expiry,
                "total_premium": ep.total_premium,
                "trade_count":   ep.trade_count,
                "alert_level":   alert_level,
                "is_accelerating": ep.is_accelerating,
                "seed_episode":  ep.summary_str(),
                "timestamp":     ev.timestamp.isoformat(),
            }
        }
        _stats["signals"] += 1
        await bus.publish_all(signal)


async def _demo_mode(symbols: list[str]):
    """Emit synthetic signals when Tradier key is not set."""
    import random
    rng      = random.Random(42)
    tickers  = symbols or ["AAPL","TSLA","NVDA","SPY","QQQ","MSFT","AMZN","META"]
    _stats["active_symbols"] = len(tickers)
    ctypes   = ["CALL","PUT"]
    levels   = ["CONVICTION","STRONG_SIGNAL","ALERT","WATCH"]
    while True:
        await asyncio.sleep(rng.uniform(2, 6))
        ticker = rng.choice(tickers)
        prem   = rng.randint(100_000, 8_000_000)
        signal = {
            "type": "signal",
            "data": {
                "ticker":          ticker,
                "direction":       rng.choice(["REPEAT_BUY","REPEAT_SELL"]),
                "contract_type":   rng.choice(ctypes),
                "strike":          round(rng.uniform(100, 500), 0),
                "expiry":          "2025-01-17",
                "total_premium":   prem,
                "trade_count":     rng.randint(3, 25),
                "alert_level":     rng.choices(levels, weights=[5,15,30,50])[0],
                "is_accelerating": rng.random() < 0.2,
                "seed_episode":    f"Demo: {ticker} synthetic flow",
                "timestamp":       __import__("datetime").datetime.utcnow().isoformat(),
            }
        }
        _stats["ticks"]     += 1
        _stats["classified"] += 1
        _stats["signals"]    += 1
        await bus.publish_all(signal)
