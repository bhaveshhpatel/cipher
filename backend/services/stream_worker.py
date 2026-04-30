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

_IDLE_TIMEOUT          = 120.0   # STREAM-6: raised from 30s; ~110s expected tick interval per worker
_CONNECT_TIMEOUT       = 15.0
_BACKOFF_BASE          = 1.0
_BACKOFF_CAP           = 10.0
_MARKET_CLOSED_SLEEP_S = 300.0
_STATS_INTERVAL_S      = 30.0    # per-worker STREAM_STATS log frequency
_STALL_LOG_INTERVAL_S  = 60.0    # how often to log a STALL warning mid-stream (raised from 30s)


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
        # session_lock intentionally ignored (STREAM-3)
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

        url = f"{settings.TRADIER_STREAM_URL}/v1/markets/events"
        stream_headers = {
            "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
            "Accept":        "application/json",
        }
        reconnect_attempt = 0

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

                        # ---- HTTP error handling ----
                        if resp.status_code == 401:
                            log.warning(
                                "[worker-%d] 401 Unauthorized — session token expired. "
                                "Signalling manager for token refresh.",
                                self.worker_id,
                            )
                            self._token_expired = True
                            self._errors += 1
                            self._inc_global_error()
                            return   # clean exit; manager respawns

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
                            # fall through to reconnect

                        else:
                            # ---- Connected successfully ----
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

                            # Periodic per-worker stats
                            async def _stats_loop(w=self):
                                while True:
                                    await asyncio.sleep(_STATS_INTERVAL_S)
                                    w._log_stats()

                            stats_task = asyncio.create_task(
                                _stats_loop(), name=f"stats-{self.worker_id}"
                            )

                            # ---- Line reader ----
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

                                # ---- API-level error in stream body ----
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

                                # ---- First tick ----
                                if not first_line_logged:
                                    log.info(
                                        "[worker-%d] FIRST_TICK | type=%s payload=%s",
                                        self.worker_id,
                                        raw.get("type", "unknown"),
                                        json.dumps(raw),   # full, untruncated
                                    )
                                    first_line_logged = True

                                # ---- Count + enqueue ----
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

            # ---- Reconnect backoff ----
            self._reconnects += 1
            self._inc_global_reconnect()
            if self._session_ticks > 0:
                reconnect_attempt = 0   # successful connection resets backoff
            else:
                reconnect_attempt += 1

            backoff = _backoff(min(reconnect_attempt, 7))
            log.info(
                "[worker-%d] RECONNECT | backoff=%.1fs attempt=%d session_ticks=%d",
                self.worker_id, backoff, reconnect_attempt, self._session_ticks,
            )
            await asyncio.sleep(backoff)

    async def _guarded_lines(
        self,
        resp: httpx.Response,
        session_token: str,
    ):
        """
        Async line iterator with idle watchdog.
        Logs a STALL warning if no line arrives within _IDLE_TIMEOUT seconds.
        STREAM-6: _IDLE_TIMEOUT raised to 120s — quiet workers on low-volume
        symbol sets were reconnecting every 30s unnecessarily.
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
                raise  # propagate to reconnect loop
