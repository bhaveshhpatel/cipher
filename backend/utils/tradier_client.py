"""
utils/tradier_client.py — Shared async Tradier REST API client.

Centralises all Tradier REST calls used across symbol_registry.py,
symbols_loader.py, and tradier_stream.py.

Rate limits:
  Tradier sandbox: 120 req/min
  Tradier production: 120 req/min (same limit)
  → use asyncio.Semaphore(10) for chain fetches with 429 retry handling
  → use asyncio.Semaphore(3) for session token fetches (B-022)

B-022 — Global Session Token Semaphore:
  _SESSION_SEM = asyncio.Semaphore(3) wraps get_session_token() / get_token().

B-023 — Explicit 429 Handling:
  If Tradier returns HTTP 429, get_session_token() and get_option_chain()
  read the Retry-After header (default 10s if absent) and sleep before retrying.

B-024 — Chain Semaphore raised to 10:
  _CHAIN_SEM raised from 2 → 10 for production Tradier. Combined with 429
  retry handling this brings OCC registry build time from ~33min down to ~8min
  without silently dropping contracts on throttle.

Public API:
  get_quote(symbol)                      -> Optional[dict]
  get_quotes_batch(symbols)              -> dict[str, dict]
  get_expirations(symbol)                -> list[str]
  get_option_chain(symbol, expiration)   -> list[dict]
  get_options_chain(symbol, expiration)  -> list[dict]  (alias)
  get_session_token()                    -> Optional[str]
  get_token()                            -> Optional[str]  (alias)

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
_CHAIN_SEM       = asyncio.Semaphore(10)  # B-024: raised from 2 → 10 for production throughput
_SESSION_SEM     = asyncio.Semaphore(3)   # B-022: max 3 concurrent session token fetches

# B-023: fallback Retry-After sleep when header is absent
_DEFAULT_RETRY_AFTER_S: float = 10.0
_CHAIN_MAX_RETRIES:     int   = 3


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
            log.warning(
                "[tradier_client] get_quote(%s) HTTP %d — body: %s",
                symbol, resp.status_code, resp.text[:300],
            )
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
            log.warning(
                "[tradier_client] get_quotes_batch HTTP %d for %d symbols (first: %s) — body: %s",
                resp.status_code, len(symbols), symbols[0], resp.text[:300],
            )
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
            log.warning(
                "[tradier_client] get_expirations(%s) HTTP %d — body: %s",
                symbol, resp.status_code, resp.text[:300],
            )
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
    Returns list of contract dicts (each has symbol, strike, option_type, open_interest).

    B-024: Uses _CHAIN_SEM(10) for production throughput.
    B-023: Explicit 429 handling — reads Retry-After and retries up to
    _CHAIN_MAX_RETRIES times instead of silently returning [].
    """
    url = f"{settings.TRADIER_BASE_URL}/v1/markets/options/chains"
    async with _CHAIN_SEM:
        for attempt in range(_CHAIN_MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as client:
                    resp = await client.get(
                        url, headers=_headers(),
                        params={"symbol": symbol, "expiration": expiration, "greeks": "false"}
                    )

                if resp.status_code == 429:
                    retry_after = float(
                        resp.headers.get("Retry-After", _DEFAULT_RETRY_AFTER_S)
                    )
                    log.warning(
                        "[tradier_client] get_option_chain(%s, %s) 429 — "
                        "Retry-After %.0fs (attempt %d/%d)",
                        symbol, expiration, retry_after, attempt + 1, _CHAIN_MAX_RETRIES,
                    )
                    await asyncio.sleep(retry_after)
                    continue

                if resp.status_code != 200:
                    log.warning(
                        "[tradier_client] get_option_chain(%s, %s) HTTP %d — body: %s",
                        symbol, expiration, resp.status_code, resp.text[:300],
                    )
                    return []

                data = resp.json()
                options = (data.get("options") or {}).get("option") or []
                if isinstance(options, dict):
                    options = [options]
                return options

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                log.warning(
                    "[tradier_client] get_option_chain(%s, %s) network error (attempt %d/%d): %s",
                    symbol, expiration, attempt + 1, _CHAIN_MAX_RETRIES, e,
                )
                if attempt < _CHAIN_MAX_RETRIES - 1:
                    await asyncio.sleep(2.0)
            except Exception as e:
                log.warning(
                    "[tradier_client] get_option_chain(%s, %s) unexpected error: %s",
                    symbol, expiration, e,
                )
                return []

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
                    continue

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
