"""
services/stream_manager.py -- Layer 2: Parallel Stream Manager

FIX STREAM-2 (2026-04-28):
  Tradier rejects stream POSTs with >500 symbols per request.
  Fix: keep _CHUNK_SIZE=500 (original), but share ONE session token across
  all workers so only 1 Tradier session is consumed while streaming up to
  32,000 OCC symbols in 500-symbol chunks.

  Architecture:
    - StreamManager fetches 1 session token at spawn time
    - All workers receive the same token + a shared asyncio.Lock
    - Lock ensures only 1 worker holds an open stream at a time
      (satisfies Tradier 1-concurrent-session rule)
    - On 401 (token expired), manager refreshes token and respawns workers

FIX STREAM-1 (2026-04-28):
  _respawn_workers() called every _worker_refresh_s (default 300s) to
  stay in sync with registry.refresh_loop() rebuilds.
"""
import asyncio
import logging
from typing import Callable, Awaitable, Optional

from utils.tradier_client import get_session_token

log = logging.getLogger("stream_manager")

_CHUNK_SIZE  = 500        # Tradier hard limit: 500 symbols per stream POST
_QUEUE_SIZE  = 10_000
_STALE_WORKER_THRESHOLD_S: float = 60.0
_DEFAULT_WORKER_REFRESH_S: float = 300.0


