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
  POST stream concurrently -- all 31,920 symbols covered from T+0.

STREAM-4 (2026-04-30):
  Add asyncio.wait_for(timeout=10s) around get_session_token() to prevent
  silent infinite hang when Tradier session endpoint is unresponsive.

STREAM-5 (2026-04-30):
  Increase retry delay from 2s -> 15s so rapid container restarts self-heal
  after Tradier's quota releases. Explicit 400 Quota Violation handling with
  20s backoff (Tradier session TTL is ~10-15s after connection drop).

STREAM-6 (2026-04-30):
  Raise _STALL_THRESHOLD_S 60s -> 300s to match the new 120s _IDLE_TIMEOUT
  in stream_worker.py. With 64 workers x 500 symbols at ~0.6 ticks/s total,
  each worker expects ~1 tick per 110s. The old 60s threshold produced
  stalled=63 in every STREAM_HEALTH report even when all workers were healthy.

STREAM-7 (2026-05-01):
  Fix QUEUE_FULL drops at market open. Root cause: _consume_queue() awaited
  _process_trade serially. persist_flow_event() has a 2s timeout and
  accumulator.ingest_tick() has non-trivial latency, so the drain rate lagged
  far behind the 64-worker ingest rate, causing QUEUE_FULL at depth=50,000.

  Fix: drain the queue non-blocking via create_task(), bounded by a
  Semaphore(_PROCESS_CONCURRENCY=32). Each _process_trade acquires the
  semaphore on entry and releases on exit, capping concurrent executions
  to 32. This prevents unbounded parallelism on shared accumulator / dedup
  state while keeping the queue from backing up.

  Also: asyncio.sleep(60) -> asyncio.sleep(10) in run() main loop so
  401 token-expired events are detected and respawned within 10s instead
  of up to 60s (previous worst-case session gap).

FIX-A (2026-05-18):
  Replace flat 50ms stagger with proportional spread across ~10s.
  Previous: idx * _WORKER_SPAWN_DELAY_S (50ms) spread 64 workers over ~3s,
  causing thundering-herd on Tradier's stream endpoint (400 Quota Violation /
  dropped connections on late workers). New formula:
    startup_delay_s = idx * (10.0 / max(len(chunks), 1))
  Spreads workers evenly over exactly 10s regardless of worker count.
  Applied to both _spawn_workers() and _respawn_workers() so every
  401-triggered respawn also gets the gradual ramp.

FIX-C (2026-05-18) — revised in STREAM-13:
  See STREAM-13 below.

STREAM-13 (2026-05-18):
  Fix FIX-C dead code — post-spawn token refresh now actually fires.

  FIX-C checked `spawn_duration > _SPAWN_WINDOW_S` after the spawn loop.
  The spawn loop is a plain Python for-loop creating asyncio.Tasks — it
  contains no awaits — so it completes in microseconds. `spawn_duration`
  was always ~0.001s, never > 10s, so the token refresh never executed.

  The actual race: each worker sleeps `startup_delay_s` (up to 10s) inside
  its asyncio.Task before its first POST. The session token was fetched
  before the spawn loop. If the last worker's startup_delay_s ≈ 10s, it
  POSTs at T+10s — right at Tradier's ~10-15s session TTL boundary,
  risking an immediate 400/401.

  Fix: schedule a one-shot _post_spawn_token_refresh() coroutine as an
  asyncio.Task immediately after the spawn loop. It sleeps
  _SPAWN_WINDOW_S + _TOKEN_REFRESH_GRACE_S (10s + 2s = 12s total), then
  fetches a fresh token and pushes it to all workers via w._shared_token.
  Workers that have already connected read self._shared_token at the top
  of their run() loop on next reconnect. Workers still in their startup
  sleep pick up the fresh token on their very first loop iteration.

  Only applied in _spawn_workers(); _respawn_workers() always fetches a
  fresh token immediately before its own loop so the TTL race does not
  apply there.

STREAM-14 (2026-05-20):
  Fix health metric corruption when _respawn_workers() replaces self._workers
  mid-iteration inside _log_health().

  _log_health() iterated self._workers directly via sum() generators. If
  _respawn_workers() swapped self._workers to a new list while those
  generators were live (possible because both run on the same event loop
  and asyncio.sleep yields between iterations), the aggregated totals
  (ticks, errors, reconnects, stalled) could span a mix of old and new
  worker objects, producing nonsensical STREAM_HEALTH log lines.

  Fix: snapshot workers = list(self._workers) at the very top of
  _log_health(). All subsequent iteration and sum() calls operate on the
  stable snapshot, not the live attribute.

