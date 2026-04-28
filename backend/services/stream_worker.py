"""
services/stream_worker.py — Layer 2: Single stream connection lifecycle.

FIX STREAM-3 (2026-04-28): Remove asyncio.Lock.
  Tradier "1 concurrent session" means 1 sessionid at a time — NOT 1 open
  connection at a time. All workers sharing the same sessionid can each hold
  a simultaneous open POST stream. Removing the lock gives us true parallel
  coverage of all 31,920 symbols from the moment workers start.

  Concurrency model:
    - 64 workers, each with 500 symbols, all connected simultaneously
    - All use the same shared session token (same Tradier session)
    - First tick of each worker is logged in full (untruncated) for shape inspection
    - Per-worker STREAM_STATS line every 30s: ticks, ticks_last_30s, errors, reconnects

Fix (S-03): _MARKET_CLOSED_SLEEP_S=300 (5min).
Fix (S-04): stats includes last_tick_at, session_ticks.
B-021: startup_delay_s kept for API compat (always 0.0 in production).
B-008: global stats rollup via _global_stats().
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

_IDLE_TIMEOUT          = 15.0
_CONNECT_TIMEOUT       = 15.0
_BACKOFF_BASE          = 1.0
_BACKOFF_CAP           = 10.0
_MARKET_CLOSED_SLEEP_S = 300.0
_STATS_INTERVAL_S      = 30.0   # how often each worker logs STREAM_STATS


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
        session_lock: Optional[asyncio.Lock] = None,  # kept for API compat, ignored
    ):
        self.worker_id            = worker_id
        self.symbols              = symbols
        self.event_queue          = event_queue
        self.startup_delay_s      = startup_delay_s
        self._shared_token        = shared_session_token
        # session_lock intentionally ignored — STREAM-3 removes the lock
        self._token_expired       = False
        self._running             = True
        self._ticks               = 0
        self._errors              = 0
        self._reconnects          = 0
        self._last_tick_at:  Optional[float] = None
        self._session_ticks: int = 0
        self._connect_at:    Optional[float] = None
        # rolling window for ticks/s
        self._ticks_at_last_stats: int = 0
        self._last_stats_at: float = _time.monotonic()

    def update_symbols(self, new_symbols: list[str]):
        self.symbols = new_symbols

    def stop(self):
        self._running = False

    @property
    def stats(self) -> dict:
        return {
            "worker_id":       self.worker_id,
            "symbols":         len(self.symbols),
            "ticks":           self._ticks,
            "errors":          self._errors,
            "reconnects":      self._reconnects,
            "startup_delay_s": self.startup_delay_s,
            "last_tick_at":    self._last_tick_at,
            "session_ticks":   self._session_ticks,
            "token_expired":   self._token_expired,
        }

    def _inc_global_error(self) -> None:
        try:
            _global_stats()["errors"] += 1
        except Exception:
            pass

    def _inc_global_reconnect(self) -> None:
        try:
            s = _global_stats()
            s["reconnects"] += 1
            s["last_reconnect_at"] = _time.time()
        except Exception:
            pass

    def _log_stats(self) -> None:
        """Emit a STREAM_STATS line for this worker — called every _STATS_INTERVAL_S."""
        now = _time.monotonic()
        elapsed = now - self._last_stats_at
        ticks_delta = self._ticks - self._ticks_at_last_stats
        rate = ticks_delta / elapsed if elapsed > 0 else 0.0
        uptime = round(now - self._connect_at, 1) if self._connect_at else None
        log.info(
            "[worker-%d] STREAM_STATS | symbols=%d ticks=%d ticks_30s=%d "
            "rate=%.1f/s errors=%d reconnects=%d uptime=%ss last_tick_ago=%s",
            self.worker_id,
            len(self.symbols),
            self._ticks,
            ticks_delta,
            rate,
            self._errors,
            self._reconnects,
            uptime,
            round(_time.time() - self._last_tick_at, 1) if self._last_tick_at else "never",
        )
        self._ticks_at_last_stats = self._ticks
        self._last_stats_at = now

    async def run(self):
        """Main loop — connect, stream, reconnect on failure."""
        if self.startup_delay_s > 0:
            await asyncio.sleep(self.startup_delay_s)

        url = f"{settings.TRADIER_STREAM_URL}/v1/markets/events"
        stream_headers = {
            "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
            "Accept":        "application/json",
        }
        reconnect_attempt = 0

        while self._running:
            if not _is_market_hours():
                log.info(
                    "[worker-%d] Market closed — sleeping %ds",
                    self.worker_id, int(_MARKET_CLOSED_SLEEP_S),
                )
                await asyncio.sleep(_MARKET_CLOSED_SLEEP_S)
                continue

            session_token = self._shared_token if self._shared_token else await get_session_token()

            if not session_token:
                self._errors += 1
                self._inc_global_error()
                backoff = _backoff(min(reconnect_attempt, 7))
                log.warning(
                    "[worker-%d] No session token — backing off %.1fs",
                    self.worker_id, backoff,
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

            self._session_ticks = 0
            first_line_logged = False
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
                                "[worker-%d] 401 Unauthorized — token expired, signalling manager",
                                self.worker_id,
                            )
                            self._token_expired = True
                            self._errors += 1
                            self._inc_global_error()
                            return

                        if resp.status_code != 200:
                            log.warning(
                                "[worker-%d] HTTP %d — backing off",
                                self.worker_id, resp.status_code,
                            )
                            self._errors += 1
                            self._inc_global_error()
                        else:
                            self._connect_at = _time.monotonic()
                            self._ticks_at_last_stats = self._ticks
                            self._last_stats_at = _time.monotonic()

                            log.info(
                                "[worker-%d] Connected — streaming %d OCC symbols "
                                "(session=%s...)",
                                self.worker_id,
                                len(self.symbols),
                                session_token[:8],
                            )

                            # Periodic stats logger for this worker
                            async def _stats_loop(w=self):
                                while True:
                                    await asyncio.sleep(_STATS_INTERVAL_S)
                                    w._log_stats()

                            stats_task = asyncio.create_task(
                                _stats_loop(), name=f"stats-{self.worker_id}"
                            )

                            async for line in self._guarded_lines(resp):
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
                                        "[worker-%d] API error on stream: %r — "
                                        "symbols=%d sessionid=%s...",
                                        self.worker_id,
                                        raw["error"],
                                        len(self.symbols),
                                        session_token[:8],
                                    )
                                    break

                                if not first_line_logged:
                                    # Log full first tick untruncated so we can inspect shape
                                    log.info(
                                        "[worker-%d] FIRST_TICK type=%s raw=%s",
                                        self.worker_id,
                                        raw.get("type", "unknown"),
                                        json.dumps(raw),
                                    )
                                    first_line_logged = True

                                self._ticks += 1
                                self._session_ticks += 1
                                self._last_tick_at = _time.time()

                                # Increment global tick counter
                                try:
                                    gs = _global_stats()
                                    gs["ticks"] = gs.get("ticks", 0) + 1
                                except Exception:
                                    pass

                                try:
                                    self.event_queue.put_nowait(raw)
                                except asyncio.QueueFull:
                                    qsize = self.event_queue.qsize()
                                    log.warning(
                                        "[worker-%d] Queue full (size=%d) — dropping tick",
                                        self.worker_id, qsize,
                                    )

                            log.info(
                                "[worker-%d] Stream closed cleanly after %d ticks",
                                self.worker_id, self._session_ticks,
                            )

            except asyncio.TimeoutError:
                self._errors += 1
                self._inc_global_error()
                log.warning(
                    "[worker-%d] Idle timeout (%.0fs) — reconnecting",
                    self.worker_id, _IDLE_TIMEOUT,
                )

            except asyncio.CancelledError:
                log.info("[worker-%d] Cancelled — stopping", self.worker_id)
                return

            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as e:
                self._errors += 1
                self._inc_global_error()
                log.warning(
                    "[worker-%d] Network error (%s): %s",
                    self.worker_id, type(e).__name__, e,
                )

            except Exception as e:
                self._errors += 1
                self._inc_global_error()
                log.error(
                    "[worker-%d] Unexpected error (%s): %s",
                    self.worker_id, type(e).__name__, e,
                )

            finally:
                if stats_task is not None and not stats_task.done():
                    stats_task.cancel()

            # Reconnect backoff
            self._reconnects += 1
            self._inc_global_reconnect()
            if self._session_ticks > 0:
                reconnect_attempt = 0
            else:
                reconnect_attempt += 1

            backoff = _backoff(min(reconnect_attempt, 7))
            log.info(
                "[worker-%d] Reconnecting in %.1fs (attempt=%d session_ticks=%d)",
                self.worker_id, backoff, reconnect_attempt, self._session_ticks,
            )
            await asyncio.sleep(backoff)

    async def _guarded_lines(self, resp: httpx.Response):
        """Line iterator with idle watchdog."""
        aiter = resp.aiter_lines().__aiter__()
        while True:
            try:
                line = await asyncio.wait_for(aiter.__anext__(), timeout=_IDLE_TIMEOUT)
                yield line
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                raise