class StreamManager:
    def __init__(
        self,
        registry=None,
        process_fn: Optional[Callable[[dict], Awaitable[None]]] = None,
        worker_refresh_s: float = _DEFAULT_WORKER_REFRESH_S,
    ):
        self._registry         = registry
        self._process_fn       = process_fn
        self._worker_refresh_s = worker_refresh_s
        self._workers:    list = []
        self._tasks:      list = []
        self._consumer:   Optional[asyncio.Task] = None
        self._running     = False
        self._stream      = None
        self._queue:      Optional[asyncio.Queue] = None
        # STREAM-2: shared session state
        self._session_token:   Optional[str]        = None
        self._session_lock:    Optional[asyncio.Lock] = None

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
        await self._spawn_workers()
        try:
            self._queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
            self._consumer = asyncio.create_task(self._consume_queue())
        except RuntimeError:
            pass

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            if task is not None:
                task.cancel()
        if self._consumer is not None:
            self._consumer.cancel()
        tasks = [t for t in self._tasks if t is not None]
        if self._consumer is not None:
            tasks.append(self._consumer)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._workers.clear()
        log.info("[stream_manager] All workers stopped")

    async def run(self):
        """
        Long-running entry point.
        Every _worker_refresh_s seconds, respawn workers against the
        current registry symbol set (STREAM-1).
        Also refreshes the shared session token on respawn.
        """
        self._running = True
        self._queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        log.info("[stream_manager] Starting...")
        await self._spawn_workers()
        self._consumer = asyncio.create_task(self._consume_queue())
        elapsed = 0.0
        try:
            while self._running:
                await asyncio.sleep(60)
                elapsed += 60.0
                # Check if any worker flagged token expiry
                if self._any_token_expired():
                    log.info("[stream_manager] Token expired signal — refreshing session")
                    await self._respawn_workers(force_token_refresh=True)
                    elapsed = 0.0
                elif elapsed >= self._worker_refresh_s:
                    elapsed = 0.0
                    await self._respawn_workers()
        except asyncio.CancelledError:
            await self.stop()
            raise

    def status(self) -> dict:
        return {
            "running":        self._running,
            "workers":        len(self._workers),
            "active_symbols": self._registry.size() if self._registry else 0,
        }

    @property
    def stats(self) -> dict:
        return self.status()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _any_token_expired(self) -> bool:
        return any(getattr(w, "_token_expired", False) for w in self._workers)

    async def _fetch_session_token(self) -> Optional[str]:
        """Fetch a fresh session token with retries."""
        for attempt in range(3):
            token = await get_session_token()
            if token:
                log.info("[stream_manager] Session token acquired")
                return token
            log.warning("[stream_manager] Session token fetch failed (attempt %d/3)", attempt + 1)
            await asyncio.sleep(2.0)
        log.error("[stream_manager] Could not acquire session token after 3 attempts")
        return None

    async def _spawn_workers(self):
        if self._registry is None:
            return
        try:
            from services.stream_worker import StreamWorker
        except ImportError:
            return

        all_symbols = self._registry.all_symbols()
        if not all_symbols:
            log.warning("[stream_manager] Registry is empty -- no workers spawned")
            return

        # STREAM-2: fetch ONE shared session token for all workers
        self._session_token = await self._fetch_session_token()
        if not self._session_token:
            log.error("[stream_manager] Aborting spawn — no session token")
            return
        self._session_lock = asyncio.Lock()

        chunks = [
            all_symbols[i:i + _CHUNK_SIZE]
            for i in range(0, len(all_symbols), _CHUNK_SIZE)
        ]
        self._workers = []
        self._tasks   = []
        for idx, chunk in enumerate(chunks):
            worker = StreamWorker(
                worker_id            = idx,
                symbols              = chunk,
                event_queue          = self._queue,
                startup_delay_s      = 0.0,
                shared_session_token = self._session_token,
                session_lock         = self._session_lock,
            )
            self._workers.append(worker)
            task = asyncio.create_task(worker.run(), name=f"stream-worker-{idx}")
            self._tasks.append(task)
        log.info(
            "[stream_manager] Spawned %d workers for %d OCC symbols (%d chunks of %d)",
            len(self._workers), len(all_symbols), len(chunks), _CHUNK_SIZE,
        )

    async def _respawn_workers(self, force_token_refresh: bool = False):
        """
        Replace all workers with fresh ones.
        Always refreshes session token on respawn.
        """
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

        old_set = set()
        for w in self._workers:
            old_set.update(w.symbols)
        new_set = set(new_symbols)

        symbols_changed = new_set != old_set
        if not symbols_changed and not force_token_refresh:
            log.debug(
                "[stream_manager] _respawn_workers: symbol set unchanged (%d) — skipping",
                len(new_set),
            )
            return

        log.info(
            "[stream_manager] _respawn_workers: old=%d new=%d token_refresh=%s — replacing workers",
            len(old_set), len(new_set), force_token_refresh,
        )

        # Cancel old tasks
        for task in self._tasks:
            if task is not None:
                task.cancel()
        old_tasks = [t for t in self._tasks if t is not None]
        if old_tasks:
            await asyncio.gather(*old_tasks, return_exceptions=True)

        # Always get a fresh token on respawn
        self._session_token = await self._fetch_session_token()
        if not self._session_token:
            log.error("[stream_manager] _respawn_workers: no session token — aborting")
            return
        self._session_lock = asyncio.Lock()

        chunks = [
            new_symbols[i:i + _CHUNK_SIZE]
            for i in range(0, len(new_symbols), _CHUNK_SIZE)
        ]
        self._workers = []
        self._tasks   = []
        for idx, chunk in enumerate(chunks):
            worker = StreamWorker(
                worker_id            = idx,
                symbols              = chunk,
                event_queue          = self._queue,
                startup_delay_s      = 0.0,
                shared_session_token = self._session_token,
                session_lock         = self._session_lock,
            )
            self._workers.append(worker)
            task = asyncio.create_task(worker.run(), name=f"stream-worker-{idx}")
            self._tasks.append(task)

        log.info(
            "[stream_manager] _respawn_workers: %d new workers for %d OCC symbols",
            len(self._workers), len(new_symbols),
        )

    async def _consume_queue(self):
        if self._queue is None:
            return
        log.info("[stream_manager] Queue consumer started")
        try:
            while True:
                raw = await self._queue.get()
                try:
                    if self._process_fn:
                        await self._process_fn(raw)
                except Exception as e:
                    log.error(f"[stream_manager] process_fn error: {e}")
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            log.info("[stream_manager] Queue consumer stopped")
            raise
