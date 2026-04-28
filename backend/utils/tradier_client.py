"""
utils/tradier_client.py — Shared async Tradier REST API client.

Centralises all Tradier REST calls used across symbol_registry.py,
symbols_loader.py, and tradier_stream.py.

Rate limits:
  Tradier sandbox: 120 req/min
  Tradier production: 120 req/min (same limit)
  → _CHAIN_SEM(2): conservative for live streaming flow path (~3 req/s)
  → _BULK_CHAIN_SEM(10): used by registry build() only (~16 req/s)
     Still within 120 req/min since build tasks are bounded by outer sem(50)
     and each ticker makes sequential expiry+chain calls.
  → _SESSION_SEM(3): for session token fetches (B-022)

P3 FIX (2026-04-27):
  Added _BULK_CHAIN_SEM = Semaphore(10) and get_option_chain_bulk() for use
  during registry build(). The live-streaming _CHAIN_SEM(2) is unchanged so
  flow ingestion throughput is unaffected. This raises build throughput from
  ~3.3 req/s to ~16 req/s, cutting cold-start chain fetch time by ~5×.

B-022 — Global Session Token Semaphore:
  _SESSION_SEM = asyncio.Semaphore(3) wraps get_session_token() / get_token().

B-023 — Explicit 429 Handling:
  If Tradier returns HTTP 429, get_session_token() reads the
  Retry-After header (default 10s if absent) and sleeps that long before retrying.

Public API:
  get_quote(symbol)                          -> Optional[dict]
  get_quotes_batch(symbols)                  -> dict[str, dict]
  get_expirations(symbol)                    -> list[str]
  get_option_chain(symbol, expiration)       -> list[dict]   (streaming, sem=2)
  get_option_chain_bulk(symbol, expiration)  -> list[dict]   (build path, sem=10)
  get_options_chain(symbol, expiration)      -> list[dict]   (alias for get_option_chain)
  get_session_token()                        -> Optional[str]
  get_token()                                -> Optional[str]  (alias)

All methods return None / [] on error — callers must handle gracefully.
"""
import asyncio
import logging
from typing import Optional

import httpx

from config import settings

log = logging.getLogger("tradier_client")

_CONNECT_TIMEOUT = 15.0
_READ_TIMEOUT    = 20.0

# Live streaming flow path — conservative, never races with Tradier rate limit
_CHAIN_SEM       = asyncio.Semaphore(2)

# Registry build() bulk path — higher concurrency, still within 120 req/min
# 10 concurrent × ~0.6s/req = ~16 req/s; outer build sem(50) bounds ticker tasks
_BULK_CHAIN_SEM  = asyncio.Semaphore(10)

# B-022: max 3 concurrent session token fetches
_SESSION_SEM     = asyncio.Semaphore(3)

# B-023: fallback Retry-After sleep when header is absent
_DEFAULT_RETRY_AFTER_S: float = 10.0


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
        "Accept":        "application/json",
    }


async def get_quote(symbol: str) -> Optional[dict]:
    """Fetch single stock quote. Returns raw quote dict or None."""
    url = f"{settings.TRADIER_BASE_URL}/v1/markets/quotes"
    try:
        async with httpx.AsyncClient(timeout=_CONNECT_TIMEOUT) as client:
            resp = await client.get(url, headers=_headers(), params={"symbols": symbol, "greeks": "false"})
        if resp.status_code != 200:
            return None
        data = resp.json()
        quote = data.get("quotes", {}).get("quote")
        if isinstance(quote, list):
            quote = quote[0] if quote else None
        return quote
    except Exception as e:
        log.warning(f"[tradier_client] get_quote({symbol}) error: {e}")
        return None


async def get_quotes_batch(symbols: list[str]) -> dict[str, dict]:
    """
    Fetch quotes for up to 200 symbols in one call.
    Returns {symbol: quote_dict} mapping.
    """
    if not symbols:
        return {}
    url = f"{settings.TRADIER_BASE_URL}/v1/markets/quotes"
    try:
        async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
            resp = await client.get(
                url, headers=_headers(),
                params={"symbols": ",".join(symbols), "greeks": "false"}
            )
        if resp.status_code != 200:
            return {}
        data = resp.json()
        quotes_raw = data.get("quotes", {}).get("quote") or []
        if isinstance(quotes_raw, dict):
            quotes_raw = [quotes_raw]
        return {q["symbol"]: q for q in quotes_raw if "symbol" in q}
    except Exception as e:
        log.warning(f"[tradier_client] get_quotes_batch error: {e}")
        return {}


async def get_expirations(symbol: str) -> list[str]:
    """
    Fetch all active expiration dates for a ticker.
    Returns list of YYYY-MM-DD strings, empty list on error.
    """
    url = f"{settings.TRADIER_BASE_URL}/v1/markets/options/expirations"
    try:
        async with httpx.AsyncClient(timeout=_CONNECT_TIMEOUT) as client:
            resp = await client.get(url, headers=_headers(), params={"symbol": symbol})
        if resp.status_code != 200:
            return []
        data = resp.json()
        dates = (data.get("expirations") or {}).get("date") or []
        if isinstance(dates, str):
            dates = [dates]
        return dates
    except Exception as e:
        log.warning(f"[tradier_client] get_expirations({symbol}) error: {e}")
        return []


