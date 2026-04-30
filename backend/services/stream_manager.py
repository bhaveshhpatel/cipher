"""
services/stream_manager.py -- Parallel Stream Manager

Summary of active fixes
-----------------------
STREAM-1 (2026-04-28):
  _respawn_workers() every _worker_refresh_s (default 300s) to stay in sync
  with registry.refresh_loop() rebuilds.

STREAM-2 (2026-04-28):
  Tradier rejects stream POSTs with >500 symbols. Fix: _CHUNK_SIZE=500,
  ONE shared session token across all workers.

STREAM-3 (2026-04-28):
  Remove asyncio.Lock. All workers connect in parallel simultaneously.
  Tradier "1 concurrent session" = 1 sessionid at a time, NOT 1 open
  connection. Workers sharing the same sessionid each hold their own open
  POST stream concurrently — all 31,920 symbols covered from T+0.

STREAM-4 (2026-04-30):
  Add asyncio.wait_for(timeout=10s) around get_session_token() to prevent
  silent infinite hang when Tradier session endpoint is unresponsive.

STREAM-5 (2026-04-30):
  Increase retry delay from 2s → 15s so rapid container restarts self-heal
  after Tradier's quota releases. Explicit 400 Quota Violation handling with
  20s backoff (Tradier session TTL is ~10-15s after connection drop).

Architecture
------------
  - 1 session token fetched at spawn time, shared to all workers
  - 64 workers × 500 symbols = 31,920 OCC symbols, all streaming in parallel
  - 50ms staggered startup to avoid thundering-herd on Tradier endpoint
  - asyncio.Queue(maxsize=50_000) feeds a single _consume_queue() task
  - Manager logs STREAM_HEALTH every 30s: aggregate ticks, active workers,
    stalled workers, queue depth, global tick rate
  - On 401 from any worker: _token_expired flag detected within 60s,
    full token refresh + worker respawn
"""
import asyncio
import logging
import time as _time
from typing import Callable, Awaitable, Optional

from utils.tradier_client import get_session_token

log = logging.getLogger("stream_manager")

_CHUNK_SIZE              = 500        # Tradier hard limit: 500 symbols per POST
_QUEUE_SIZE              = 50_000     # handle burst from 64 parallel workers
_WORKER_SPAWN_DELAY_S    = 0.05       # 50ms stagger between worker starts
_HEALTH_LOG_INTERVAL_S   = 30.0       # manager-level aggregate log interval
_DEFAULT_WORKER_REFRESH_S: float = 300.0
_STALL_THRESHOLD_S       = 60.0       # worker is "stalled" if no tick in this many seconds
_SESSION_TOKEN_TIMEOUT_S = 10.0       # hard timeout for each get_session_token() attempt (STREAM-4)
_SESSION_RETRY_DELAY_S   = 15.0       # delay between retry attempts (STREAM-5)
_SESSION_QUOTA_BACKOFF_S = 20.0       # extra backoff on 400 Quota Violation (STREAM-5)


