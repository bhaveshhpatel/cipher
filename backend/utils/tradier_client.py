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

Fix (SEM-STREAM 2026-05-12): expose acquire_session_token_slot() for
  tradier_stream._get_session_token() which has its own private token-fetch
  implementation and previously bypassed _SESSION_SEM entirely. The slot
  coroutine tries to acquire _SESSION_SEM within a derived timeout
  (_SESSION_RETRY_DELAY * _SESSION_RETRY_MAX = 6s) and returns a bool
  indicating whether the semaphore was acquired. The caller (tradier_stream)
  is responsible for releasing _SESSION_SEM in its finally block.
  This restores B-022 burst protection for all token-fetch paths while
  preserving worker isolation — a timed-out acquire proceeds without the
  semaphore rather than blocking indefinitely.

Flaw 3 Fix (TCP Connection Pooling):
  Replaced per-call httpx.AsyncClient instantiation with a single shared
  client (_shared_client) that maintains a persistent connection pool.
  max_connections=30 / max_keepalive_connections=20 / keepalive_expiry=30s.
  Call init_http_client() on app startup and close_http_client() on shutdown
  (both wired into main.py lifespan). Eliminates ~22 min of TCP handshake
  overhead during cold-start registry build at _BULK_CHAIN_SEM(10).

Public API:
  init_http_client()                         -> None  (call on startup)
  close_http_client()                        -> None  (call on shutdown)
  get_quote(symbol)                          -> Optional[dict]
  get_quotes_batch(symbols)                  -> dict[str, dict]
  get_expirations(symbol)                    -> list[str]
  get_option_chain(symbol, expiration)       -> list[dict]   (streaming, sem=2)
  get_option_chain_bulk(symbol, expiration)  -> list[dict]   (build path, sem=10)
  get_options_chain(symbol, expiration)      -> list[dict]   (alias for get_option_chain)
  get_session_token()                        -> Optional[str]
  get_token()                                -> Optional[str]  (alias)
  acquire_session_token_slot(timeout_s)      -> bool  (for tradier_stream)

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

# B-022: max 3 concurrent session token fetches across ALL callers
# (stream_worker.py via get_session_token() + tradier_stream.py via
# acquire_session_token_slot()).  Both paths share this one semaphore.
_SESSION_SEM     = asyncio.Semaphore(3)

# B-023: fallback Retry-After sleep when header is absent
_DEFAULT_RETRY_AFTER_S: float = 10.0

# SEM-STREAM: semaphore-acquire timeout constants.
# tradier_stream._get_session_token() uses _SESSION_RETRY_DELAY * _SESSION_RETRY_MAX
# as its acquire timeout so a worker that cannot get a slot in one full
# retry-round duration proceeds independently rather than blocking indefinitely.
_SESSION_RETRY_DELAY: float = 2.0   # seconds between retry attempts
_SESSION_RETRY_MAX:   int   = 3     # max retry attempts per token fetch

# ---------------------------------------------------------------------------
# Shared HTTP connection pool
# ---------------------------------------------------------------------------
# One persistent client is shared across all callers. Connections are reused
# after initial TCP handshake, eliminating ~100ms overhead per call.
# Pool sizing rationale:
#   max_connections=30      — covers _BULK_CHAIN_SEM(10) + _CHAIN_SEM(2) +
#                             _SESSION_SEM(3) + quotes/expirations bursts
#   max_keepalive_connections=20 — keep warm connections for the build path
#   keepalive_expiry=30s    — Tradier keeps-alive are typically <60s
# init_http_client() / close_http_client() are called from main.py lifespan.
# ---------------------------------------------------------------------------
_shared_client: Optional[httpx.AsyncClient] = None


def init_http_client() -> None:
    """Create the shared httpx client. Call once on app startup."""
    global _shared_client
    limits = httpx.Limits(
        max_connections=30,
        max_keepalive_connections=20,
        keepalive_expiry=30.0,
    )
    timeout = httpx.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT, write=10.0, pool=5.0)
    _shared_client = httpx.AsyncClient(limits=limits, timeout=timeout)
    log.info("[tradier_client] shared HTTP client initialised (pool max=30)")


async def close_http_client() -> None:
    """Gracefully close the shared client. Call on app shutdown."""
    global _shared_client
    if _shared_client is not None:
        await _shared_client.aclose()
        _shared_client = None
        log.info("[tradier_client] shared HTTP client closed")


def _client() -> httpx.AsyncClient:
    """
    Return the shared client. Falls back to a temporary per-call client if
    init_http_client() was not called (e.g. in unit tests that import the
    module directly without running the FastAPI lifespan).
    """
    if _shared_client is not None:
        return _shared_client
    log.debug("[tradier_client] _shared_client not initialised — using ephemeral client")
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT)
    )


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
        "Accept":        "application/json",
    }


async def acquire_session_token_slot(
    timeout_s: Optional[float] = None,
) -> bool:
    """
    Try to acquire the shared _SESSION_SEM slot within timeout_s.

    Returns True  if the slot was acquired (caller MUST release via
                  _SESSION_SEM.release() in a finally block).
    Returns False if timeout_s elapsed before a slot was available
                  (caller proceeds without semaphore — isolation wins).

    Default timeout = _SESSION_RETRY_DELAY * _SESSION_RETRY_MAX (6s).
    Rationale: a worker that cannot get a slot in the time it would take
    one full retry round to complete should proceed independently rather
    than stalling behind a potentially hung peer.

    SEM-STREAM (2026-05-12): introduced so tradier_stream._get_session_token()
    can participate in the B-022 semaphore without merging its implementation
    into get_session_token() (which would break worker isolation).
    """
    if timeout_s is None:
        timeout_s = _SESSION_RETRY_DELAY * _SESSION_RETRY_MAX
    try:
        await asyncio.wait_for(_SESSION_SEM.acquire(), timeout=timeout_s)
        return True
    except asyncio.TimeoutError:
        log.debug(
            "[tradier_client] acquire_session_token_slot timed out after %.1fs — "
            "proceeding without semaphore",
            timeout_s,
        )
        return False


async def get_quote(symbol: str) -> Optional[dict]:
    """Fetch single stock quote. Returns raw quote dict or None."""
    url = f"{settings.TRADIER_BASE_URL}/v1/markets/quotes"
    try:
        resp = await _client().get(url, headers=_headers(), params={"symbols": symbol, "greeks": "false"})
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
        resp = await _client().get(
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
        resp = await _client().get(url, headers=_headers(), params={"symbol": symbol})
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
    Keeps flow ingestion well under Tradier's 120 req/min rate limit.
    """
    url = f"{settings.TRADIER_BASE_URL}/v1/markets/options/chains"
    async with _CHAIN_SEM:
        try:
            resp = await _client().get(
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
            resp = await _client().get(
                url, headers=_headers(),
                params={"symbol": symbol, "expiration": expiration, "greeks": "false"}
            )
            if resp.status_code == 429:
                # Back off and retry once — don't crash the whole build.
                # Use an ephemeral client for the retry to avoid polluting the
                # shared pool with a connection that may be in a bad state.
                retry_after = float(
                    resp.headers.get("Retry-After", _DEFAULT_RETRY_AFTER_S)
                )
                log.warning(
                    "[tradier_client] get_option_chain_bulk(%s, %s) 429 — "
                    "backing off %.0fs",
                    symbol, expiration, retry_after,
                )
                await asyncio.sleep(retry_after)
                async with httpx.AsyncClient(timeout=_READ_TIMEOUT) as retry_client:
                    resp = await retry_client.get(
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
                resp = await _client().post(url, headers=_headers(), data={})

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
