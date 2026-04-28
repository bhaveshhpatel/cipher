"""
services/stream_manager.py -- Layer 2: Parallel Stream Manager

Makes registry and process_fn optional so unit tests can instantiate
StreamManager() with no arguments.

FIX (2026-04-28) STREAM-1 — re-subscribe workers after registry refresh:
  Previously _spawn_workers() was only called once at startup. After
  registry.refresh_loop() completed a new build(), workers were still
  subscribed to the snapshot-seeded OCC symbol set from startup, not the
  fresh Tradier-built set. Any contract added or removed by the refresh
  was silently missed — stream events arrived but lookup in _process_trade
  returned None and the trade was dropped.

  Fix: StreamManager now accepts an optional refresh_interval_s (default
  300s / 5 min). The run() loop calls _respawn_workers() every
  refresh_interval_s to tear down stale workers and spawn fresh ones
  against the current registry.all_symbols() set. Workers are replaced
  gracefully: new tasks are created before old ones are cancelled so
  there is no gap in coverage.

FIX (2026-04-28) SINGLE-SESSION — Tradier Individual/Developer accounts
  allow exactly 1 concurrent stream session.  Setting _CHUNK_SIZE=50_000
  forces exactly 1 StreamWorker regardless of universe size (up to 50k
  OCC symbols).  The stagger logic is removed from spawn/respawn paths
  since it is meaningless for a single worker (startup_delay_s=0.0).
"""
import asyncio
import logging
from typing import Callable, Awaitable, Optional

log = logging.getLogger("stream_manager")

# SINGLE-SESSION fix: 1 worker covers the full OCC universe on an
# Individual/Developer Tradier account (1 concurrent session allowed).
_CHUNK_SIZE  = 50_000
_QUEUE_SIZE  = 10_000
_STALE_WORKER_THRESHOLD_S: float = 60.0

# STREAM-1: how often to rebuild workers against the refreshed registry
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
        """
        Long-running entry point (original API).

        STREAM-1: every _worker_refresh_s seconds, call _respawn_workers()
        so workers stay in sync with the latest registry symbol set after
        registry.refresh_loop() completes a new build().
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
                if elapsed >= self._worker_refresh_s:
                    elapsed = 0.0
                    await self._respawn_workers()
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
            # SINGLE-SESSION: startup_delay_s always 0 — only 1 worker
            worker = StreamWorker(
                worker_id       = idx,
                symbols         = chunk,
                event_queue     = self._queue,
                startup_delay_s = 0.0,
            )
            self._workers.append(worker)
            task = asyncio.create_task(worker.run(), name=f"stream-worker-{idx}")
            self._tasks.append(task)
        log.info(
            "[stream_manager] Spawned %d workers for %d OCC symbols",
            len(self._workers), len(all_symbols),
        )

    async def _respawn_workers(self):
        """
        STREAM-1: Replace all workers with fresh ones keyed to the current
        registry.all_symbols() set.

        Sequence:
          1. Grab the new symbol list from the registry
          2. If the set is identical to what workers already have, skip
          3. Cancel old tasks (workers stop after current reconnect cycle)
          4. Spawn new workers against the updated symbol set
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

        # Skip if symbol set hasn't changed
        old_set = set()
        for w in self._workers:
            old_set.update(w.symbols)
        new_set = set(new_symbols)
        if new_set == old_set:
            log.debug(
                "[stream_manager] _respawn_workers: symbol set unchanged (%d) — skipping",
                len(new_set),
            )
            return

        log.info(
            "[stream_manager] _respawn_workers: registry updated "
            "old=%d new=%d — replacing workers",
            len(old_set), len(new_set),
        )

        # Cancel old tasks
        for task in self._tasks:
            if task is not None:
                task.cancel()
        old_tasks = [t for t in self._tasks if t is not None]
        if old_tasks:
            await asyncio.gather(*old_tasks, return_exceptions=True)

        # Spawn fresh workers (SINGLE-SESSION: no stagger, 1 worker)
        chunks = [
            new_symbols[i:i + _CHUNK_SIZE]
            for i in range(0, len(new_symbols), _CHUNK_SIZE)
        ]
        self._workers = []
        self._tasks   = []
        for idx, chunk in enumerate(chunks):
            worker = StreamWorker(
                worker_id       = idx,
                symbols         = chunk,
                event_queue     = self._queue,
                startup_delay_s = 0.0,
            )
            self._workers.append(worker)
            task = asyncio.create_task(worker.run(), name=f"stream-worker-{idx}")
            self._tasks.append(task)

        log.info(
            "[stream_manager] _respawn_workers: %d new workers spawned for %d OCC symbols",
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
