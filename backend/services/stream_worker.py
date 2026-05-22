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
    STALL        — logged when no tick received for >60s on an active stream

  Logging: 401 — token expired, _token_expired flag set, clean exit

B-021: startup_delay_s for staggered startup (50ms × worker_id).
B-008: global stats rollup via _global_stats().

STREAM-8 (2026-05-15):
  Remove independent get_session_token() fallback. Previously, if
  _shared_token was None, the worker would call get_session_token()
  directly. Tradier's model is one sessionid per API key — a second
  POST /v1/markets/session call immediately invalidates the existing
  shared token, causing a 401 storm across all other workers.

  Fix: workers NEVER call get_session_token() independently. If
  _shared_token is missing, set _token_expired=True and return cleanly
  so the manager fetches a fresh token and respawns via STREAM-7.

STREAM-9 (2026-05-15):
  Connect-then-sleep probe pattern for market-closed periods.

  Previously workers hit the market-closed gate and slept 300s immediately,
  never attempting a Tradier connection. This meant connectivity failures
  (Render/Railway egress blocks, stale session tokens, TLS issues) were only
  discovered at market open — too late.

  Fix: when market is closed, worker-0 attempts a real probe connection
  to Tradier (5s timeout) before sleeping. Workers 1-N sleep immediately.
  One probe is sufficient — same egress IP, same token, same network path.

    PROBE_OK   — connection succeeded; market is closed; sleeping Xs
    PROBE_FAIL — connection failed; logs repr(e); sleeps 60s then retries
    401 probe  — stale token detected immediately; sets _token_expired so
                 manager refreshes before market open

  The market-closed gate is moved to AFTER the session-token check so token
  validity is validated on every wake cycle regardless of market hours.

STREAM-10 (2026-05-15):
  Fix health check timeout caused by STREAM-9.

  STREAM-9 probed on ALL workers simultaneously at startup. 173 concurrent
  outbound httpx streams flooded the event loop before the HTTP health
  endpoint could respond, causing Render to time out the 5s health check
  and mark the instance as failed (ddkv9).

  Fix: guard probe behind `if self.worker_id == 0`. Workers 1-172 fall
  through to sleep(300s) immediately. Worker-0 probes once on behalf of
  all workers. Startup probe load: 173 connections → 1 connection.

STREAM-11 (2026-05-15):
  Smart market-open sleep — wake exactly at 9:30 ET, not flat 300s.

  Flat sleep(300s) could leave workers asleep through market open. Worst
  case: a worker spawned at 9:27 ET sleeps until 9:32 ET, missing the
  first 5 minutes of flow data.

  Fix: replace flat _MARKET_CLOSED_SLEEP_S with _seconds_until_market_open()
  which calculates exact seconds to the next 9:30 ET open, capped at 300s
  so overnight deploys don't sleep for hours in a single shot.

  Worker-0 probe-fail path (60s retry sleep) is unaffected.

STREAM-12 (2026-05-18):
  Fix false-reconnect loop caused by _guarded_lines re-raising TimeoutError.

  Two problems with the previous behaviour:
    1. _IDLE_TIMEOUT=30s was too short. With 64 workers covering 500 OCC
       contracts each, a single worker can legitimately receive no ticks for
       >30s in a quiet market. Every timeout triggered a reconnect, which
       issued a fresh POST to Tradier — risking 400 Quota Violations and
       growing exponential backoff (up to 10s after 7 stalls).
    2. _guarded_lines re-raised TimeoutError, which propagated out of the
       async-for loop, through the inner try block, and was caught by the
       outer `except asyncio.TimeoutError`. That outer handler called
       `reconnect_attempt += 1`, meaning 7 normal quiet-market periods
       were enough to pin backoff at 10s — turning normal idle time into
       prolonged blackouts.

  Fixes:
    a. _IDLE_TIMEOUT raised 30s → 60s to better match OCC tick cadence.
    b. _guarded_lines no longer re-raises on idle timeout. It logs the STALL
       once per _STALL_LOG_INTERVAL_S (30s), then loops back to the next
       asyncio.wait_for — keeping the worker alive and connected. Only
       StopAsyncIteration (server closed the TCP stream) exits the iterator.
    c. The outer `except asyncio.TimeoutError` block is retained but is now
       only reachable from a genuine connection-phase timeout (the httpx POST
       itself timing out at _CONNECT_TIMEOUT=15s). Its log message is updated
       to CONNECT_TIMEOUT to distinguish it from idle stalls.
