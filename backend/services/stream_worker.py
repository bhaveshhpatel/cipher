"""
services/stream_worker.py — Single stream connection lifecycle.

Summary of active fixes
-----------------------
STREAM-3 (2026-04-28):
  Lock removed. Workers connect in parallel — all 31,920 symbols covered
  from T+0. One shared session token from StreamManager; each worker opens
  its own POST stream concurrently under the same Tradier sessionid.

  Logging added for deep observability:
    CONNECT      — worker connected, symbols count, session prefix
    FIRST_TICK   — full untruncated first JSON payload for shape inspection
    STREAM_STATS — per-worker every 30s: ticks, rate/s, errors, reconnects,
                   uptime, last_tick_ago, queue_depth
    API_ERROR    — any {"error":...} payload from Tradier with full context
    RECONNECT    — backoff duration, attempt count, session_ticks on last conn
    STALL        — logged when no tick received for >30s on an active stream
    401          — token expired, _token_expired flag set, clean exit

B-021: startup_delay_s for staggered startup (50ms × worker_id).
B-008: global stats rollup via _global_stats().

STREAM-6 (2026-04-30):
  Increase _IDLE_TIMEOUT 30s → 120s. With 64 workers × 500 symbols at
  ~0.6 ticks/s total, each worker statistically receives a tick every ~110s.
  The 30s timeout was causing constant false-stall reconnects on quiet symbol
  sets, producing stalled=63 in STREAM_HEALTH and hammering Tradier with
  unnecessary reconnect churn.

APEX-S2 (2026-05-01):
  Wires tier_engine → ThresholdReconciler into the hot path.

  tick_to_metrics(tick, avg_volume) — pure module-level function.
    Filters non-timesale payloads first. Returns SymbolMetrics | None.
    Named field constants (_TICK_*) protect against Tradier key renames.
    oi_delta is ALWAYS 0.0 from timesale events (OI is a chain-level field
    only available via chain_store). VOLUME_SURGE and PREMIUM_FLOOD breach
    types are active; OI breach types (OI_SPIKE, OI_COLLAPSE) are suppressed
    in S2. Filed as S3 enrichment gate (Issue #20).

  _tier_map_cache / _get_tier_map() — module-level, 5-min TTL.
    Cold start returns {} immediately (reconciler falls back to T3).
    Stale refresh is a background asyncio.create_task — never blocks the
    stream reader. int tier (1/2/3) converted to str (T1/T2/T3) at this
    boundary so ThresholdReconciler.reconcile() receives the expected format.

  _pending accumulator + _flush_loop() per worker.
    Ticks accumulate into dict[str, SymbolMetrics] (last-write-wins).
    Flushed every _RECONCILE_INTERVAL_S = 5.0s via asyncio.create_task.
    Atomic drain: pending dict swapped before await so in-flight ticks
    go to the next window, not a shared dict being iterated.
"""
import asyncio
import json
import logging
import random
import time as _time
from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from config import settings
from utils.tradier_client import get_session_token

log = logging.getLogger("stream_worker")

_ET = ZoneInfo("America/New_York")
_MARKET_OPEN  = time(9, 30)
_MARKET_CLOSE = time(16, 0)

_IDLE_TIMEOUT          = 120.0   # STREAM-6: raised from 30s
_CONNECT_TIMEOUT       = 15.0
_BACKOFF_BASE          = 1.0
_BACKOFF_CAP           = 10.0
_MARKET_CLOSED_SLEEP_S = 300.0
_STATS_INTERVAL_S      = 30.0
_STALL_LOG_INTERVAL_S  = 60.0

# ---------------------------------------------------------------------------
# APEX-S2: tick field name constants
# ---------------------------------------------------------------------------
_TICK_TYPE        = "type"
_TICK_SYMBOL      = "symbol"
_TICK_LAST        = "last"
_TICK_SIZE        = "size"
_TICK_VOLUME      = "volume"
_TICK_OI          = "open_interest"
_TICK_TIMESTAMP   = "timestamp"
_TICK_TYPE_TIMESALE = "timesale"

# ---------------------------------------------------------------------------
# APEX-S2: tier_map module-level cache
# ---------------------------------------------------------------------------
_TIER_MAP_TTL         = 300.0   # 5 minutes — matches tier_engine threshold TTL
_RECONCILE_INTERVAL_S = 5.0     # flush window per worker

_tier_map_cache: dict[str, str] = {}   # symbol → "T1" | "T2" | "T3"
_tier_map_ts:    float          = 0.0
_tier_map_refresh_task: Optional[asyncio.Task] = None


