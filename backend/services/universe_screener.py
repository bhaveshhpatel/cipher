"""
services/universe_screener.py

DEPRECATED as of Step 3 refactor.

Stream eligibility is now computed in symbols_loader._fetch_batch_quotes()
via Tradier /v1/markets/quotes (200 symbols/batch, ~28 parallel requests).
This file is kept for reference and backward compatibility with any tests
that import ScreenResult, but screen_universe() is NO LONGER CALLED from
load_universe() or anywhere in the startup pipeline.

The old approach (per-symbol OI chain call) was too slow for 5,500 symbols:
  - 5,500 symbols / 20 concurrency = 275 serial batches × ~1s each ≈ 4+ minutes
The new approach (batch quotes):
  - 5,500 symbols / 200 per batch = ~28 batches fired in parallel ≈ 5–10 seconds

If you need OI-based screening in the future, re-enable from here.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from config import settings

log = logging.getLogger("universe_screener")

_SCREEN_TIMEOUT     = 8.0
_SCREEN_CONCURRENCY = 20
_OI_URL             = f"{settings.TRADIER_BASE_URL}/v1/markets/options/chains"


@dataclass
class ScreenResult:
    eligible:   list[str] = field(default_factory=list)
    ineligible: list[str] = field(default_factory=list)
    priority:   list[str] = field(default_factory=list)
    screened:   int       = 0
    duration_s: float     = 0.0
    source:     str       = "screener"

    @property
    def total(self) -> int:
        return len(self.eligible) + len(self.ineligible)

    def summary(self) -> dict:
        return {
            "eligible":   len(self.eligible),
            "ineligible": len(self.ineligible),
            "priority":   len(self.priority),
            "screened":   self.screened,
            "duration_s": round(self.duration_s, 2),
            "source":     self.source,
        }


async def screen_universe(symbols: list[str]) -> ScreenResult:
    """
    DEPRECATED — no longer called from load_universe().
    Stream eligibility is now computed via _fetch_batch_quotes() in symbols_loader.py.
    Kept for reference / legacy test compatibility only.
    """
    log.warning(
        "universe_screener.screen_universe() called — this function is DEPRECATED. "
        "Stream eligibility is now computed in symbols_loader._fetch_batch_quotes()."
    )
    start  = time.monotonic()
    result = ScreenResult()

    if not symbols:
        return result

    priority_set = set(settings.priority_symbols)
    priority     = [s for s in symbols if s in priority_set]
    candidates   = [s for s in symbols if s not in priority_set]

    result.priority = priority
    result.eligible = list(priority)

    if candidates:
        if settings.TRADIER_API_KEY:
            screened_eligible, screened_count = await _screen_candidates(candidates)
            result.eligible.extend(screened_eligible)
            result.ineligible = list(set(candidates) - set(screened_eligible))
            result.screened   = screened_count
        else:
            if settings.UNIVERSE_STREAM_ELIGIBLE_DEFAULT:
                result.eligible.extend(candidates)
            else:
                result.ineligible.extend(candidates)

    result.duration_s = time.monotonic() - start
    return result


async def get_stream_eligible(symbols: list[str]) -> list[str]:
    result = await screen_universe(symbols)
    return result.eligible


async def _screen_candidates(candidates: list[str]) -> tuple[list[str], int]:
    sem      = asyncio.Semaphore(_SCREEN_CONCURRENCY)
    delay_s  = settings.UNIVERSE_BATCH_DELAY_MS / 1000.0
    eligible = []
    screened = 0

    async def _check(symbol: str) -> Optional[str]:
        async with sem:
            return await _is_stream_eligible(symbol)

    batch_size = _SCREEN_CONCURRENCY
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i : i + batch_size]
        results = await asyncio.gather(*[_check(s) for s in batch])
        eligible.extend(s for s in results if s is not None)
        screened += len(batch)
        if delay_s > 0 and i + batch_size < len(candidates):
            await asyncio.sleep(delay_s)

    return eligible, screened


async def _is_stream_eligible(symbol: str) -> Optional[str]:
    headers = {
        "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
        "Accept": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=_SCREEN_TIMEOUT) as client:
            resp = await client.get(
                _OI_URL,
                headers=headers,
                params={"symbol": symbol, "expiration": _nearest_expiry_param()},
            )
        if resp.status_code != 200:
            return symbol if settings.UNIVERSE_STREAM_ELIGIBLE_DEFAULT else None

        data    = resp.json()
        options = data.get("options") or {}
        option  = options.get("option") or []
        if isinstance(option, dict):
            option = [option]

        for contract in option:
            if (contract.get("open_interest") or 0) > 0:
                return symbol

        return symbol if settings.UNIVERSE_STREAM_ELIGIBLE_DEFAULT else None

    except Exception as e:
        log.debug("universe_screener._is_stream_eligible(%s) error: %s", symbol, e)
        return symbol if settings.UNIVERSE_STREAM_ELIGIBLE_DEFAULT else None


def _nearest_expiry_param() -> str:
    from datetime import date, timedelta
    today      = date.today()
    days_ahead = (4 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return (today + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
