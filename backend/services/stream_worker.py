"""
services/stream_worker.py — Layer 2: Single stream connection lifecycle.

FIX STREAM-2 (2026-04-28):
  Tradier hard limit: 500 symbols per stream POST.
  Workers now receive a shared_session_token from StreamManager instead
  of fetching their own. All workers share the same Tradier session,
  and a shared asyncio.Lock (session_lock) ensures only 1 worker holds
  an open stream at a time (Tradier 1-concurrent-session rule).

  If the token expires mid-stream (401 response), the worker sets
  self._token_expired = True and exits cleanly. StreamManager detects
  this flag in its run() loop and calls _respawn_workers(force_token_refresh=True).

Fix (S-03): _MARKET_CLOSED_SLEEP_S=300 (5min) replaces hard-coded 60s.
Fix (S-04): stats includes last_tick_at, session_ticks.
B-021: startup_delay_s kept for API compatibility (always 0.0 in production).
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
        session_lock: Optional[asyncio.Lock] = None,
    ):
        self.worker_id            = worker_id
        self.symbols              = symbols
        self.event_queue          = event_queue
        self.startup_delay_s      = startup_delay_s
        # STREAM-2: shared session token + lock
        self._shared_token        = shared_session_token
        self._session_lock        = session_lock
        self._token_expired       = False   # signals manager to refresh
        self._running             = True
        self._ticks               = 0
        self._errors              = 0
        self._reconnects          = 0
        self._last_tick_at:  Optional[float] = None
        self._session_ticks: int = 0

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
                await asyncio.sleep(_MARKET_CLOSED_SLEEP_S)
                continue

            # STREAM-2: use shared token if provided, else fetch own
            if self._shared_token:
                session_token = self._shared_token
            else:
                session_token = await get_session_token()

            if not session_token:
                self._errors += 1
                self._inc_global_error()
                backoff = _backoff(min(reconnect_attempt, 7))
                log.warning(f"[worker-{self.worker_id}] No session token — backing off {backoff:.1f}s")
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

            # STREAM-2: acquire lock so only 1 worker streams at a time
            lock = self._session_lock
            async with (lock if lock else asyncio.Lock()):
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
                                    f"[worker-{self.worker_id}] 401 — token expired, "
                                    f"signalling manager"
                                )
                                self._token_expired = True
                                self._errors += 1
                                self._inc_global_error()
                                return  # exit cleanly, manager will respawn

                            if resp.status_code != 200:
                                log.warning(
                                    f"[worker-{self.worker_id}] HTTP {resp.status_code} — retrying"
                                )
                                self._errors += 1
                                self._inc_global_error()
                                # fall through to reconnect logic below
                            else:
                                log.info(
                                    f"[worker-{self.worker_id}] Connected — "
                                    f"streaming {len(self.symbols)} OCC symbols"
                                )
                                async for line in self._guarded_lines(resp):
                                    stripped = line.strip()
                                    if not stripped:
                                        continue
                                    try:
                                        raw = json.loads(stripped)
                                    except json.JSONDecodeError:
                                        continue

                                    # Drop API error responses immediately
                                    if isinstance(raw, dict) and raw.get("error"):
                                        log.warning(
                                            f"[worker-{self.worker_id}] "
                                            f"stream error: {raw['error']}"
                                        )
                                        break

                                    if not first_line_logged:
                                        log.info(
                                            f"[worker-{self.worker_id}] "
                                            f"first tick: {stripped[:300]}"
                                        )
                                        first_line_logged = True

                                    self._ticks += 1
                                    self._session_ticks += 1
                                    self._last_tick_at = _time.time()
                                    try:
                                        self.event_queue.put_nowait(raw)
                                    except asyncio.QueueFull:
                                        log.warning(
                                            f"[worker-{self.worker_id}] Queue full — dropping tick"
                                        )

                                log.info(f"[worker-{self.worker_id}] Stream closed cleanly")

                except asyncio.TimeoutError:
                    self._errors += 1
                    self._inc_global_error()
                    log.warning(
                        f"[worker-{self.worker_id}] Idle {_IDLE_TIMEOUT}s — reconnecting"
                    )

                except asyncio.CancelledError:
                    log.info(f"[worker-{self.worker_id}] Cancelled — stopping")
                    return

                except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as e:
                    self._errors += 1
                    self._inc_global_error()
                    log.warning(f"[worker-{self.worker_id}] Network error: {e}")

                except Exception as e:
                    self._errors += 1
                    self._inc_global_error()
                    log.error(f"[worker-{self.worker_id}] Unexpected error: {e}")

            # Outside the lock — reconnect backoff
            self._reconnects += 1
            self._inc_global_reconnect()
            if self._session_ticks > 0:
                reconnect_attempt = 0
            else:
                reconnect_attempt += 1

            backoff = _backoff(min(reconnect_attempt, 7))
            log.info(f"[worker-{self.worker_id}] Reconnecting in {backoff:.1f}s")
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
