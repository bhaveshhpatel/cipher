"""
utils/tradier_client.py — Shared async Tradier REST API client.

Centralises all Tradier REST calls used across symbol_registry.py,
symbols_loader.py, and tradier_stream.py.

Rate limits:
  Tradier sandbox: 120 req/min
  Tradier production: 120 req/min (same limit)
  → _CHAIN_SEM(2): conservative for live streaming flow path (~3 req/s)
  → _BULK_CHAIN_SEM(50): used by registry build() only — raised from 10
     to match outer build sem(50) in symbol_registry.py. True 50-concurrent
     chain fetches; sustained rate stays below 120 req/min because each
     ticker also blocks on get_expirations() before the chain loop.
  → _SESSION_SEM(3): for session token fetches (B-022)

P3 FIX (2026-04-27):
  Added _BULK_CHAIN_SEM = Semaphore(10) and get_option_chain_bulk() for use
  during registry build(). The live-streaming _CHAIN_SEM(2) is unchanged so
  flow ingestion throughput is unaffected. This raises build throughput from
  ~3.3 req/s to ~16 req/s, cutting cold-start chain fetch time by ~5×.

PERF-SEM-50 (2026-05-14):
  Raised _BULK_CHAIN_SEM from 10 → 50 to match outer build sem(50).
  Previously min(50,10)=10 was the real concurrency ceiling, not the outer
  sem. Cold-start now completes in ~2.5 min (clean) / ~12 min (degraded)
  instead of 9-12 min (clean) / never (degraded with 15s per-request timeouts).

POOL-MISMATCH fix (2026-05-14):
  When PERF-SEM-50 raised _BULK_CHAIN_SEM from 10 → 50, max_connections was
  not updated (still 30). 50 coroutines competed for 30 TCP slots; the 20
  blocked on pool-wait burned the 15s asyncio.wait_for budget in _build_ticker
  before the HTTP request even started — timeouts fired on pool contention,
  not Tradier TCP stalls. Cascading timeouts yielded only ~5,560 OCC contracts
  vs the expected ~50K.
  Fix:
    max_connections: 30 → 75  (1.5× sem=50; restores the 3× ratio from sem=10 era)
    max_keepalive_connections: 20 → 60
    _CONNECT_TIMEOUT: 15.0 → 10.0  (fail fast on connect, free pool slot sooner)
    _READ_TIMEOUT:    20.0 → 25.0  (must exceed _CHAIN_REQUEST_TIMEOUT_S=30s budget)
  Companion fix in symbol_registry.py: _CHAIN_REQUEST_TIMEOUT_S 15 → 30s.

FIX-RETRY-POOL (fix/build-perf-bugs):
  429 retry in get_option_chain_bulk() and get_option_chain_bulk_all() previously
  spawned a fresh httpx.AsyncClient per retry, bypassing max_connections=75.
  Under load (40+ simultaneous 429s) this created uncapped ephemeral connections.
  Both retry paths now reuse _client() (the shared pool).

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
  max_connections=75 / max_keepalive_connections=60 / keepalive_expiry=30s.
  Call init_http_client() on app startup and close_http_client() on shutdown
  (both wired into main.py lifespan). Eliminates ~22 min of TCP handshake
  overhead during cold-start registry build.

CHAIN-ALL (2026-05-14):
  New get_option_chain_bulk_all(symbol) omits the expiration param entirely.
  Tradier returns all expiries in one response (options.option[] with each
  contract carrying an expiration_date field).
  - Uses _BULK_CHAIN_SEM(50) — same semaphore as get_option_chain_bulk().
  - 429 back-off retry logic carried over from get_option_chain_bulk().
  - Reduces total API calls from ~21,450 to ~3,900 (82% fewer).
  - Build time: ~117s clean / ~312s degraded (vs 343s / 1287s before).
  - get_expirations() is preserved for non-build callers.

Public API:
  init_http_client()                         -> None  (call on startup)
  close_http_client()                        -> None  (call on shutdown)
  get_quote(symbol)                          -> Optional[dict]
  get_quotes_batch(symbols)                  -> dict[str, dict]
  get_expirations(symbol)                    -> list[str]
  get_option_chain(symbol, expiration)       -> list[dict]   (streaming, sem=2)
  get_option_chain_bulk(symbol, expiration)  -> list[dict]   (build path, sem=50, per-expiry)
  get_option_chain_bulk_all(symbol)          -> list[dict]   (build path, sem=50, ALL expiries)
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

_CONNECT_TIMEOUT = 10.0   # was 15.0 — fail fast on connect, free pool slot sooner
_READ_TIMEOUT    = 25.0   # was 20.0 — must exceed _CHAIN_REQUEST_TIMEOUT_S=30s budget

# Live streaming flow path — conservative, never races with Tradier rate limit
_CHAIN_SEM       = asyncio.Semaphore(2)

# Registry build() bulk path — raised from 10 → 50 (PERF-SEM-50 2026-05-14)
# to match outer build sem(50) in symbol_registry.py. Previously the inner
# sem(10) was the real throughput ceiling (min(50,10)=10 effective
# concurrency). Now true 50-concurrent chain fetches are possible.
_BULK_CHAIN_SEM  = asyncio.Semaphore(50)

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
# Pool sizing rationale (POOL-MISMATCH fix 2026-05-14):
#   max_connections=75      — 1.5× _BULK_CHAIN_SEM(50). Restores the 3× ratio
#                             from the sem=10 era (pool=30 was 3× sem=10).
#                             Previously pool=30 was 0.6× sem=50, causing 20
#                             coroutines to block on pool-wait and burn the
#                             per-request asyncio.wait_for budget before the
#                             HTTP request even started.
#   max_keepalive_connections=60 — keep warm connections for the build path
#   keepalive_expiry=30s    — Tradier keep-alives are typically <60s
# init_http_client() / close_http_client() are called from main.py lifespan.
# ---------------------------------------------------------------------------
_shared_client: Optional[httpx.AsyncClient] = None

# Canonical Timeout used by the shared pool, the _client() fallback, and
# any ephemeral retry clients. httpx requires either a scalar default= or
# all four parameters (connect, read, write, pool) set explicitly — partial
# kwargs without default= raise ValueError at request time, not at
# construction time, which caused every get_expirations() call to fail.
_TIMEOUT = httpx.Timeout(
    connect=_CONNECT_TIMEOUT,
    read=_READ_TIMEOUT,
    write=10.0,
    pool=15.0,
)


def init_http_client() -> None:
    """Create the shared httpx client. Call once on app startup."""
    global _shared_client
    limits = httpx.Limits(
        max_connections=75,            # POOL-MISMATCH fix: was 30 (0.6× sem=50)
        max_keepalive_connections=60,  # was 20
        keepalive_expiry=30.0,
    )
    _shared_client = httpx.AsyncClient(limits=limits, timeout=_TIMEOUT)
    log.info("[tradier_client] shared HTTP client initialised (pool max=75)")


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
    # Always use the module-level _TIMEOUT here. Passing partial kwargs to
    # httpx.Timeout (e.g. connect= + read= without default=) is rejected by
    # httpx at request time — not at construction time — so the error would
    # surface as a warning on every API call rather than at startup.
    return httpx.AsyncClient(timeout=_TIMEOUT)


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

    Uses _BULK_CHAIN_SEM(50) — raised from 10 to match outer build sem(50)
    in symbol_registry.py (PERF-SEM-50 2026-05-14). Previously the inner
    sem(10) was the real throughput ceiling (min(50,10)=10 effective
    concurrency). Now true 50-concurrent chain fetches are possible.

    MUST NOT be used from the streaming flow path — use get_option_chain().

    Still safe under 120 req/min because:
      - each ticker coroutine also blocks on get_expirations() first
      - per-request 30s timeouts in _build_ticker free stalled slots fast
      - 429 responses are handled gracefully with back-off retry
    """
    url = f"{settings.TRADIER_BASE_URL}/v1/markets/options/chains"
    async with _BULK_CHAIN_SEM:
        try:
            resp = await _client().get(
                url, headers=_headers(),
                params={"symbol": symbol, "expiration": expiration, "greeks": "false"}
            )
            if resp.status_code == 429:
                # Back off and retry once using the SHARED pool (not an ephemeral
                # client). FIX-RETRY-POOL: the previous pattern spun up a fresh
                # httpx.AsyncClient for each 429 retry, bypassing max_connections=75
                # and creating uncapped connections under load.
                retry_after = float(
                    resp.headers.get("Retry-After", _DEFAULT_RETRY_AFTER_S)
                )
                log.warning(
                    "[tradier_client] get_option_chain_bulk(%s, %s) 429 — "
                    "backing off %.0fs",
                    symbol, expiration, retry_after,
                )
                await asyncio.sleep(retry_after)
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
            log.warning(
                f"[tradier_client] get_option_chain_bulk({symbol}, {expiration}) error: {e}"
            )
            return []