def _int_tier_to_str(t: int) -> str:
    """Convert tier_engine int tier (1/2/3) to reconciler string (T1/T2/T3)."""
    return f"T{t}" if t in (1, 2, 3) else "T3"


async def _refresh_tier_map() -> None:
    """Background task: rebuild tier_map from tier_engine + symbol_registry."""
    global _tier_map_cache, _tier_map_ts
    try:
        from services.symbol_registry import get_registry
        from services.tier_engine import assign_tiers

        registry = get_registry()
        if registry is None or not registry.is_ready():
            log.debug("[stream_worker] _refresh_tier_map: registry not ready — skip")
            return

        from services.symbols_loader import SymbolQuote
        quotes: list[SymbolQuote] = []
        for ticker in registry._watchlist:
            avg_vol = registry._avg_volume_by_ticker.get(ticker, 0)
            price   = registry.stock_price(ticker)
            oi      = registry._oi_by_ticker.get(ticker, 0)
            quotes.append(SymbolQuote(
                symbol         = ticker,
                last_price     = price,
                volume         = 0,
                average_volume = avg_vol,
                open_interest  = oi,
            ))

        int_map: dict[str, int] = await assign_tiers(quotes)
        _tier_map_cache = {sym: _int_tier_to_str(t) for sym, t in int_map.items()}
        _tier_map_ts    = _time.monotonic()
        log.info(
            "[stream_worker] tier_map refreshed: %d symbols "
            "(T1=%d T2=%d T3=%d)",
            len(_tier_map_cache),
            sum(1 for v in _tier_map_cache.values() if v == "T1"),
            sum(1 for v in _tier_map_cache.values() if v == "T2"),
            sum(1 for v in _tier_map_cache.values() if v == "T3"),
        )
    except Exception as exc:
        log.warning("[stream_worker] _refresh_tier_map error (non-fatal): %s", exc)


def _get_tier_map() -> dict[str, str]:
    """
    Return cached tier_map. If stale, schedule a background refresh and
    return the current (possibly empty) cache immediately — never blocks.
    Cold start: returns {} → reconciler falls back to T3 for all symbols.
    """
    global _tier_map_refresh_task
    now = _time.monotonic()
    if (now - _tier_map_ts) >= _TIER_MAP_TTL:
        # Only schedule one refresh at a time
        if _tier_map_refresh_task is None or _tier_map_refresh_task.done():
            try:
                _tier_map_refresh_task = asyncio.create_task(
                    _refresh_tier_map(),
                    name="tier_map_refresh",
                )
            except RuntimeError:
                # No running event loop (e.g. during unit tests that call
                # _get_tier_map() synchronously) — return cache as-is.
                pass
    return dict(_tier_map_cache)


# ---------------------------------------------------------------------------
# APEX-S2: tick → SymbolMetrics
# ---------------------------------------------------------------------------

def tick_to_metrics(tick: dict, avg_volume: float = 1.0):
    """
    Convert a raw Tradier timesale tick to a SymbolMetrics instance.

    Returns None if:
      - tick type is not "timesale"
      - symbol key is missing or empty
      - last price is missing, non-numeric, or <= 0

    oi_delta is always 0.0 — Tradier timesale events do not carry OI.
    OI is a chain-level field. OI_SPIKE / OI_COLLAPSE breach types are
    suppressed in S2. S3 enrichment wires oi_delta from chain_store.

    avg_volume parameter: pass symbol's average_volume from symbol_registry.
    Falls back to 1.0 if not provided or if provided value is 0.
    volume_ratio = tick_volume / avg_volume.
    """
    from services.threshold_reconciliation import SymbolMetrics

    # Filter: only process timesale events
    if tick.get(_TICK_TYPE) != _TICK_TYPE_TIMESALE:
        return None

    symbol = tick.get(_TICK_SYMBOL, "")
    if not symbol:
        return None

    # last price: required, must be positive numeric
    try:
        last = float(tick[_TICK_LAST])
    except (KeyError, TypeError, ValueError):
        return None
    if last <= 0:
        return None

    # size: contracts traded in this tick
    try:
        size = float(tick.get(_TICK_SIZE, 0) or 0)
    except (TypeError, ValueError):
        size = 0.0

    # volume: cumulative volume for the session
    try:
        volume = float(tick.get(_TICK_VOLUME, 0) or 0)
    except (TypeError, ValueError):
        volume = 0.0

    # oi_delta: always 0.0 from timesale (see docstring)
    oi_delta: float = 0.0

    # premium_usd: notional per tick = last price × size
    premium_usd = last * size

    # volume_ratio: tick volume vs baseline; guard against zero baseline
    base = avg_volume if avg_volume and avg_volume > 0 else 1.0
    volume_ratio = volume / base

    # timestamp
    try:
        ts = float(tick.get(_TICK_TIMESTAMP, 0) or 0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts <= 0:
        ts = _time.time()

    return SymbolMetrics(
        symbol       = symbol,
        oi_delta     = oi_delta,
        premium_usd  = premium_usd,
        volume_ratio = volume_ratio,
        timestamp    = ts,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_market_hours() -> bool:
    now = datetime.now(_ET)
    if now.weekday() >= 5:
        return False
    return _MARKET_OPEN <= now.time() < _MARKET_CLOSE


def _backoff(attempt: int) -> float:
    delay = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** attempt))
    return random.uniform(0, delay)


