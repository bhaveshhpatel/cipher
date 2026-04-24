"""
services/stream_manager.py — Layer 2: Parallel Stream Manager

Manages N parallel StreamWorker instances to cover the full OCC symbol
registry. Tradier limits ~500 symbols per stream connection, so with
~16,000 OCC contracts we need ~32 workers.

Responsibilities:
  1. Split OCC symbol list into 500-symbol chunks
  2. Spawn one StreamWorker per chunk
  3. All workers push to a single shared asyncio.Queue
  4. One consumer task drains the queue → parse → dedup → DB write
  5. On registry refresh: diff old vs new symbols, restart only affected workers
  6. Expose aggregate stats for /health endpoint

USAGE in main.py:
  manager = StreamManager(registry=registry, process_fn=_process_trade)
  asyncio.create_task(manager.run())
"""
import asyncio
import logging
from typing import Callable, Awaitable, Optional

from services.symbol_registry import SymbolRegistry
from services.stream_worker import StreamWorker

log = logging.getLogger("stream_manager")

_CHUNK_SIZE  = 500    # OCC symbols per stream connection
_QUEUE_SIZE  = 10_000  # max buffered events before dropping


class StreamManager:
    """
    Orchestrates all StreamWorker instances and feeds events to the processor.
    """

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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self):
        """
        Main entry point. Spawns workers and consumer, then blocks.
        Restart safe — call again after stopping.
        """
        self._running = True
        log.info("[stream_manager] Starting...")

        # Initial spawn based on current registry
        await self._spawn_workers()

        # Start queue consumer
        self._consumer = asyncio.create_task(self._consume_queue())

        # Wait — tasks handle their own reconnection
        try:
            while self._running:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            await self.stop()
            raise

    async def stop(self):
        """Graceful shutdown — cancel all workers and consumer."""
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
        """
        Called after symbol registry rebuild.
        Diffs old vs new symbol set, restarts only affected workers.
        """
        new_symbols = self._registry.all_symbols()
        new_set = set(new_symbols)
        old_set = set()
        for w in self._workers:
            old_set.update(w.symbols)

        added   = new_set - old_set
        removed = old_set - new_set

        if not added and not removed:
            log.info("[stream_manager] Refresh: no symbol changes — workers unchanged")
            return

        log.info(
            f"[stream_manager] Refresh: +{len(added)} / -{len(removed)} symbols "
            f"— restarting affected workers"
        )

        # Full restart is simplest and most reliable
        await self.stop()
        await self._spawn_workers()
        self._consumer = asyncio.create_task(self._consume_queue())

    @property
    def stats(self) -> dict:
        total_ticks    = sum(w._ticks    for w in self._workers)
        total_errors   = sum(w._errors   for w in self._workers)
        total_reconnects = sum(w._reconnects for w in self._workers)
        return {
            "workers":      len(self._workers),
            "active_symbols": self._registry.size(),
            "queue_size":   self._queue.qsize(),
            "total_ticks":  total_ticks,
            "total_errors": total_errors,
            "total_reconnects": total_reconnects,
            "worker_detail": [w.stats for w in self._workers],
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _spawn_workers(self):
        """Create StreamWorker for each 500-symbol chunk and start tasks."""
        all_symbols = self._registry.all_symbols()
        if not all_symbols:
            log.warning("[stream_manager] Registry is empty — no workers spawned")
            return

        chunks = [
            all_symbols[i:i + _CHUNK_SIZE]
            for i in range(0, len(all_symbols), _CHUNK_SIZE)
        ]
        log.info(
            f"[stream_manager] Spawning {len(chunks)} workers "
            f"for {len(all_symbols):,} OCC symbols ({_CHUNK_SIZE} symbols/worker)"
        )

        self._workers = []
        self._tasks   = []
        for idx, chunk in enumerate(chunks):
            worker = StreamWorker(
                worker_id   = idx,
                symbols     = chunk,
                event_queue = self._queue,
            )
            self._workers.append(worker)
            task = asyncio.create_task(worker.run(), name=f"stream-worker-{idx}")
            self._tasks.append(task)

    async def _consume_queue(self):
        """
        Drain the shared event queue and call process_fn on each raw event.
        This is the single consumer — all workers are producers.
        """
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