"""
import asyncio
import json
import logging
import random
import time as _time
from datetime import datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from config import settings

log = logging.getLogger("stream_worker")

_ET = ZoneInfo("America/New_York")
_MARKET_OPEN  = time(9, 30)
_MARKET_CLOSE = time(16, 0)

# STREAM-12: raised 30s → 60s — OCC tick cadence in quiet markets can exceed
# 30s per worker; the previous value triggered spurious reconnects.
_IDLE_TIMEOUT          = 60.0
_CONNECT_TIMEOUT       = 15.0
_PROBE_TIMEOUT_S       = 5.0     # STREAM-9: short timeout for market-closed probe
_BACKOFF_BASE          = 1.0
_BACKOFF_CAP           = 10.0
_MARKET_CLOSED_SLEEP_S = 300.0   # cap for _seconds_until_market_open()
_STATS_INTERVAL_S      = 30.0    # per-worker STREAM_STATS log frequency
_STALL_LOG_INTERVAL_S  = 30.0    # how often to log a STALL warning mid-stream


def _is_market_hours() -> bool:
    now = datetime.now(_ET)
    if now.weekday() >= 5:
        return False
    return _MARKET_OPEN <= now.time() < _MARKET_CLOSE


def _seconds_until_market_open() -> float:
    """
    STREAM-11: Return seconds until the next 9:30 ET market open, capped at
    _MARKET_CLOSED_SLEEP_S (300s).

    Replaces the flat sleep(300s) in the market-closed gate so workers
    wake up exactly at open rather than potentially sleeping through it.

    Examples:
      9:29:45 ET  →  15s   (wakes at open)
      9:31:00 ET  →  300s  (already past open; next open is tomorrow)
      3:00 AM ET  →  300s  (cap; converges on 9:30 after several cycles)
      Saturday    →  300s  (cap; rolls to Monday 9:30)
    """
    now = datetime.now(_ET)

    # Build today's open candidate
    candidate = now.replace(hour=9, minute=30, second=0, microsecond=0)

    # If we're already at or past today's open (or it's a weekend), roll forward
    if now.time() >= _MARKET_OPEN or now.weekday() >= 5:
        days_ahead = 1
        while (now + timedelta(days=days_ahead)).weekday() >= 5:  # skip Sat/Sun
            days_ahead += 1
        candidate = (now + timedelta(days=days_ahead)).replace(
            hour=9, minute=30, second=0, microsecond=0
        )

    secs = (candidate - now).total_seconds()
    return min(max(secs, 1.0), _MARKET_CLOSED_SLEEP_S)  # floor 1s, cap 300s


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
    # STREAM-9: Probe connection (worker-0 only, market-closed path)
    # ------------------------------------------------------------------

    async def _probe_connection(self, url: str, headers: dict, session_token: str) -> Optional[int]:
        """
        Attempt a real Tradier SSE connection with a short timeout.
        Returns the HTTP status code on connect, or None on network error.

        Only called by worker-0 (STREAM-10). One probe is sufficient to
        confirm Tradier reachability for all workers — same egress IP,
        same token, same network path.
        """
        payload = {
            "sessionid": session_token,
            "symbols":   ",".join(self.symbols[:1]),  # 1 symbol is enough to test auth+routing
            "filter":    "timesale",
            "linebreak": True,
        }
        try:
            timeout = httpx.Timeout(
                connect=_PROBE_TIMEOUT_S, read=_PROBE_TIMEOUT_S, write=5.0, pool=5.0
            )
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, headers=headers, data=payload) as resp:
                    status = resp.status_code
                    if status == 200:
                        # Attempt to read one line to confirm the stream opens
                        try:
                            async for _ in resp.aiter_lines():
                                break  # got at least one line (may be empty keepalive)
                        except Exception:
                            pass  # EOF or timeout on read is fine — connect succeeded
                    return status
        except (httpx.ConnectTimeout, httpx.ReadTimeout, asyncio.TimeoutError):
            log.warning(
                "[worker-%d] PROBE_FAIL | ConnectTimeout after %.0fs — Tradier unreachable?",
                self.worker_id, _PROBE_TIMEOUT_S,
            )
            return None
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError) as e:
            log.warning(
                "[worker-%d] PROBE_FAIL | %s: %r",
                self.worker_id, type(e).__name__, e,
            )
            return None
        except Exception as e:
            log.warning(
                "[worker-%d] PROBE_FAIL | Unexpected: %r",
                self.worker_id, e,
            )
            return None

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

            # ---- Session token (STREAM-8) ----
            # Check token BEFORE the market-hours gate so a stale token is
            # detected even when market is closed (STREAM-9).
            session_token = self._shared_token
            if not session_token:
                log.error(
                    "[worker-%d] No shared_session_token — refusing to fetch independently "
                    "(would invalidate shared token). Signalling manager for respawn.",
                    self.worker_id,
                )
                self._token_expired = True
                self._errors += 1
                self._inc_global_error()
                return  # manager detects _token_expired and respawns all workers

            # ---- Market-closed gate (STREAM-9 / STREAM-10 / STREAM-11) ----
            if not _is_market_hours():
                # STREAM-10: Only worker-0 probes. Workers 1-N sleep immediately.
                if self.worker_id == 0:
                    status = await self._probe_connection(url, stream_headers, session_token)

                    if status == 200:
                        sleep_secs = _seconds_until_market_open()
                        log.info(
                            "[worker-0] PROBE_OK | Connected to Tradier SSE — "
                            "market closed, sleeping %.0fs (until ~9:30 ET)",
                            sleep_secs,
                        )
                    elif status == 401:
                        log.warning(
                            "[worker-0] PROBE_FAIL | 401 Unauthorized — session token is stale. "
                            "Signalling manager for token refresh.",
                        )
                        self._token_expired = True
                        self._errors += 1
                        self._inc_global_error()
                        return  # manager will refresh token and respawn
                    elif status is not None:
                        # Other HTTP error (429, 400, 503 etc.) — short retry, not smart sleep
                        log.warning(
                            "[worker-0] PROBE_FAIL | HTTP %d — sleeping 60s before retry",
                            status,
                        )
                        self._errors += 1
                        self._inc_global_error()
                        await asyncio.sleep(60)
                        continue
                    else:
                        # None = network/connect error, already logged in _probe_connection
                        self._errors += 1
                        self._inc_global_error()
                        await asyncio.sleep(60)
                        continue

                # STREAM-11: All workers (including worker-0 on PROBE_OK path) sleep
                # until just before market open rather than a flat 300s.
                sleep_secs = _seconds_until_market_open()
                log.debug(
                    "[worker-%d] Market closed — sleeping %.0fs (until ~9:30 ET)",
                    self.worker_id, sleep_secs,
                )
                await asyncio.sleep(sleep_secs)
                continue

            # ---- Live market path ----
            payload = {
                "sessionid": session_token,
                "symbols":   ",".join(self.symbols),
                "filter":    "timesale",
                "linebreak": True,
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
                # STREAM-12: This except now only fires on a genuine connection-phase
                # timeout (httpx POST at _CONNECT_TIMEOUT=15s). Idle-stream timeouts
                # are absorbed inside _guarded_lines and no longer propagate here.
                self._errors += 1
                self._inc_global_error()
                log.warning(
                    "[worker-%d] CONNECT_TIMEOUT | Could not connect within %.0fs — reconnecting",
                    self.worker_id, _CONNECT_TIMEOUT,
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

        STREAM-12: No longer re-raises TimeoutError on idle timeout.
        Previously, re-raising propagated out of the `async for` loop and
        was caught by the outer `except asyncio.TimeoutError`, which triggered
        a full reconnect and incremented reconnect_attempt. In quiet markets
        a single worker can go >30s (now 60s) without a tick for its 500 OCC
        contracts — this was causing spurious reconnect storms.

        New behaviour: on idle timeout, log a STALL warning (rate-limited to
        every _STALL_LOG_INTERVAL_S=30s) and loop back to wait_for — the
        worker stays connected on the same POST stream. Only StopAsyncIteration
        (server closed the TCP stream) exits the iterator cleanly.
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
                # STREAM-12: do NOT re-raise — stay on the open POST stream
                # and keep waiting for the next tick.