STREAM-15 (2026-05-20):
  Fix spurious ValueError from task_done() in _consume_queue._run_process().

  task_done() was called unconditionally in the finally block, which fires
  whether or not get() actually succeeded. If get() raised (e.g., the queue
  was drained and cancelled mid-flight), task_done() would be called without
  a matching get(), raising ValueError: task_done() called too many times.

  Fix: remove task_done() from finally. Call it explicitly after the
  process_fn invocation succeeds or raises — i.e., exactly once per
  confirmed get(). Wrapped in try/except ValueError as a belt-and-suspenders
  guard against any edge case where the count drifts.

Architecture
------------
  - 1 session token fetched at spawn time, shared to all workers
  - 64 workers x 500 symbols = 31,920 OCC symbols, all streaming in parallel
  - Workers staggered proportionally over ~10s (FIX-A) to avoid
    thundering-herd on Tradier endpoint
  - STREAM-13: token refreshed 12s post-spawn, pushed to all workers so the
    last worker connects with a fresh token regardless of TTL boundary
  - asyncio.Queue(maxsize=50_000) feeds a single _consume_queue() task
  - _consume_queue drains at wire speed; _process_trade runs concurrently
    under a Semaphore(32) cap to protect shared accumulator/dedup state
  - Manager logs STREAM_HEALTH every 30s: aggregate ticks, active workers,
    stalled workers, queue depth, global tick rate
  - On 401 from any worker: _token_expired flag detected within 10s,
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
_WORKER_SPAWN_DELAY_S    = 0.05       # kept for reference; spawn loop now uses proportional formula
_HEALTH_LOG_INTERVAL_S   = 30.0       # manager-level aggregate log interval
_DEFAULT_WORKER_REFRESH_S: float = 300.0
_STALL_THRESHOLD_S       = 300.0      # STREAM-6: raised from 60s; matches 120s idle timeout with headroom
_SESSION_TOKEN_TIMEOUT_S = 10.0       # hard timeout for each get_session_token() attempt (STREAM-4)
_SESSION_RETRY_DELAY_S   = 15.0       # delay between retry attempts (STREAM-5)
_SESSION_QUOTA_BACKOFF_S = 20.0       # extra backoff on 400 Quota Violation (STREAM-5)
_PROCESS_CONCURRENCY     = 32         # STREAM-7: max concurrent _process_trade coroutines
_TOKEN_POLL_INTERVAL_S   = 10.0       # STREAM-7: poll interval for 401 detection (was 60s)
_SPAWN_WINDOW_S          = 10.0       # FIX-A: target window to spread worker startups
_TOKEN_REFRESH_GRACE_S   = 2.0        # STREAM-13: extra grace after last worker wakes before token refresh


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
        # STREAM-7: semaphore to cap concurrent process_fn executions
        self._process_sem: asyncio.Semaphore = asyncio.Semaphore(_PROCESS_CONCURRENCY)

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
        log.info("[stream_manager] All workers stopped -- Tradier connections closed")

    async def run(self):
        """
        Long-running entry point called by main.
        Runs health logging + periodic worker refresh in parallel.
        """
        self._running = True
        self._queue = asyncio.Queue(maxsize=_QUEUE_SIZE)
        log.info("[stream_manager] Starting -- chunk_size=%d queue_size=%d",
                 _CHUNK_SIZE, _QUEUE_SIZE)
        await self._spawn_workers()
        self._consumer    = asyncio.create_task(self._consume_queue(),   name="stream-consumer")
        self._health_task = asyncio.create_task(self._health_loop(),     name="stream-health")
        elapsed = 0.0
        try:
            while self._running:
                # STREAM-7: poll every 10s (was 60s) so 401 token expiry is
                # detected and respawned within one poll cycle, not up to 60s.
                await asyncio.sleep(_TOKEN_POLL_INTERVAL_S)
                elapsed += _TOKEN_POLL_INTERVAL_S
                if self._any_token_expired():
                    log.warning("[stream_manager] Token expired detected -- refreshing session + respawning")
       