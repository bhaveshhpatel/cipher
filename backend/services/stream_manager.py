"""
services/stream_manager.py -- Layer 2: Parallel Stream Manager

Manages N parallel StreamWorker instances to cover the full OCC symbol
registry. Tradier limits ~500 symbols per stream connection, so with
~16,000 OCC contracts we need ~32 workers.

Responsibilities:
  1. Split OCC symbol list into 500-symbol chunks
  2. Spawn one StreamWorker per chunk
  3. All workers push to a single shared asyncio.Queue
  4. One consumer task drains the queue -> parse -> dedup -> DB write
  5. On registry refresh: diff old vs new symbols, restart only affected workers
  6. Expose aggregate stats for /health endpoint

B-021 -- Staggered worker startup:
  Workers are started with a 200ms delay between each one.
  With 32 workers this adds a one-time 6.4s startup cost before all
  workers are streaming. There is zero ongoing latency impact -- once
  connected each worker streams in real-time exactly as before.
  The stagger prevents a simultaneous burst of ~32 session-token
  requests hitting Tradier at t=0, which was causing silent 429s and
  immediate reconnect death spirals.

Fix (F-03) -- Surgical worker restart on refresh():
  Previously refresh() called stop() + _spawn_workers() on every symbol
  change, which cancelled ALL 32 workers and re-triggered the full
  B-021/B-022 thundering herd (6.4s stagger, 32 simultaneous session
  token requests). This happened every 30 minutes on the normal registry
  refresh cycle, even when only a handful of contracts changed.

  Now refresh() identifies which worker *chunks* overlap the added/removed
  symbol diff and only cancels + respawns those workers. Workers whose
  chunks are entirely unaffected by the diff continue streaming without
  interruption. The original full restart path is preserved as a fallback
  when _workers is empty (cold boot or after a manual stop).

  Stagger delay: re-spawned workers receive the same startup_delay_s as
  their original index (idx * _WORKER_STARTUP_STAGGER_S) to maintain
  B-021 behaviour.

USAGE in main.py:
  manager = StreamManager(registry=registry, process_fn=_process_trade)
  asyncio.create_task(manager.run())
"""
import asyncio
import logging
import math
from typing import Callable, Awaitable, Optional

from services.symbol_registry import SymbolRegistry
from services.stream_worker import StreamWorker

log = logging.getLogger("stream_manager")

_CHUNK_SIZE  = 500     # OCC symbols per stream connection
_QUEUE_SIZE  = 10_000  # max buffered events before dropping

# B-021: delay between successive worker starts (seconds)
_WORKER_STARTUP_STAGGER_MS: int   = 200
_WORKER_STARTUP_STAGGER_S:  float = _WORKER_STARTUP_STAGGER_MS / 1000.0


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
        Restart safe -- call again after stopping.
        """
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
        """Graceful shutdown -- cancel all workers and consumer."""
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

        F-03: Surgical restart -- only workers whose symbol chunks overlap
        the added/removed diff are cancelled and respawned. Workers whose
        chunks are entirely unaffected continue streaming without interruption.

        Falls back to full stop()+_spawn_workers() when _workers is empty
        (cold boot or post-manual-stop).
        """
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

        # Cold boot / post-stop fallback: no workers yet, full spawn
        if not self._workers:
            log.info("[stream_manager] Refresh: no existing workers -- full spawn")
            await self._spawn_workers()
            if not self._consumer or self._consumer.done():
                self._consumer = asyncio.create_task(self._consume_queue())
            return

        # F-03: identify which existing workers touch the diff
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

        # Build new full symbol list chunked identically to _spawn_workers
        chunks = [
            new_symbols[i:i + _CHUNK_SIZE]
            for i in range(0, len(new_symbols), _CHUNK_SIZE)
        ]

        for idx in affected_indices:
            # Cancel the existing task for this worker
            if idx < len(self._tasks):
                self._tasks[idx].cancel()
                try:
                    await self._tasks[idx]
                except (asyncio.CancelledError, Exception):
                    pass

            # Determine new chunk for this slot (may not exist if registry shrank)
            if idx < len(chunks):
                new_chunk = chunks[idx]
            else:
                # Registry shrank below this index -- slot is now unused
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

        # Handle newly added chunks beyond original worker count
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

        # Prune None slots (shrunken registry)
        self._workers = [w for w in self._workers if w is not None]
        self._tasks   = [t for t in self._tasks   if t is not None]

        # Restart consumer if it died
        if not self._consumer or self._consumer.done():
            self._consumer = asyncio.create_task(self._consume_queue())

        log.info(
            "[stream_manager] Surgical refresh complete: %d workers active",
            len(self._workers),
        )

    @property
    def stats(self) -> dict:
        total_ticks      = sum(w._ticks      for w in self._workers)
        total_errors     = sum(w._errors     for w in self._workers)
        total_reconnects = sum(w._reconnects for w in self._workers)
        return {
            "workers":           len(self._workers),
            "active_symbols":    self._registry.size(),
            "queue_size":        self._queue.qsize(),
            "total_ticks":       total_ticks,
            "total_errors":      total_errors,
            "total_reconnects":  total_reconnects,
            "worker_detail":     [w.stats for w in self._workers],
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _spawn_workers(self):
        """
        Create StreamWorker for each 500-symbol chunk and start tasks.

        B-021: Workers are staggered by _WORKER_STARTUP_STAGGER_S (200ms)
        between each spawn. Each worker receives its startup_delay_s so it
        can sleep before opening its first Tradier connection.
        """
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
        """
        Drain the shared event queue and call process_fn on each raw event.
        This is the single consumer -- all workers are producers.
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