class StreamManager:
    def __init__(
        self,
        registry=None,
        process_fn: Optional[Callable[[dict], Awaitable[None]]] = None,
        worker_refresh_s: float = _DEFAULT_WORKER_REFRESH_S,
    ):
        self._registry          = registry
        self._process_fn        = process_fn
        self._worker_refresh_s  = worker_refresh_s
        self._workers:     list = []
        self._tasks:       list = []
        self._consumer:    Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._running      = False
        self._queue:       Optional[asyncio.Queue] = None
        self._session_token: Optional[str] = None
        self._spawn_at:    Optional[float] = None
        self._total_ticks_at_last_health: int = 0
        self._last_health_at: float = _time.monotonic()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @running.setter
    def running(self, value: bool) -> None:
        self._running = value

    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._registry is None:
            log.warning("[stream_manager] start() called with no registry -- no-op")
            return
        self._running = True
        self._queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        await self._spawn_workers()
        self._consumer = asyncio.create_task(
            self._consume_queue(), name="stream-consumer"
        )

    async def stop(self) -> None:
        self._running = False
        all_tasks = [
            *[t for t in self._tasks if t is not None],
            self._consumer,
            self._health_task,
        ]
        for t in all_tasks:
            if t is not None:
                t.cancel()
        await asyncio.gather(*[t for t in all_tasks if t is not None], return_exceptions=True)
        self._tasks.clear()
        self._workers.clear()
        log.info("[stream_manager] All workers stopped — Tradier connections closed")

    async def run(self):
        """
        Long-running entry point called by main.
        Runs health logging + periodic worker refresh in parallel.
        """
        self._running = True
        self._queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        log.info("[stream_manager] Starting — chunk_size=%d queue_size=%d",
                 _CHUNK_SIZE, _QUEUE_SIZE)
        await self._spawn_workers()
        self._consumer    = asyncio.create_task(self._consume_queue(),   name="stream-consumer")
        self._health_task = asyncio.create_task(self._health_loop(),     name="stream-health")
        elapsed = 0.0
        try:
            while self._running:
                await asyncio.sleep(60)
                elapsed += 60.0
                if self._any_token_expired():
                    log.warning("[stream_manager] Token expired detected — refreshing session + respawning")
                    await self._respawn_workers(force_token_refresh=True)
                    elapsed = 0.0
                elif elapsed >= self._worker_refresh_s:
                    elapsed = 0.0
                    await self._respawn_workers()
        except asyncio.CancelledError:
            await self.stop()
            raise

    def status(self) -> dict:
        now = _time.time()
        stalled = sum(
            1 for w in self._workers
            if w._last_tick_at and (now - w._last_tick_at) > _STALL_THRESHOLD_S
        )
        return {
            "running":         self._running,
            "workers":         len(self._workers),
            "active_symbols":  self._registry.size() if self._registry else 0,
            "stalled_workers": stalled,
            "queue_depth":     self._queue.qsize() if self._queue else 0,
        }

    @property
    def stats(self) -> dict:
        return self.status()

    # ------------------------------------------------------------------
    # Health log loop
    # ------------------------------------------------------------------

    async def _health_loop(self):
        """Log aggregate STREAM_HEALTH every _HEALTH_LOG_INTERVAL_S seconds."""
        await asyncio.sleep(_HEALTH_LOG_INTERVAL_S)  # first report after 30s
        while True:
            try:
                await asyncio.sleep(_HEALTH_LOG_INTERVAL_S)
                self._log_health()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error("[stream_manager] _health_loop error: %s", e)

    def _log_health(self):
        now_mono = _time.monotonic()
        now_wall = _time.time()
        elapsed  = now_mono - self._last_health_at

        total_ticks  = sum(w._ticks       for w in self._workers)
        total_errors = sum(w._errors      for w in self._workers)
        total_recon  = sum(w._reconnects  for w in self._workers)

        ticks_delta = total_ticks - self._total_ticks_at_last_health
        rate        = ticks_delta / elapsed if elapsed > 0 else 0.0

        active   = sum(1 for w in self._workers if not getattr(w, "_token_expired", False))
        stalled  = sum(
            1 for w in self._workers
            if w._last_tick_at and (now_wall - w._last_tick_at) > _STALL_THRESHOLD_S
        )
        never_ticked = sum(1 for w in self._workers if w._last_tick_at is None)
        queue_depth  = self._queue.qsize() if self._queue else 0
        uptime_s     = round(now_wall - self._spawn_at, 0) if self._spawn_at else None

        log.info(
            "[stream_manager] STREAM_HEALTH | workers=%d active=%d stalled=%d "
            "never_ticked=%d total_ticks=%d ticks_30s=%d rate=%.1f/s "
            "errors=%d reconnects=%d queue_depth=%d uptime=%ss",
            len(self._workers), active, stalled, never_ticked,
            total_ticks, ticks_delta, rate,
            total_errors, total_recon,
            queue_depth, uptime_s,
        )

        self._total_ticks_at_last_health = total_ticks
        self._last_health_at = now_mono

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _any_token_expired(self) -> bool:
        return any(getattr(w, "_token_expired", False) for w in self._workers)

    async def _fetch_session_token(self) -> Optional[str]:
        """
        Fetch a fresh session token with up to 3 retries.
        STREAM-4: 10s hard timeout per attempt via asyncio.wait_for.
        STREAM-5: 15s base retry delay; 20s extra backoff on 400 Quota Violation
                  so rapid restarts self-heal after Tradier releases the old session.
        """
        for attempt in range(1, 4):
            try:
                token = await asyncio.wait_for(
                    get_session_token(), timeout=_SESSION_TOKEN_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                log.warning(
                    "[stream_manager] Session token fetch timed out after %.0fs (attempt %d/3)",
                    _SESSION_TOKEN_TIMEOUT_S, attempt,
                )
                token = None
                is_quota = False
            except Exception as e:
                err_str = str(e)
                is_quota = "400" in err_str and "Quota" in err_str
                if is_quota:
                    log.warning(
                        "[stream_manager] Session token 400 Quota Violation (attempt %d/3) — "
                        "old session still registered on Tradier, backing off %.0fs",
                        attempt, _SESSION_QUOTA_BACKOFF_S,
                    )
                else:
                    log.warning(
                        "[stream_manager] Session token fetch error (attempt %d/3): %s",
                        attempt, e,
                    )
                token = None
            else:
                is_quota = False

            if token:
                log.info("[stream_manager] Session token acquired (attempt %d)", attempt)
                return token

            log.warning(
                "[stream_manager] Session token fetch failed (attempt %d/3)", attempt
            )
            if attempt < 3:
                delay = _SESSION_QUOTA_BACKOFF_S if is_quota else _SESSION_RETRY_DELAY_S
                log.info(
                    "[stream_manager] Waiting %.0fs before retry (attempt %d/3)…",
                    delay, attempt + 1,
                )
                await asyncio.sleep(delay)

        log.error("[stream_manager] Could not acquire session token after 3 attempts")
        return None

    async def _spawn_workers(self):
        if self._registry is None:
            return
        try:
            from services.stream_worker import StreamWorker
        except ImportError:
            log.error("[stream_manager] Could not import StreamWorker")
            return

        all_symbols = self._registry.all_symbols()
        if not all_symbols:
            log.warning("[stream_manager] Registry is empty -- no workers spawned")
            return

        self._session_token = await self._fetch_session_token()
        if not self._session_token:
            log.error("[stream_manager] Aborting spawn — no session token")
            return

        chunks = [
            all_symbols[i:i + _CHUNK_SIZE]
            for i in range(0, len(all_symbols), _CHUNK_SIZE)
        ]

        log.info(
            "[stream_manager] Spawning %d workers | symbols=%d chunk_size=%d "
            "session=%s... stagger=%dms",
            len(chunks), len(all_symbols), _CHUNK_SIZE,
            self._session_token[:8], int(_WORKER_SPAWN_DELAY_S * 1000),
        )

        self._workers = []
        self._tasks   = []
        self._spawn_at = _time.time()
        self._total_ticks_at_last_health = 0
        self._last_health_at = _time.monotonic()

        for idx, chunk in enumerate(chunks):
            worker = StreamWorker(
                worker_id            = idx,
                symbols              = chunk,
                event_queue          = self._queue,
                startup_delay_s      = idx * _WORKER_SPAWN_DELAY_S,  # 50ms stagger
                shared_session_token = self._session_token,
            )
            self._workers.append(worker)
            task = asyncio.create_task(worker.run(), name=f"stream-worker-{idx}")
            self._tasks.append(task)

        log.info(
            "[stream_manager] %d workers spawned — all streaming in parallel",
            len(self._workers),
        )

    async def _respawn_workers(self, force_token_refresh: bool = False):
        """Cancel all current workers and start fresh ones."""
        if self._registry is None:
            return
        try:
            from services.stream_worker import StreamWorker
        except ImportError:
            return

        new_symbols = self._registry.all_symbols()
        if not new_symbols:
            log.warning("[stream_manager] _respawn_workers: registry empty — skipping")
            return

        old_set = {s for w in self._workers for s in w.symbols}
        new_set = set(new_symbols)
        symbols_changed = new_set != old_set

        if not symbols_changed and not force_token_refresh:
            log.debug(
                "[stream_manager] _respawn_workers: symbol set unchanged (%d) — skipping",
                len(new_set),
            )
            return

        log.info(
            "[stream_manager] _respawn_workers: old_symbols=%d new_symbols=%d "
            "force_token_refresh=%s — cancelling %d workers",
            len(old_set), len(new_set), force_token_refresh, len(self._tasks),
        )

        # Emit final health snapshot before teardown
        if self._workers:
            self._log_health()

        for task in self._tasks:
            if task is not None:
                task.cancel()
        await asyncio.gather(*[t for t in self._tasks if t is not None], return_exceptions=True)

        # Always refresh token on respawn
        self._session_token = await self._fetch_session_token()
        if not self._session_token:
            log.error("[stream_manager] _respawn_workers: no session token — aborting")
            return

        chunks = [
            new_symbols[i:i + _CHUNK_SIZE]
            for i in range(0, len(new_symbols), _CHUNK_SIZE)
        ]

        self._workers = []
        self._tasks   = []
        self._spawn_at = _time.time()
        self._total_ticks_at_last_health = 0
        self._last_health_at = _time.monotonic()

        for idx, chunk in enumerate(chunks):
            worker = StreamWorker(
                worker_id            = idx,
                symbols              = chunk,
                event_queue          = self._queue,
                startup_delay_s      = idx * _WORKER_SPAWN_DELAY_S,
                shared_session_token = self._session_token,
            )
            self._workers.append(worker)
            task = asyncio.create_task(worker.run(), name=f"stream-worker-{idx}")
            self._tasks.append(task)

        log.info(
            "[stream_manager] _respawn_workers done: %d workers for %d symbols "
            "session=%s...",
            len(self._workers), len(new_symbols), self._session_token[:8],
        )

    async def _consume_queue(self):
        if self._queue is None:
            return
        log.info(
            "[stream_manager] Queue consumer started — maxsize=%d", _QUEUE_SIZE
        )
        processed = 0
        try:
            while True:
                raw = await self._queue.get()
                try:
                    if self._process_fn:
                        await self._process_fn(raw)
                    processed += 1
                except Exception as e:
                    log.error("[stream_manager] process_fn error: %s", e)
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            log.info(
                "[stream_manager] Queue consumer stopped — total_processed=%d", processed
            )
            raise
