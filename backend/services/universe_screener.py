"""
services/universe_screener.py

Stream-eligible screening for the options universe.

Determines which symbols in the validated universe should be actively
monitored by the Tradier stream. Not every ~8,000 symbol needs to stream
simultaneously — this screener identifies a focused pool of
stream-eligible tickers based on:

  1. Priority pool  — always stream (configured via UNIVERSE_PRIORITY_SYMBOLS)
  2. Open interest  — symbols with meaningful OI pass automatically
  3. Default flag   — symbols without data default to UNIVERSE_STREAM_ELIGIBLE_DEFAULT

Public API:
  screen_universe(symbols) -> ScreenResult
      Runs the full screening pass over a symbol list.
      Returns a ScreenResult with eligible/ineligible sets and metadata.

  get_stream_eligible(symbols) -> list[str]
      Convenience wrapper — returns only the eligible symbol list.

Design:
  - Priority symbols always pass — no API call needed
  - Remaining symbols screened in batches with configurable delay
    (UNIVERSE_BATCH_DELAY_MS) to avoid Tradier rate limits
  - All network errors are caught — a failed screen defaults to
    UNIVERSE_STREAM_ELIGIBLE_DEFAULT for that symbol
  - Fully async, safe to call from startup or background refresh
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from config import settings

log = logging.getLogger("universe_screener")

_SCREEN_TIMEOUT     = 8.0   # per-symbol HTTP timeout
_SCREEN_CONCURRENCY = 20    # parallel screening requests
_OI_URL             = f"{settings.TRADIER_BASE_URL}/v1/markets/options/chains"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ScreenResult:
    eligible:   list[str]        = field(default_factory=list)
    ineligible: list[str]        = field(default_factory=list)
    priority:   list[str]        = field(default_factory=list)
    screened:   int              = 0
    duration_s: float            = 0.0
    source:     str              = "screener"

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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def screen_universe(symbols: list[str]) -> ScreenResult:
    """
    Screen symbols for stream eligibility.

    Priority symbols always pass. Remaining symbols are screened
    in parallel batches via Tradier options chains endpoint.
    Falls back to UNIVERSE_STREAM_ELIGIBLE_DEFAULT on any error.
    """
    start = time.monotonic()
    result = ScreenResult()

    if not symbols:
        log.warning("universe_screener: called with empty symbol list")
        return result

    priority_set = set(settings.priority_symbols)

    # Partition into priority (always eligible) and candidates
    priority   = [s for s in symbols if s in priority_set]
    candidates = [s for s in symbols if s not in priority_set]

    result.priority  = priority
    result.eligible  = list(priority)   # priority always streams

    log.info(
        "universe_screener: %d priority symbols auto-eligible, screening %d candidates",
        len(priority), len(candidates),
    )

    if candidates:
        if settings.TRADIER_API_KEY:
            screened_eligible, screened_count = await _screen_candidates(candidates)
            result.eligible.extend(screened_eligible)
            ineligible_set = set(candidates) - set(screened_eligible)
            result.ineligible = list(ineligible_set)
            result.screened = screened_count
        else:
            # No API key — apply default flag to all candidates
            log.warning(
                "universe_screener: TRADIER_API_KEY not set — "
                "applying default eligible=%s to %d candidates",
                settings.UNIVERSE_STREAM_ELIGIBLE_DEFAULT, len(candidates),
            )
            if settings.UNIVERSE_STREAM_ELIGIBLE_DEFAULT:
                result.eligible.extend(candidates)
            else:
                result.ineligible.extend(candidates)

    result.duration_s = time.monotonic() - start
    log.info(
        "universe_screener: complete — eligible=%d ineligible=%d screened=%d duration=%.2fs",
        len(result.eligible), len(result.ineligible),
        result.screened, result.duration_s,
    )
    return result


async def get_stream_eligible(symbols: list[str]) -> list[str]:
    """Convenience wrapper — returns only the eligible symbol list."""
    result = await screen_universe(symbols)
    return result.eligible


# ---------------------------------------------------------------------------
# Internal — batch screening
# ---------------------------------------------------------------------------

async def _screen_candidates(candidates: list[str]) -> tuple[list[str], int]:
    """
    Screen candidate symbols in parallel batches.
    Returns (eligible_list, total_screened_count).
    """
    sem          = asyncio.Semaphore(_SCREEN_CONCURRENCY)
    delay_s      = settings.UNIVERSE_BATCH_DELAY_MS / 1000.0
    eligible     = []
    screened     = 0

    async def _check(symbol: str) -> Optional[str]:
        async with sem:
            return await _is_stream_eligible(symbol)

    # Process in batches with optional inter-batch delay
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
    """
    Returns symbol if it has open interest on any near-term option chain,
    None otherwise.

    Falls back to UNIVERSE_STREAM_ELIGIBLE_DEFAULT on any network/parse error.
    """
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

        # Eligible if any contract has open_interest > 0
        for contract in option:
            if (contract.get("open_interest") or 0) > 0:
                return symbol

        # No OI found — apply default
        return symbol if settings.UNIVERSE_STREAM_ELIGIBLE_DEFAULT else None

    except Exception as e:
        log.debug("universe_screener._is_stream_eligible(%s) error: %s", symbol, e)
        return symbol if settings.UNIVERSE_STREAM_ELIGIBLE_DEFAULT else None


def _nearest_expiry_param() -> str:
    """
    Returns a near-term expiration string (next Friday) as a rough
    screening anchor. Exact expiry doesn't matter — we just need any
    chain response to check for open interest.
    """
    from datetime import date, timedelta
    today = date.today()
    # Next Friday (weekday 4)
    days_ahead = (4 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    next_friday = today + timedelta(days=days_ahead)
    return next_friday.strftime("%Y-%m-%d")