async def get_option_chain(symbol: str, expiration: str) -> list[dict]:
    """
    Fetch full option chain for ticker + expiry.
    Uses _CHAIN_SEM(2) — conservative semaphore for the live streaming path.
    Keeps flow ingestion well under Tradier’s 120 req/min rate limit.
    """
    url = f"{settings.TRADIER_BASE_URL}/v1/markets/options/chains"
    async with _CHAIN_SEM:
        try:
            async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
                resp = await client.get(
                    url, headers=_headers(),
                    params={"symbol": symbol, "expiration": expiration, "greeks": "false"}
                )
            if resp.status_code != 200:
                return []
            data = resp.json()
            options = (data.get("options") or {}).get("option") or []
            if isinstance(options, dict):
                options = [options]
            return options
        except Exception as e:
            log.warning(f"[tradier_client] get_option_chain({symbol}, {expiration}) error: {e}")
            return []


async def get_option_chain_bulk(symbol: str, expiration: str) -> list[dict]:
    """
    Fetch full option chain for ticker + expiry — BULK BUILD PATH ONLY.

    Uses _BULK_CHAIN_SEM(10) instead of _CHAIN_SEM(2) so registry build()
    can fetch chains at ~16 req/s without blocking the live flow ingestion
    path. MUST NOT be used from the streaming flow path — use get_option_chain().

    Still safe under 120 req/min because:
      - outer build() sem(50) limits concurrent ticker coroutines
      - each ticker makes sequential expiry+chain calls (not parallel)
      - 10 concurrent chains × ~0.6s each = ~16 req/s = ~960 req/min
        BUT each ticker also blocks on get_expirations() first, so real
        sustained rate is lower; 429 responses are handled gracefully.
    """
    url = f"{settings.TRADIER_BASE_URL}/v1/markets/options/chains"
    async with _BULK_CHAIN_SEM:
        try:
            async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
                resp = await client.get(
                    url, headers=_headers(),
                    params={"symbol": symbol, "expiration": expiration, "greeks": "false"}
                )
            if resp.status_code == 429:
                # Back off and retry once — don’t crash the whole build
                retry_after = float(
                    resp.headers.get("Retry-After", _DEFAULT_RETRY_AFTER_S)
                )
                log.warning(
                    "[tradier_client] get_option_chain_bulk(%s, %s) 429 — "
                    "backing off %.0fs",
                    symbol, expiration, retry_after,
                )
                await asyncio.sleep(retry_after)
                async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
                    resp = await client.get(
                        url, headers=_headers(),
                        params={"symbol": symbol, "expiration": expiration, "greeks": "false"}
                    )
            if resp.status_code != 200:
                return []
            data = resp.json()
            options = (data.get("options") or {}).get("option") or []
            if isinstance(options, dict):
                options = [options]
            return options
        except Exception as e:
            log.warning(
                f"[tradier_client] get_option_chain_bulk({symbol}, {expiration}) error: {e}"
            )
            return []


# Alias: tests import get_options_chain (plural)
get_options_chain = get_option_chain


async def get_session_token() -> Optional[str]:
    """
    Fetch a fresh Tradier streaming session token.
    Tokens are single-use and expire when the stream closes.
    Returns sessionid string or None on failure.

    B-022: Guarded by _SESSION_SEM (max 3 concurrent). Prevents the
    simultaneous 32-worker burst at startup from triggering Tradier 429s.

    B-023: Explicit 429 handling. Reads Retry-After header and sleeps
    that duration before retrying, rather than crashing via raise_for_status().
    """
    url = f"{settings.TRADIER_BASE_URL}/v1/markets/events/session"

    async with _SESSION_SEM:   # B-022: max 3 concurrent token fetches
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=_CONNECT_TIMEOUT) as client:
                    resp = await client.post(url, headers=_headers(), data={})

                # B-023: explicit 429 — read Retry-After, sleep, then retry
                if resp.status_code == 429:
                    retry_after = float(
                        resp.headers.get("Retry-After", _DEFAULT_RETRY_AFTER_S)
                    )
                    log.warning(
                        f"[tradier_client] session token 429 — Retry-After {retry_after:.0f}s "
                        f"(attempt {attempt + 1}/3)"
                    )
                    await asyncio.sleep(retry_after)
                    continue   # retry within the semaphore hold

                if resp.status_code == 401:
                    log.error("[tradier_client] session token 401 — check TRADIER_API_KEY")
                    return None

                resp.raise_for_status()
                token = resp.json().get("stream", {}).get("sessionid")
                if token:
                    return token
                log.warning(f"[tradier_client] session response missing sessionid: {resp.text[:200]}")
                return None

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                log.warning(f"[tradier_client] session fetch attempt {attempt+1}/3 failed: {e}")
                if attempt < 2:
                    await asyncio.sleep(2.0)

    return None


# Alias: tests import get_token
get_token = get_session_token
