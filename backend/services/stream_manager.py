"""
services/stream_manager.py -- Layer 2: Parallel Stream Manager
"""
import asyncio
import logging
import time as _time
from typing import Callable, Awaitable, Optional

from services.symbol_registry import SymbolRegistry
from services.stream_worker import StreamWorker

log = logging.getLogger("stream_manager")

_CHUNK_SIZE  = 500
_QUEUE_SIZE  = 10_000

_WORKER_STARTUP_STAGGER_MS: int   = 200
_WORKER_STARTUP_STAGGER_S:  float = _WORKER_STARTUP_STAGGER_MS / 1000.0

_STALE_WORKER_THRESHOLD_S: float = 60.0


class StreamManager:
    def __init__(
        self,
        registry: SymbolRegistry,
        process_fn: Callable[[dict], Awaitable[None]],
    ):
        self._registry    = registry
        self._process_fn  = process_fn
        self._queue:      asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._workers:    list[StreamWorker] = []
        self._tasks:      list[asyncio.Task] = []
        self._consumer:   Optional[asyncio.Task] = None
        self._running     = False

    async def run(self):
        self._running = True
        log.info("[stream_manager] Starting...")

        await self._spawn_workers()

        self._consumer = asyncio.create_task(self._consume_queue())

        try:
            while self._running:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            await self.stop()
            raise

    async def stop(self):
        self._running = False
        log.info("[stream_manager] Stopping all workers...")
        for task in self._tasks:
            task.cancel()
        if self._consumer:
            self._consumer.cancel()
        await asyncio.gather(*self._tasks, self._consumer, return_exceptions=True)
        self._tasks.clear()
        self._workers.clear()
        log.info("[stream_manager] All workers stopped")

    async def refresh(self):
        new_symbols = self._registry.all_symbols()
        new_set     = set(new_symbols)
        old_set: set = set()
        for w in self._workers:
            old_set.update(w.symbols)

        added   = new_set - old_set
        removed = old_set - new_set

        if not added and not removed:
            log.info("[stream_manager] Refresh: no symbol changes -- workers unchanged")
            return

        log.info(
            "[stream_manager] Refresh: +%d / -%d symbols",
            len(added), len(removed),
        )

        if not self._workers:
            log.info("[stream_manager] Refresh: no existing workers -- full spawn")
            await self._spawn_workers()
            if not self._consumer or self._consumer.done():
                self._consumer = asyncio.create_task(self._consume_queue())
            return

        affected_set = added | removed
        affected_indices: list[int] = []
        for idx, worker in enumerate(self._workers):
            if any(sym in affected_set for sym in worker.symbols):
                affected_indices.append(idx)

        if not affected_indices:
            log.info("[stream_manager] Refresh: diff symbols not in any active chunk -- no restart needed")
            return

        log.info(
            "[stream_manager] Refresh: restarting %d / %d workers (indices: %s)",
            len(affected_indices), len(self._workers), affected_indices,
        )

        chunks = [
            new_symbols[i:i + _CHUNK_SIZE]
            for i in range(0, len(new_symbols), _CHUNK_SIZE)
        ]

        for idx in affected_indices:
            if idx < len(self._tasks):
                self._tasks[idx].cancel()
                try:
                    await self._tasks[idx]
                except (asyncio.CancelledError, Exception):
                    pass

            if idx < len(chunks):
                new_chunk = chunks[idx]
            else:
                if idx < len(self._workers):
                    self._workers[idx] = None  # type: ignore[assignment]
                if idx < len(self._tasks):
                    self._tasks[idx] = None    # type: ignore[assignment]
                continue

            startup_delay = idx * _WORKER_STARTUP_STAGGER_S
            new_worker = StreamWorker(
                worker_id       = idx,
                symbols         = new_chunk,
                event_queue     = self._queue,
                startup_delay_s = startup_delay,
            )
            new_task = asyncio.create_task(
                new_worker.run(), name=f"stream-worker-{idx}"
            )

            if idx < len(self._workers):
                self._workers[idx] = new_worker
                self._tasks[idx]   = new_task
            else:
                self._workers.append(new_worker)
                self._tasks.append(new_task)

        for idx in range(len(self._workers), len(chunks)):
            startup_delay = idx * _WORKER_STARTUP_STAGGER_S
            new_worker = StreamWorker(
                worker_id       = idx,
                symbols         = chunks[idx],
                event_queue     = self._queue,
                startup_delay_s = startup_delay,
            )
            new_task = asyncio.create_task(
                new_worker.run(), name=f"stream-worker-{idx}"
            )
            self._workers.append(new_worker)
            self._tasks.append(new_task)

        self._workers = [w for w in self._workers if w is not None]
        self._tasks   = [t for t in self._tasks   if t is not None]

        if not self._consumer or self._consumer.done():
            self._consumer = asyncio.create_task(self._consume_queue())

        log.info(
            "[stream_manager] Surgical refresh complete: %d workers active",
            len(self._workers),
        )

    @property
    def stats(self) -> dict:
        now              = _time.time()
        total_ticks      = sum(w._ticks      for w in self._workers)
        total_errors     = sum(w._errors     for w in self._workers)
        total_reconnects = sum(w._reconnects for w in self._workers)

        stale_workers = sum(
            1 for w in self._workers
            if w._last_tick_at is None
            or (now - w._last_tick_at) > _STALE_WORKER_THRESHOLD_S
        )

        return {
            "workers":           len(self._workers),
            "active_symbols":    self._registry.size(),
            "queue_size":        self._queue.qsize(),
            "total_ticks":       total_ticks,
            "total_errors":      total_errors,
            "total_reconnects":  total_reconnects,
            "stale_workers":     stale_workers,
            "worker_detail":     [w.stats for w in self._workers],
        }

    async def _spawn_workers(self):
        all_symbols = self._registry.all_symbols()
        if not all_symbols:
            log.warning("[stream_manager] Registry is empty -- no workers spawned")
            return

        chunks = [
            all_symbols[i:i + _CHUNK_SIZE]
            for i in range(0, len(all_symbols), _CHUNK_SIZE)
        ]
        total_stagger_s = (len(chunks) - 1) * _WORKER_STARTUP_STAGGER_S
        log.info(
            "[stream_manager] Spawning %d workers for %s OCC symbols "
            "(%d symbols/worker) | stagger=%dms | total startup window=%.1fs (B-021)",
            len(chunks), f"{len(all_symbols):,}", _CHUNK_SIZE,
            _WORKER_STARTUP_STAGGER_MS, total_stagger_s,
        )

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
        log.info("[stream_manager] Queue consumer started")
        try:
            while True:
                raw = await self._queue.get()
                try:
                    await self._process_fn(raw)
                except Exception as e:
                    log.error(f"[stream_manager] process_fn error: {e}")
                finally:
                    self._queue.task_done()
        except asyncio.CancelledError:
            log.info("[stream_manager] Queue consumer stopped")
            raise