def _global_stats() -> dict:
    from services import tradier_stream
    return tradier_stream._stats


# ---------------------------------------------------------------------------
# StreamWorker
# ---------------------------------------------------------------------------

class StreamWorker:
    def __init__(
        self,
        worker_id: int,
        symbols: list[str],
        event_queue: asyncio.Queue,
        startup_delay_s: float = 0.0,
        shared_session_token: Optional[str] = None,
        session_lock: Optional[object] = None,   # kept for API compat, ignored
    ):
        self.worker_id            = worker_id
        self.symbols              = symbols
        self.event_queue          = event_queue
        self.startup_delay_s      = startup_delay_s
        self._shared_token        = shared_session_token
        self._token_expired       = False
        self._running             = True
        self._ticks               = 0
        self._errors              = 0
        self._reconnects          = 0
        self._last_tick_at:       Optional[float] = None
        self._session_ticks:      int   = 0
        self._connect_at:         Optional[float] = None
        self._ticks_at_last_stats: int  = 0
        self._last_stats_at:      float = _time.monotonic()
        self._last_stall_log_at:  float = 0.0

        # APEX-S2: per-worker reconcile accumulator
        self._pending: dict[str, object] = {}   # symbol → SymbolMetrics (last-write-wins)

    def update_symbols(self, new_symbols: list[str]):
        self.symbols = new_symbols

    def stop(self):
        self._running = False

    @property
    def stats(self) -> dict:
        return {
            "worker_id":     self.worker_id,
            "symbols":       len(self.symbols),
            "ticks":         self._ticks,
            "errors":        self._errors,
            "reconnects":    self._reconnects,
            "last_tick_at":  self._last_tick_at,
            "session_ticks": self._session_ticks,
            "token_expired": self._token_expired,
        }

    # ------------------------------------------------------------------
    # Global stats helpers
    # ------------------------------------------------------------------

    def _inc_global_error(self) -> None:
        try:
            _global_stats()["errors"] += 1
        except Exception:
            pass

    def _inc_global_reconnect(self) -> None:
        try:
            s = _global_stats()
            s["reconnects"] = s.get("reconnects", 0) + 1
            s["last_reconnect_at"] = _time.time()
        except Exception:
            pass

    def _inc_global_ticks(self) -> None:
        try:
            s = _global_stats()
            s["ticks"] = s.get("ticks", 0) + 1
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Per-worker stats log
    # ------------------------------------------------------------------

    def _log_stats(self) -> None:
        now_mono = _time.monotonic()
        now_wall = _time.time()
        elapsed  = now_mono - self._last_stats_at
        delta    = self._ticks - self._ticks_at_last_stats
        rate     = delta / elapsed if elapsed > 0 else 0.0
        uptime   = round(now_mono - self._connect_at, 1) if self._connect_at else None
        last_ago = (
            round(now_wall - self._last_tick_at, 1)
            if self._last_tick_at else "never"
        )
        queue_depth = self.event_queue.qsize()

        log.info(
            "[worker-%d] STREAM_STATS | symbols=%d ticks=%d ticks_30s=%d "
            "rate=%.2f/s errors=%d reconnects=%d uptime=%ss "
            "last_tick_ago=%ss queue_depth=%d",
            self.worker_id,
            len(self.symbols),
            self._ticks,
            delta,
            rate,
            self._errors,
            self._reconnects,
            uptime,
            last_ago,
            queue_depth,
        )
        self._ticks_at_last_stats  = self._ticks
        self._last_stats_at        = now_mono

    # ------------------------------------------------------------------
    # APEX-S2: tick processing + reconcile flush
    # ------------------------------------------------------------------

    def _process_tick(self, raw: dict) -> None:
        """
        Convert raw tick to SymbolMetrics and accumulate into _pending.
        avg_volume looked up from symbol_registry; falls back to 1.0.
        """
        symbol = raw.get(_TICK_SYMBOL, "")
        avg_volume = 1.0
        if symbol:
            try:
                from services.symbol_registry import get_registry
                reg = get_registry()
                if reg is not None:
                    avg_volume = float(
                        reg._avg_volume_by_ticker.get(symbol, 0) or 1.0
                    )
            except Exception:
                pass

        metrics = tick_to_metrics(raw, avg_volume=avg_volume)
        if metrics is not None:
            self._pending[metrics.symbol] = metrics

    async def _flush_pending(self) -> None:
        """
        Atomically drain _pending and fire a reconcile call.
        Swap first so ticks arriving during reconcile go to the next window.
        """
        if not self._pending:
            return

        batch, self._pending = self._pending, {}

        try:
            from services.threshold_reconciliation import reconcile
            tier_map = _get_tier_map()
            await reconcile(batch, tier_map)
        except Exception as exc:
            log.warning(
                "[worker-%d] reconcile flush error (non-fatal): %s",
                self.worker_id, exc,
            )

    async def _flush_loop(self) -> None:
        """Periodic flush task running alongside the stream reader."""
        while self._running:
            await asyncio.sleep(_RECONCILE_INTERVAL_S)
            asyncio.create_task(
                self._flush_pending(),
                name=f"reconcile-flush-{self.worker_id}",
            )

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self):
        """Connect, stream, reconnect. Runs until cancelled or self._running=False."""
        if self.startup_delay_s > 0:
            log.debug(
                "[worker-%d] Staggered startup: sleeping %.3fs",
                self.worker_id, self.startup_delay_s,
            )
            await asyncio.sleep(self.startup_delay_s)

        # APEX-S2: start the per-worker reconcile flush loop
        flush_task = asyncio.create_task(
            self._flush_loop(),
            name=f"flush-loop-{self.worker_id}",
        )

        url = f"{settings.TRADIER_STREAM_URL}/v1/markets/events"
        stream_headers = {
            "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
            "Accept":        "application/json",
        }
        reconnect_attempt = 0

        try:
            while self._running:

                # ---- Market hours gate ----
                if not _is_market_hours():
                    log.info(
                        "[worker-%d] Market closed — sleeping %ds",
                        self.worker_id, int(_MARKET_CLOSED_SLEEP_S),
                    )
                    await asyncio.sleep(_MARKET_CLOSED_SLEEP_S)
                    continue

                # ---- Session token ----
                session_token = (
                    self._shared_token
                    if self._shared_token
                    else await get_session_token()
                )
                if not session_token:
                    self._errors += 1
                    self._inc_global_error()
                    backoff = _backoff(min(reconnect_attempt, 7))
                    log.warning(
                        "[worker-%d] No session token — backing off %.1fs (attempt=%d)",
                        self.worker_id, backoff, reconnect_attempt,
                    )
                    await asyncio.sleep(backoff)
                    reconnect_attempt += 1
                    continue

                payload = {
                    "sessionid": session_token,
                    "symbols":   ",".join(self.symbols),
                    "filter":    "timesale",
                    "linebreak": "true",
                }

                self._session_ticks   = 0
                first_line_logged     = False
                stats_task: Optional[asyncio.Task] = None

                try:
                    timeout = httpx.Timeout(
                        connect=_CONNECT_TIMEOUT, read=None, write=10.0, pool=10.0
                    )
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        async with client.stream(
                            "POST", url, headers=stream_headers, data=payload
                        ) as resp:

                            if resp.status_code == 401:
                                log.warning(
                                    "[worker-%d] 401 Unauthorized — session token expired. "
                                    "Signalling manager for token refresh.",
                                    self.worker_id,
                                )
                                self._token_expired = True
                                self._errors += 1
                                self._inc_global_error()
                                return

                            if resp.status_code == 429:
                                log.warning(
                                    "[worker-%d] 429 Rate Limited — backing off 30s",
                                    self.worker_id,
                                )
                                self._errors += 1
                                self._inc_global_error()
                                await asyncio.sleep(30)
                                reconnect_attempt += 1
                                continue

                            if resp.status_code != 200:
                                log.warning(
                                    "[worker-%d] HTTP %d — retrying (attempt=%d)",
                                    self.worker_id, resp.status_code, reconnect_attempt,
                                )
                                self._errors += 1
                                self._inc_global_error()

                            else:
                                self._connect_at       = _time.monotonic()
                                self._ticks_at_last_stats = self._ticks
                                self._last_stats_at    = _time.monotonic()

                                log.info(
                                    "[worker-%d] CONNECT | symbols=%d session=%s... "
                                    "reconnect_attempt=%d",
                                    self.worker_id,
                                    len(self.symbols),
                                    session_token[:8],
                                    reconnect_attempt,
                                )

                                async def _stats_loop(w=self):
                                    while True:
                                        await asyncio.sleep(_STATS_INTERVAL_S)
                                        w._log_stats()

                                stats_task = asyncio.create_task(
                                    _stats_loop(), name=f"stats-{self.worker_id}"
                                )

                                async for line in self._guarded_lines(resp, session_token):
                                    stripped = line.strip()
                                    if not stripped:
                                        continue

                                    try:
                                        raw = json.loads(stripped)
                                    except json.JSONDecodeError:
                                        log.debug(
                                            "[worker-%d] Non-JSON line: %s",
                                            self.worker_id, stripped[:200],
                                        )
                                        continue

                                    if isinstance(raw, dict) and raw.get("error"):
                                        log.warning(
                                            "[worker-%d] API_ERROR | error=%r "
                                            "symbols=%d sessionid=%s...",
                                            self.worker_id,
                                            raw["error"],
                                            len(self.symbols),
                                            session_token[:8],
                                        )
                                        break

                                    if not first_line_logged:
                                        log.info(
                                            "[worker-%d] FIRST_TICK | type=%s payload=%s",
                                            self.worker_id,
                                            raw.get("type", "unknown"),
                                            json.dumps(raw),
                                        )
                                        first_line_logged = True

                                    self._ticks         += 1
                                    self._session_ticks += 1
                                    self._last_tick_at   = _time.time()
                                    self._inc_global_ticks()

                                    try:
                                        self.event_queue.put_nowait(raw)
                                    except asyncio.QueueFull:
                                        log.warning(
                                            "[worker-%d] QUEUE_FULL | depth=%d — dropping tick",
                                            self.worker_id,
                                            self.event_queue.qsize(),
                                        )

                                    # APEX-S2: accumulate into pending batch
                                    self._process_tick(raw)

                                log.info(
                                    "[worker-%d] Stream closed cleanly | session_ticks=%d",
                                    self.worker_id, self._session_ticks,
                                )

                except asyncio.TimeoutError:
                    self._errors += 1
                    self._inc_global_error()
                    log.warning(
                        "[worker-%d] STALL | No tick for %.0fs — reconnecting",
                        self.worker_id, _IDLE_TIMEOUT,
                    )

                except asyncio.CancelledError:
                    log.info("[worker-%d] Cancelled — stopping", self.worker_id)
                    return

                except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as e:
                    self._errors += 1
                    self._inc_global_error()
                    log.warning(
                        "[worker-%d] NETWORK_ERROR | %s: %s",
                        self.worker_id, type(e).__name__, e,
                    )

                except Exception as e:
                    self._errors += 1
                    self._inc_global_error()
                    log.error(
                        "[worker-%d] UNEXPECTED_ERROR | %s: %s",
                        self.worker_id, type(e).__name__, e,
                    )

                finally:
                    if stats_task is not None and not stats_task.done():
                        stats_task.cancel()

                self._reconnects += 1
                self._inc_global_reconnect()
                if self._session_ticks > 0:
                    reconnect_attempt = 0
                else:
                    reconnect_attempt += 1

                backoff = _backoff(min(reconnect_attempt, 7))
                log.info(
                    "[worker-%d] RECONNECT | backoff=%.1fs attempt=%d session_ticks=%d",
                    self.worker_id, backoff, reconnect_attempt, self._session_ticks,
                )
                await asyncio.sleep(backoff)

        finally:
            flush_task.cancel()

    async def _guarded_lines(
        self,
        resp: httpx.Response,
        session_token: str,
    ):
        """
        Async line iterator with idle watchdog.
        STREAM-6: _IDLE_TIMEOUT raised to 120s.
        """
        aiter = resp.aiter_lines().__aiter__()
        stall_logged = False
        while True:
            try:
                line = await asyncio.wait_for(
                    aiter.__anext__(), timeout=_IDLE_TIMEOUT
                )
                stall_logged = False
                yield line
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                now = _time.time()
                if not stall_logged or (now - self._last_stall_log_at) > _STALL_LOG_INTERVAL_S:
                    log.warning(
                        "[worker-%d] STALL | No data for %.0fs | "
                        "symbols=%d session_ticks=%d session=%s...",
                        self.worker_id,
                        _IDLE_TIMEOUT,
                        len(self.symbols),
                        self._session_ticks,
                        session_token[:8],
                    )
                    self._last_stall_log_at = now
                    stall_logged = True
                raise