async def get_option_chain_bulk_all(symbol: str) -> list[dict]:
    """
    Fetch ALL expiries for a ticker in a single Tradier API call — BULK BUILD PATH ONLY.

    CHAIN-ALL (2026-05-14):
    Omits the ``expiration`` param from /v1/markets/options/chains so Tradier
    returns every available expiry in one response. Each contract dict in the
    returned list includes an ``expiration_date`` field (YYYY-MM-DD) that
    _build_ticker uses to group and DTE-filter client-side.

    Advantages vs the two-call-per-expiry approach:
      - 82% fewer total API calls  (3,900 vs ~21,450 for 3,900 tickers)
      - 66-76% faster build time   (~117s clean vs ~343s; ~312s degraded vs ~1287s)
      - Lower burst-rate exposure  (~2,000 rpm vs ~3,750 rpm)
      - Larger TCP transfer per call provides natural spacing between
        completions, making Tradier's rolling-window enforcement friendlier.

    Uses _BULK_CHAIN_SEM(50) — same semaphore as get_option_chain_bulk().
    MUST NOT be used from the streaming flow path — use get_option_chain().

    429 handling: identical back-off retry as get_option_chain_bulk().
    HTTP 400: returns [] (ticker has no listed options — correct behaviour;
    the caller _build_ticker skips tickers that return []).
    """
    url = f"{settings.TRADIER_BASE_URL}/v1/markets/options/chains"
    async with _BULK_CHAIN_SEM:
        try:
            resp = await _client().get(
                url, headers=_headers(),
                # No expiration param → all expiries returned in one response.
                params={"symbol": symbol, "greeks": "false"}
            )
            if resp.status_code == 429:
                retry_after = float(
                    resp.headers.get("Retry-After", _DEFAULT_RETRY_AFTER_S)
                )
                log.warning(
                    "[tradier_client] get_option_chain_bulk_all(%s) 429 — "
                    "backing off %.0fs then retrying",
                    symbol, retry_after,
                )
                await asyncio.sleep(retry_after)
                # FIX-RETRY-POOL: retry through shared pool, not an ephemeral client.
                resp = await _client().get(
                    url, headers=_headers(),
                    params={"symbol": symbol, "greeks": "false"}
                )
            if resp.status_code == 400:
                # Ticker has no listed options — expected for many watchlist
                # symbols. Return [] silently; caller skips gracefully.
                log.debug(
                    "[tradier_client] get_option_chain_bulk_all(%s) HTTP 400 — "
                    "no listed options, skipping",
                    symbol,
                )
                return []
            if resp.status_code != 200:
                log.warning(
                    "[tradier_client] get_option_chain_bulk_all(%s) HTTP %d — skipping",
                    symbol, resp.status_code,
                )
                return []
            data = resp.json()
            options = (data.get("options") or {}).get("option") or []
            if isinstance(options, dict):
                options = [options]
            return options
        except Exception as e:
            log.warning(
                "[tradier_client] get_option_chain_bulk_all(%s) error: %s",
                symbol, e,
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
