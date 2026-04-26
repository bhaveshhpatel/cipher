"""
services/stream_worker.py — Layer 2: Single stream connection lifecycle.

Each StreamWorker manages one Tradier streaming connection for up to
500 OCC option contract symbols. The StreamManager creates N workers
to cover the full OCC registry (~16,000 symbols = ~32 workers).

Each worker:
  1. Sleeps startup_delay_s before opening its first connection (B-021)
  2. Fetches its own fresh session token (tokens are single-use)
  3. Opens a POST stream to stream.tradier.com/v1/markets/events
     with filter=timesale and its 500 OCC symbols
  4. Reads lines → pushes raw dicts to the shared asyncio.Queue
  5. Reconnects with exponential backoff on any disconnect
  6. Respects a 30s idle watchdog (Tradier sends bare newlines as keepalives)
  7. Shuts down cleanly on cancellation

B-021 — Staggered startup:
  StreamManager passes startup_delay_s = idx * 0.200 to each worker.
  Worker-0 has delay=0 (connects immediately), worker-1 delays 200ms,
  worker-31 delays 6.2s. After the initial delay the worker runs its
  normal market-hours guard and reconnect loop with zero extra latency.
  The startup_delay_s delay only fires once at the start of run();
  reconnects do NOT re-apply the startup delay.

B-008 — Global stats rollup:
  On every reconnect and on every error, the worker calls
  _report_reconnect() / _report_error() which write directly into the
  tradier_stream._stats dict so GET /health/stream reflects live data.
  last_reconnect_at is set to time.time() on every self._reconnects
  increment so the health endpoint can report when the last disruption
  occurred.

The shared queue is consumed by the parser/enricher layer.
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

_IDLE_TIMEOUT    = 30.0
_CONNECT_TIMEOUT = 15.0
_BACKOFF_BASE    = 5.0
_BACKOFF_CAP     = 60.0


def _is_market_hours() -> bool:
    now = datetime.now(_ET)
    if now.weekday() >= 5:
        return False
    return _MARKET_OPEN <= now.time() < _MARKET_CLOSE


def _backoff(attempt: int) -> float:
    delay = min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** attempt))
    return random.uniform(0, delay)


# ---------------------------------------------------------------------------
# B-008: lazy import helper to avoid circular import at module level.
# tradier_stream imports stream_worker, so we cannot import tradier_stream
# at the top of this file. Instead we import it on first use inside the
# helper functions below.
# ---------------------------------------------------------------------------
def _global_stats() -> dict:
    """Return the live _stats dict from tradier_stream (lazy import)."""
    from services import tradier_stream  # noqa: PLC0415
    return tradier_stream._stats


class StreamWorker:
    """
    Manages a single Tradier streaming connection for a chunk of OCC symbols.
    Pushes raw event dicts to the shared queue for processing.
    """

    def __init__(
        self,
        worker_id: int,
        symbols: list[str],
        event_queue: asyncio.Queue,
        startup_delay_s: float = 0.0,  # B-021: stagger delay before first connect
    ):
        self.worker_id       = worker_id
        self.symbols         = symbols
        self.event_queue     = event_queue
        self.startup_delay_s = startup_delay_s  # B-021
        self._running        = True
        self._ticks          = 0
        self._errors         = 0
        self._reconnects     = 0

    def update_symbols(self, new_symbols: list[str]):
        """Update symbol list — takes effect on next reconnect."""
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
            "startup_delay_s": self.startup_delay_s,  # B-021: visible in /health
        }

    # ------------------------------------------------------------------
    # B-008: global stat helpers
    # ------------------------------------------------------------------
    def _inc_global_error(self) -> None:
        """Increment global errors counter in tradier_stream._stats."""
        try:
            _global_stats()["errors"] += 1
        except Exception:
            pass  # never crash the worker over a stats write

    def _inc_global_reconnect(self) -> None:
        """Increment global reconnects counter and set last_reconnect_at."""
        try:
            s = _global_stats()
            s["reconnects"] += 1
            s["last_reconnect_at"] = _time.time()
        except Exception:
            pass

    async def run(self):
        """Main loop — connect, stream, reconnect on failure."""

        # B-021: One-time startup stagger. Worker-0 is delay=0 (instant).
        # Subsequent workers sleep N*200ms before their first connection attempt.
        # This delay does NOT re-apply on reconnects — reconnects use _backoff().
        if self.startup_delay_s > 0:
            log.info(
                f"[worker-{self.worker_id}] B-021 startup stagger: "
                f"sleeping {self.startup_delay_s:.1f}s before first connect"
            )
            await asyncio.sleep(self.startup_delay_s)

        url = f"{settings.TRADIER_STREAM_URL}/v1/markets/events"
        stream_headers = {
            "Authorization": f"Bearer {settings.TRADIER_API_KEY}",
            "Accept":        "application/json",
        }
        reconnect_attempt = 0

        while self._running:
            # Market hours guard
            if not _is_market_hours():
                await asyncio.sleep(60.0)
                continue

            # Fetch fresh session token
            session_token = await get_session_token()
            if not session_token:
                self._errors += 1
                self._inc_global_error()  # B-008
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

            session_ticks = 0
            first_line_logged = False

            try:
                timeout = httpx.Timeout(connect=_CONNECT_TIMEOUT, read=None, write=10.0, pool=10.0)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream("POST", url, headers=stream_headers, data=payload) as resp:
                        if resp.status_code == 401:
                            log.warning(f"[worker-{self.worker_id}] 401 — expired token, retrying")
                            self._errors += 1
                            self._inc_global_error()  # B-008
                            await asyncio.sleep(1.0)
                            reconnect_attempt += 1
                            continue

                        log.info(
                            f"[worker-{self.worker_id}] Connected — streaming {len(self.symbols)} OCC symbols"
                        )

                        async for line in self._guarded_lines(resp):
                            stripped = line.strip()
                            if not stripped:
                                continue
                            try:
                                raw = json.loads(stripped)
                            except json.JSONDecodeError:
                                continue

                            if not first_line_logged:
                                log.info(f"[worker-{self.worker_id}] first tick: {stripped[:300]}")
                                first_line_logged = True

                            self._ticks += 1
                            session_ticks += 1
                            try:
                                self.event_queue.put_nowait(raw)
                            except asyncio.QueueFull:
                                log.warning(f"[worker-{self.worker_id}] Queue full — dropping tick")

                        log.info(f"[worker-{self.worker_id}] Stream closed cleanly")

            except asyncio.TimeoutError:
                self._errors += 1
                self._inc_global_error()  # B-008
                log.warning(f"[worker-{self.worker_id}] Idle {_IDLE_TIMEOUT}s — reconnecting")

            except asyncio.CancelledError:
                log.info(f"[worker-{self.worker_id}] Cancelled — stopping")
                return

            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError) as e:
                self._errors += 1
                self._inc_global_error()  # B-008
                log.warning(f"[worker-{self.worker_id}] Network error: {e}")

            except Exception as e:
                self._errors += 1
                self._inc_global_error()  # B-008
                log.error(f"[worker-{self.worker_id}] Unexpected error: {e}")

            self._reconnects += 1
            self._inc_global_reconnect()  # B-008: rolls up into _stats + sets last_reconnect_at
            if session_ticks > 0:
                reconnect_attempt = 0
            else:
                reconnect_attempt += 1

            backoff = _backoff(min(reconnect_attempt, 7))
            log.info(f"[worker-{self.worker_id}] Reconnecting in {backoff:.1f}s")
            await asyncio.sleep(backoff)

    async def _guarded_lines(self, resp: httpx.Response):
        """Line iterator with 30s idle watchdog."""
        aiter = resp.aiter_lines().__aiter__()
        while True:
            try:
                line = await asyncio.wait_for(aiter.__anext__(), timeout=_IDLE_TIMEOUT)
                yield line
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                raise
