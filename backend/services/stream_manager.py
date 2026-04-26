"""
services/stream_manager.py -- Layer 2: Parallel Stream Manager

Makes registry and process_fn optional so unit tests can instantiate
StreamManager() with no arguments.
"""
import asyncio
import logging
import time as _time
from typing import Callable, Awaitable, Optional

log = logging.getLogger("stream_manager")

_CHUNK_SIZE  = 500
_QUEUE_SIZE  = 10_000
_WORKER_STARTUP_STAGGER_MS: int   = 200
_WORKER_STARTUP_STAGGER_S:  float = _WORKER_STARTUP_STAGGER_MS / 1000.0
_STALE_WORKER_THRESHOLD_S: float = 60.0


class StreamManager:
    def __init__(
        self,
        registry=None,
        process_fn: Optional[Callable[[dict], Awaitable[None]]] = None,
    ):
        self._registry    = registry
        self._process_fn  = process_fn
        self._workers:    list = []
        self._tasks:      list = []
        self._consumer:   Optional[asyncio.Task] = None
        self._running     = False
        self._stream      = None  # injectable mock stream for tests
        self._queue:      Optional[asyncio.Queue] = None

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        """True when the manager is actively streaming."""
        return self._running

    @running.setter
    def running(self, value: bool) -> None:
        self._running = value

    def is_running(self) -> bool:
        """Method form of running check — for test compatibility."""
        return self._running

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start streaming. No-op if no registry is configured."""
        if self._registry is None:
            log.warning("[stream_manager] start() called with no registry -- no-op")
            return
        self._running = True
        await self._spawn_workers()
        try:
            self._queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
            self._consumer = asyncio.create_task(self._consume_queue())
        except RuntimeError:
            pass  # no running event loop in tests

    async def stop(self) -> None:
        """Stop all workers gracefully."""
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
        """Long-running entry point (original API)."""
        self._running = True
        self._queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        log.info("[stream_manager] Starting...")
        await self._spawn_workers()
        self._consumer = asyncio.create_task(self._consume_queue())
        try:
            while self._running:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            await self.stop()
            raise

    def status(self) -> dict:
        """Return a dict describing current manager state."""
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
        chunks = [
            all_symbols[i:i + _CHUNK_SIZE]
            for i in range(0, len(all_symbols), _CHUNK_SIZE)
        ]
        self._workers = []
        self._tasks   = []
        for idx, chunk in enumerate(chunks):
            startup_delay = idx * _WORKER_STARTUP_STAGGER_S
            worker = StreamWorker(
                worker_id       = idx,
                symbols         = chunk,
                event_queue     = self._queue,
                startup_delay_s = startup_delay,
            )
            self._workers.append(worker)
            task = asyncio.create_task(worker.run(), name=f"stream-worker-{idx}")
            self._tasks.append(task)

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
