"""
services/symbol_registry.py — Layer 1: OCC Symbol Registry

FIX P3 (2026-04-27): _build_ticker now uses get_option_chain_bulk() instead
  of get_option_chain() so build() uses _BULK_CHAIN_SEM(10) rather than the
  live-stream _CHAIN_SEM(2). Cold-start chain throughput increases ~5x.

FIX P4 (2026-04-27): build() now performs an incremental warm-restart when
  the registry was pre-seeded from DB (load_from_db returned > 0 rows). Only
  tickers whose minimum DTE in the seeded registry is 0 (contracts expired
  today) are re-fetched; all other tickers are carried forward unchanged.
  Warm-restart chain API calls drop from ~17,360 to ~50-400.

FIX C-3 (2026-04-27): assign_tiers() is now called with require_oi=True
  in the post-build reclassification step. This ensures OI is enforced in
  the tier gate only after build() has populated OI data from chain fetches.
  Pre-build tier assignments (main.py Step 3b) default to require_oi=False,
  which skips the OI gate and produces stable T1/T2 counts based on
  volume and price alone.

FIX H1 (2026-04-27): build() now returns a tuple[int, dict[str, dict]]
  (count, raw_quotes). Callers that only need the count ignore the second
  element; _background_build_and_upsert passes raw_quotes to
  _post_build_upsert so it can skip the duplicate _fetch_batch_quotes call.

FIX H3 (2026-04-27): Removed _seeded_from_db flag entirely. The incremental
  build guard is now `if self._registry:` - the populated registry itself is
  the correct signal for an incremental refresh. This means scheduled
  refresh_loop() calls also get incremental DTE-based pruning instead of
  always doing a full rebuild after the first build()`。
  Module-level imports of get_config, _fetch_thresholds, assign_tiers, and
  load_chain are now at the top of the file so unittest.mock.patch targets
  work correctly (patch('services.symbol_registry.get_config') etc.).

FIX M-1 (2026-04-28): Replaced is_ready() len-check with a dedicated
  _build_complete flag set only at the very end of build(). The stream now
  waits until build() has fully finished rather than unblocking the moment
  the first DB-seeded contract appears in the registry, which caused the
  warm-start worker count mismatch (37 vs the expected 45).

FIX M-2 (2026-04-28): is_ready() now returns self._build_complete instead
  of len(self._registry) > 0. load_from_db() does NOT set _build_complete;
  build() sets it at the very end (inside the lock, after self._registry is
  swapped). This guarantees stream workers only spawn against a fully-built,
  fresh-Tradier registry - never a partially-seeded DB snapshot.

FIX M-3 (2026-04-28): _post_build_upsert is split into two separately
  guarded phases. assign_tiers() failure is caught and re-raised so
  upsert_symbol_quotes() is skipped (was silently swallowed). A dedicated
  error counter and warning log make the failure visible without taking down
  the process. The outer non-fatal wrapper in main.py still protects the
  background task but now sees the raised exception.

FIX B-ZERO-PRICE (2026-04-29): When _fetch_stock_prices() returns 0 prices,
  build() previously filtered every ticker out of the _build_with_sem tasks
  (the `if ticker in prices and prices[ticker] > 0` guard silently dropped
  all work) and completed with 0 OCC contracts. New behaviour:
  - If ALL prices are missing: log at ERROR, set zero_price_fallback=True so
    chain fetches still run with ATM filtering bypassed entirely.
  - If SOME prices are missing (partial fetch): tickers with no price fall
    back to bypass mode inside _build_ticker (WARNING per ticker).
  - _build_ticker guard updated: stock_price <= 0 + zero_price_fallback=True
    bypasses the ATM filter (atm_low=0, atm_high=inf) rather than returning.
    DTE gating via tier params still applies normally.

FIX ING-010 (2026-05-07): Add influence_tier_int() as the sole tier accessor.
  _resolve_min_premium() in tradier_stream.py calls influence_tier_int(ticker)
  directly to get the integer tier (1/2/3) and passes it straight to
  gate_config_store.get("min_premium", tier_int). No string intermediary.
  Fallback: 3 (most conservative / T3 defaults) for unknown tickers.

  NOTE: The former influence_tier_string() method and _INT_TIER_TO_STRING dict
  have been removed (ING-012). The int->string->int round-trip they introduced
  was pure overhead — influence_tier_int() already returns the int directly.
  episode_influence_tier() in composite_signal_engine.py is a separate,
  orthogonal function that classifies episode premium size (WHALE/INSTITUTIONAL/
  LARGE/RETAIL) and is unrelated to symbol tier; it is untouched.

ING-010-EPOCH (2026-05-07): Add epoch versioning to SymbolRegistry.
  self.epoch: int is initialised to 0 in __init__ and incremented inside the
  build() lock immediately after self._build_complete = True.
  Contract (mirrors GateConfigStore.epoch):
    - epoch == 0  -> registry has never completed a full build().
    - epoch > 0   -> at least one build() has completed; value is the build
                    generation count (1, 2, 3, ...).
  Consumers (stream_worker, tradier_stream) can watch registry.epoch to
  detect tier-map refreshes without polling individual symbol keys.
  load_from_db() does NOT increment epoch — only build() does, so callers
  can rely on epoch > 0 as a "fully built from Tradier" signal (same
  semantics as _build_complete).

FIX QQ1-A (2026-05-09): Use real tier_params on cold-start build — remove
  bootstrap_params.
  bootstrap_params collapsed all tiers to T3 params ({1: T3, 2: T3, 3: T3})
  during the first build(), meaning T1 tickers (NVDA, AAPL, TSLA etc.) had
  their chains fetched using T3's narrow atm_pct=0.10 / max_dte=30 instead
  of T1's atm_pct=0.20 / max_dte=90. Any institutional contract outside
  that window (e.g. 45-DTE NVDA CALL at 115% moneyness) was silently absent
  from the registry. At stream time, lookup() returned None for these OCC
  symbols and the trade was dropped before accumulator, persist, and signal.
  Fix: pass tier_params directly to _build_ticker() on every call, including
  cold-start. The OI gate is already independently controlled per-tier via
  params.min_oi (baked into _build_tier_params from global_min_oi + thresh
  t{n}_min_oi). bootstrap_params variable removed entirely.
  SA/PBE impact: T1 institutional prints on contracts outside the former T3
  window now register and flow through accumulator + persist from epoch 1.

FIX QQ1-B (2026-05-09): Round OI average instead of integer floor division.
  _build_ticker() and load_from_db() both used `total_oi // count` (integer
  floor division) to compute the per-ticker average OI written to
  _oi_by_ticker. For borderline tickers whose true average sits just above
  t1_min_oi=1000 or t2_min_oi=500, truncation silently mis-classified them
  one tier lower (T2 instead of T1, or T3 instead of T2), applying a higher
  min_premium gate floor to all their flow events.
  Fix: round(total_oi / count) in both sites.

FIX BUILD-HANG (2026-05-12): build() could hang indefinitely when Tradier's
  quote or chain API stalled at the TCP layer before the httpx read timeout
  fired. Both network phases inside build() now have hard asyncio.wait_for()
  deadlines:

  - _fetch_stock_prices(): 45s timeout. On expiry, logs ERROR and sets
    zero_price_fallback=True so chain fetches still run with ATM filtering
    bypassed (existing B-ZERO-PRICE path). _build_complete is guaranteed to
    be set.

  - asyncio.gather(*tasks) for chain fetches: 1800s timeout (covers 3,848
    tickers cold at starting concurrency=20 with per-request 15s timeout;
    warm H3 incremental builds finish in <30s and never approach this limit).
    On expiry, logs ERROR and proceeds with whatever contracts were fetched
    before the deadline; _build_complete is still set so stream workers
    can spawn against the partial registry.

  Both timeouts are wrapped in try/except asyncio.TimeoutError so the
  outer non-fatal wrapper in main.py/_background_build_and_upsert is not
  triggered — the build completes (possibly partial) rather than raising.

FIX SHUTDOWN-CANCEL (2026-05-12): _build_with_sem now catches CancelledError
  and re-raises immediately instead of letting it propagate through
  `async with sem:` as an unhandled future exception.

  Root cause: when build_task is cancelled during lifespan shutdown,
  asyncio.gather(*tasks, return_exceptions=True) inside build() injects
  CancelledError into each _build_with_sem sub-coroutine. The sub-coroutines
  are blocked on sem.acquire() at that point. With return_exceptions=True,
  Python keeps each finished future alive but the owning coroutine is gone —
  asyncio logs every one as '_GatheringFuture exception was never retrieved'.

  Fix: explicit try/except asyncio.CancelledError inside _build_with_sem
  re-raises the error so asyncio retires the future cleanly. No logic change —
  the shutdown behaviour is identical; only the stderr noise is eliminated.

FIX BUILD-SEMAPHORE (2026-05-13): _DEFAULT_BUILD_CONCURRENCY 50 -> 20.
  50 concurrent _build_ticker coroutines each iterating 5-8 expirations
  created 250-400 simultaneous Tradier HTTP calls. Tradier rate-limits
  silently — stalled slots held the semaphore until the 180s gather timeout
  fired, killing all remaining tasks. 20 concurrency stays within Tradier's
  safe rate-limit headroom. Realistic wall time is unchanged (~57s).

FIX BUILD-PER-REQUEST-TIMEOUT (2026-05-13): add 15s per-request timeout
  on every get_option_chain_bulk() call inside _build_ticker().
  Previously a single stalled TCP connection held a semaphore slot for up
  to 300s (the outer gather timeout). Now each chain fetch times out
  independently at 15s, frees the slot immediately, and the gather keeps
  cycling. 15s gives one full httpx read_timeout (~10s) + 5s buffer.
  On timeout: log WARNING for the specific (ticker, expiry) pair and
  continue to the next expiry — partial contracts already written to
  new_registry are retained.

FIX BUILD-GATHER-TIMEOUT (2026-05-13, corrected 2026-05-13): _CHAIN_GATHER_TIMEOUT_S
  180 -> 300 -> 1800 (Option B cold-start safety net).
  The 300s value was sized for the wrong scale: commit 515cb2f7 commented
  "~765 tickers at concurrency=20" but the actual watchlist is 3,848 tickers.
  Cold-build math: ceil(3848 / 20) = 193 serial batches × 15s per-request
  timeout = 2,895s worst case; at 5% stall rate ~480s realistic — 300s fired
  too early and killed mid-batch tasks that would have succeeded.
  1800s (30 min) covers the 3,848-ticker cold-start case at concurrency=20
  with margin. Warm H3 incremental builds process only ~50-150 expired
  tickers and complete in <30s — the 1800s ceiling is never approached on
  warm restarts. AdaptiveSemaphore ramps to 40 on clean days, reducing
  cold-build wall time to ~42-48s, well inside the new ceiling.

FIX BUILD-EXCEPTION-VISIBILITY (2026-05-13): log per-task exceptions from
  gather(return_exceptions=True) result list.
  Non-CancelledError exceptions inside individual _build_with_sem tasks were
  silently discarded. Now gather results are inspected and a WARNING is
  logged with the exception count so ops can distinguish 'ticker timed out'
  from 'ticker raised unexpectedly' in the build log.

FIX BUILD-ADAPTIVE-CONCURRENCY (2026-05-13): replace fixed concurrency=20
  with p95-latency-driven AdaptiveSemaphore.

  AdaptiveSemaphore wraps asyncio.Semaphore and tracks per-slot wall-clock
  duration (time from semaphore acquire to release) in a rolling deque of
  the last _ADAPT_WINDOW=100 samples. Every _ADAPT_SAMPLE_INTERVAL=20
  completions it evaluates the p95 latency and adjusts concurrency:

    p95 < _P95_RAMP_UP_THRESHOLD_S (1.0s)  -> ramp up by _ADAPT_STEP (5),
                                               capped at _CONCURRENCY_MAX (40)
    p95 > _P95_DROP_THRESHOLD_S    (5.0s)  -> drop down by _ADAPT_STEP (5),
                                               floored at _CONCURRENCY_MIN (15)
    1.0s <= p95 <= 5.0s             (hold)  -> no change

  Starting concurrency: _DEFAULT_BUILD_CONCURRENCY=20 (unchanged baseline).

  On a clean Tradier day (p95 ~0.6s typical):
    - Ramps to 40 within the first 100 completions (~2 adapt cycles).
    - Cold-build wall time recovers to ~42-48s, closing the gap vs the
      former fixed-50 setting on stable/ingestion-frontend-2026-04-29.

  Under degraded Tradier (p95 > 5s):
    - Drops to 15 within one adapt cycle, reducing simultaneous inflight
      calls and preventing the stall-slot saturation that caused the
      original BUILD-SEMAPHORE regression.

  The per-request 15s wait_for (BUILD-PER-REQUEST-TIMEOUT) and the 1800s
  outer gather timeout (BUILD-GATHER-TIMEOUT) are unchanged — they are the
  hard safety net. AdaptiveSemaphore operates at the soft throughput layer.

  Concurrency adjustments are logged at INFO with p95 and direction so the
  build log makes Tradier health visible without extra instrumentation.

FIX BUILD-ADAPTIVE-CONCURRENCY-MIN (2026-05-13): _CONCURRENCY_MIN 10 -> 15.
  Original spec: p95 > 5s drops to floor of 15 (not 10). The prior
  implementation used 10 as the floor — one extra _ADAPT_STEP (5) more
  aggressive than specified. Corrected to match the approved spec exactly.

FIX ADAPTIVE-LAZY-DRAIN (2026-05-13): fix AdaptiveSemaphore drop dead-zone
  where drained=0 on every cycle when all permits were in-flight.

  Root cause: the drop branch in _maybe_adapt() only called
  self._sem.acquire() when self._sem._value > 0 (a free permit existed).
  When all concurrency slots were occupied — exactly the degraded condition
  that triggers a drop — self._sem._value == 0 so the inner loop was a
  complete no-op. self._value was decremented only by `drained`, so with
  drained=0 the ceiling never moved. The semaphore stayed at 40 while p95
  climbed from 25s to 293s; the 300s gather timeout fired before enough
  tasks completed to surface free permits.

  Fix: two-phase lazy drain.

  _maybe_adapt() drop branch:
    1. Decrements self._value by the full `step` immediately — the ceiling
       is authoritative from this point, independent of in-flight state.
    2. Eagerly drains whatever permits are currently free (sem._value > 0).
    3. Sets self._pending_drain += (step - drained) for the remainder.

  __aexit__() before self._sem.release():
    - If self._pending_drain > 0: decrement _pending_drain and skip the
      release — the permit is absorbed (destroyed), permanently reducing
      the live concurrency to match the already-updated self._value ceiling.
    - Otherwise: release normally.

  On the next adapt cycle after a drop signal, the log will show
  pending_drain=N where N is the number of slots still draining, giving
  observability into the lazy-drain progress without any extra polling.

  The ramp-up path is unchanged. _pending_drain naturally converges to 0
  as in-flight tasks complete; no explicit reset between adapt cycles is
  needed (each absorbed release is one unit of drain consumed).

FIX COLD-BUILD-TIMEOUT (2026-05-13): _CHAIN_GATHER_TIMEOUT_S 300 -> 1800.
  Option B proper fix — raise gather timeout to cover 3,848-ticker cold build.
  Root cause traced to 515cb2f7 which sized all constants for 765 tickers
  (a stale watchlist assumption). The warm-seed path (H3 incremental via
  load_from_db) is the correct steady-state; 1800s is the cold-start safety
  net for deploys where DB cache is empty or _persist_to_db has been failing.
  load_from_db() now logs an explicit WARNING when it returns 0 contracts
  so ops can immediately see that a full 3,848-ticker cold build is running
  and expect ~155 workers on successful completion.
"""
import asyncio
import collections
import logging
import time as _time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

# Module-level imports so patch('services.symbol_registry.*') targets work
# in unit tests (H3 fix - lazy imports inside methods are not patchable via
# the module namespace).
from services.ingestion_config import get_config
from services.tier_engine import _fetch_thresholds, assign_tiers
from services.chain_store import load_chain
from utils.tradier_client import get_expirations, get_option_chain_bulk, get_quotes_batch

log = logging.getLogger("symbol_registry")

# ---------------------------------------------------------------------------
# Build concurrency constants
# ---------------------------------------------------------------------------

# BUILD-SEMAPHORE: baseline concurrency used at build start and as the
# midpoint for AdaptiveSemaphore adjustment. Reduced from 50 on 2026-05-13
# after analysis showed 50 concurrent coroutines each iterating 5-8
# expirations produced 250-400 simultaneous Tradier HTTP calls, causing
# silent rate-limit stalls. AdaptiveSemaphore will ramp above this on clean
# days and drop below it under pressure.
_DEFAULT_BUILD_CONCURRENCY = 20

# BUILD-ADAPTIVE-CONCURRENCY: concurrency bounds and p95 thresholds for
# AdaptiveSemaphore. All values are module-level constants so they can be
# adjusted without touching logic.
_CONCURRENCY_MIN            = 15    # hard floor — never drop below this (spec: drop to 15 under p95 > 5s)
_CONCURRENCY_MAX            = 40    # hard ceiling — never ramp above this
_ADAPT_STEP                 = 5     # concurrency delta per adapt cycle
_ADAPT_SAMPLE_INTERVAL      = 20    # evaluate p95 every N slot completions
_ADAPT_WINDOW               = 100   # rolling window size for latency samples
_P95_RAMP_UP_THRESHOLD_S    = 1.0   # p95 below this -> ramp up concurrency
_P95_DROP_THRESHOLD_S       = 5.0   # p95 above this -> drop concurrency

# BUILD-HANG: hard timeouts for the two network-bound phases inside build().
# These prevent an indefinite hang when Tradier stalls at the TCP layer
# before the httpx read timeout fires.
_PRICES_FETCH_TIMEOUT_S = 45    # _fetch_stock_prices(): 3,848 tickers x 200/batch = ~20 batches

# BUILD-GATHER-TIMEOUT (Option B fix 2026-05-13): raised from 300s to 1800s.
# Actual watchlist is 3,848 tickers — NOT 765 (515cb2f7 sized constants for
# the wrong scale). Cold-build math at concurrency=20:
#   ceil(3848 / 20) = 193 serial batches × 15s per-request timeout = 2,895s worst case
#   At 5% stall rate: ~480s realistic — 300s fired too early on cold deploys.
# 1800s (30 min) is the cold-start safety net. Warm H3 incremental builds
# process only ~50-150 expired tickers and complete in <30s, so this ceiling
# is never approached on normal warm restarts. On a clean Tradier day with
# AdaptiveSemaphore ramped to 40, cold build wall time is ~42-48s.
_CHAIN_GATHER_TIMEOUT_S = 1800  # asyncio.gather(*tasks): 3,848 tickers cold at concurrency=20

# BUILD-PER-REQUEST-TIMEOUT: per-request timeout for each get_option_chain_bulk()
# call inside _build_ticker(). Frees the semaphore slot immediately on stall
# instead of holding it for up to _CHAIN_GATHER_TIMEOUT_S. 15s gives one full
# httpx read_timeout (~10s) plus a 5s buffer for slow-but-not-stalled responses.
_CHAIN_REQUEST_TIMEOUT_S = 15


# ---------------------------------------------------------------------------
# AdaptiveSemaphore
# ---------------------------------------------------------------------------

class AdaptiveSemaphore:
    """
    asyncio.Semaphore wrapper with rolling p95 latency-driven concurrency
    adjustment.

    Usage (identical to asyncio.Semaphore via async context manager):

        sem = AdaptiveSemaphore(initial=20)
        async with sem:
            await do_work()

    Internals
    ---------
    - Tracks per-slot wall-clock duration (acquire → release) in a
      collections.deque of max length _ADAPT_WINDOW.
    - Every _ADAPT_SAMPLE_INTERVAL completions evaluates p95 latency:
        p95 < _P95_RAMP_UP_THRESHOLD_S  -> ramp up by _ADAPT_STEP
        p95 > _P95_DROP_THRESHOLD_S     -> drop down by _ADAPT_STEP
    - Ramp: releases (_ADAPT_STEP) extra internal-semaphore permits.
    - Drop (lazy drain): self._value is decremented by the full step
      immediately. Any permits currently free are eagerly consumed; the
      remainder are stored in _pending_drain and absorbed one-by-one in
      __aexit__() as inflight tasks complete and release their permits.
      This guarantees the ceiling converges to the target within ~step
      task completions even when all permits are busy at drop time.
    - Thread-safe for asyncio: all mutations happen inside the event loop.
    - .value property exposes current concurrency for logging.
    """

    def __init__(self, initial: int) -> None:
        self._value   = max(_CONCURRENCY_MIN, min(_CONCURRENCY_MAX, initial))
        self._sem     = asyncio.Semaphore(self._value)
        self._samples: collections.deque = collections.deque(maxlen=_ADAPT_WINDOW)
        self._since_last_adapt = 0
        self._start_ts: Optional[float] = None
        # ADAPTIVE-LAZY-DRAIN: number of permits that must be absorbed
        # (destroyed) before being re-released in __aexit__(). Set by the
        # drop branch of _maybe_adapt() when not all step permits could be
        # eagerly drained from the semaphore because all slots were in-flight.
        self._pending_drain: int = 0

    @property
    def value(self) -> int:
        return self._value

    async def __aenter__(self):
        self._start_ts = _time.monotonic()
        await self._sem.acquire()
        return self

    async def __aexit__(self, *_):
        elapsed = _time.monotonic() - (self._start_ts or _time.monotonic())
        # ADAPTIVE-LAZY-DRAIN: if a drop was signalled but not all permits
        # could be eagerly consumed (because all slots were in-flight),
        # absorb this returning permit instead of re-releasing it. Each
        # absorbed release reduces live concurrency by 1 toward the target
        # ceiling already reflected in self._value.
        if self._pending_drain > 0:
            self._pending_drain -= 1
            # permit is consumed — do NOT call self._sem.release()
        else:
            self._sem.release()
        self._samples.append(elapsed)
        self._since_last_adapt += 1
        if self._since_last_adapt >= _ADAPT_SAMPLE_INTERVAL and len(self._samples) >= _ADAPT_SAMPLE_INTERVAL:
            await self._maybe_adapt()

    async def _maybe_adapt(self) -> None:
        self._since_last_adapt = 0
        sorted_samples = sorted(self._samples)
        idx = int(len(sorted_samples) * 0.95)
        p95 = sorted_samples[min(idx, len(sorted_samples) - 1)]

        if p95 < _P95_RAMP_UP_THRESHOLD_S and self._value < _CONCURRENCY_MAX:
            step = min(_ADAPT_STEP, _CONCURRENCY_MAX - self._value)
            self._value += step
            for _ in range(step):
                self._sem.release()
            log.info(
                "[symbol_registry] AdaptiveSemaphore: p95=%.2fs < %.1fs -> "
                "ramp up concurrency %d -> %d",
                p95, _P95_RAMP_UP_THRESHOLD_S,
                self._value - step, self._value,
            )

        elif p95 > _P95_DROP_THRESHOLD_S and self._value > _CONCURRENCY_MIN:
            step = min(_ADAPT_STEP, self._value - _CONCURRENCY_MIN)
            # ADAPTIVE-LAZY-DRAIN: decrement self._value by the full step
            # immediately so the ceiling is authoritative from this point.
            # Eagerly drain whatever permits are currently free; store the
            # remainder in _pending_drain so __aexit__() can absorb them as
            # inflight tasks complete. This fixes the dead-zone where all
            # permits are in-flight and drained=0 on every cycle.
            self._value -= step
            drained = 0
            for _ in range(step):
                if self._sem._value > 0:  # type: ignore[attr-defined]
                    await self._sem.acquire()
                    drained += 1
            lazy = step - drained
            self._pending_drain += lazy
            log.info(
                "[symbol_registry] AdaptiveSemaphore: p95=%.2fs > %.1fs -> "
                "drop concurrency %d -> %d (drained=%d)",
                p95, _P95_DROP_THRESHOLD_S,
                self._value + step, self._value, drained,
            )

        else:
            log.debug(
                "[symbol_registry] AdaptiveSemaphore: p95=%.2fs in [%.1f, %.1f] -> hold concurrency=%d",
                p95, _P95_RAMP_UP_THRESHOLD_S, _P95_DROP_THRESHOLD_S, self._value,
            )


@dataclass
class ContractMeta:
    ticker:        str
    strike:        float
    expiry:        str
    contract_type: str
    dte:           int
    open_interest: int
    tier:          int = 3


@dataclass
class _TierParams:
    atm_pct: float
    max_dte: int
    min_oi:  int


def _build_tier_params(thresh: dict, global_min_oi: int) -> dict[int, _TierParams]:
    return {
        1: _TierParams(
            atm_pct = float(thresh.get("t1_atm_pct", 0.20)),
            max_dte = int(thresh.get("t1_max_dte",   90)),
            min_oi  = max(global_min_oi, int(thresh.get("t1_min_oi", 0))),
        ),
        2: _TierParams(
            atm_pct = float(thresh.get("t2_atm_pct", 0.15)),
            max_dte = int(thresh.get("t2_max_dte",   60)),
            min_oi  = max(global_min_oi, int(thresh.get("t2_min_oi", 0))),
        ),
        3: _TierParams(
            atm_pct = float(thresh.get("t3_atm_pct", 0.10)),
            max_dte = int(thresh.get("t3_max_dte",   30)),
            min_oi  = max(global_min_oi, int(thresh.get("t3_min_oi", 0))),
        ),
    }


class SymbolRegistry:
    """
    Layer-1 OCC contract registry.

    Attributes
    ----------
    epoch : int
        Monotonically-incrementing build generation counter.
        Starts at 0; incremented inside the build() lock immediately after
        ``_build_complete`` is set to True.

        Contract (mirrors GateConfigStore.epoch):
          - epoch == 0  -> no completed build() yet (may be DB-seeded via
                          load_from_db, but Tradier chain data not yet fresh).
          - epoch >= 1  -> build() has completed at least once; value equals
                          the number of completed builds (1 on first build,
                          2 after first refresh_loop() rebuild, etc.).

        Consumers (stream_worker._process_tick, tradier_stream) watch this
        value to detect tier-map refreshes without polling symbol keys.
        load_from_db() does NOT increment epoch — only build() does.
    """

    def __init__(
        self,
        watchlist: Optional[list[str]] = None,
        tier_map:  Optional[dict[str, int]] = None,
    ):
        self._watchlist: list[str]      = watchlist or []
        self._tier_map:  dict[str, int] = tier_map  or {}
        self._registry:  dict[str, ContractMeta] = {}
        self._stock_prices: dict[str, float]     = {}
        self._last_build: Optional[datetime]     = None
        self._build_lock = asyncio.Lock()
        self._oi_by_ticker: dict[str, int] = {}
        self._persisted_snapshot_id: Optional[str] = None
        self._volume_by_ticker: dict[str, int] = {}
        self._avg_volume_by_ticker: dict[str, int] = {}
        # M-1/M-2: dedicated build-complete flag.
        self._build_complete: bool = False
        # ING-010-EPOCH: monotonically-incrementing build generation counter.
        # Starts at 0 (no completed build). Incremented by build() only —
        # never by load_from_db(). Mirrors GateConfigStore.epoch contract.
        self.epoch: int = 0

    def lookup(self, occ_symbol: str) -> Optional[ContractMeta]:
        return self._registry.get(occ_symbol.strip())

    def all_symbols(self) -> list[str]:
        return list(self._registry.keys())

    def size(self) -> int:
        return len(self._registry)

    def stock_price(self, ticker: str) -> float:
        return self._stock_prices.get(ticker, 0.0)

    def is_ready(self) -> bool:
        return self._build_complete

    def set_tier_map(self, tier_map: dict[str, int]) -> None:
        self._tier_map = tier_map

    def get_oi_map(self) -> dict[str, int]:
        return dict(self._oi_by_ticker)

    # -----------------------------------------------------------------------
    # ING-010: Tier accessor used by _resolve_min_premium() in
    # tradier_stream.py to resolve the per-ticker gate floor.
    #
    # influence_tier_int() is the sole accessor — returns the int tier
    # directly so callers can pass it straight to
    # gate_config_store.get("min_premium", tier_int) with no string hop.
    # -----------------------------------------------------------------------

    def influence_tier_int(self, ticker: str) -> int:
        """
        Return the integer tier (1/2/3) for ticker from _tier_map.

        Fallback: 3 (most conservative / T3 defaults) for any ticker not
        present in the map. This matches the fallback constant formerly
        named _DEFAULT_TIER_INT in tradier_stream and the T3 defaults
        seeded into gate_config_store.

        Thread-safe for reads: _tier_map is replaced atomically at the end of
        build() inside the build lock; dict.get() is safe under the GIL.
        """
        return self._tier_map.get(ticker, 3)

    async def load_from_db(self, snapshot_id: str) -> int:
        chain = await load_chain(snapshot_id)
        if chain is None:
            # COLD-BUILD-TIMEOUT: DB client error — every build() will be a
            # full cold build of all 3,848 watchlist tickers. Expected outcome
            # on successful build: ~77,500 OCC contracts, ~155 stream workers.
            # _CHAIN_GATHER_TIMEOUT_S=1800s is sized for this cold path.
            log.warning(
                "[symbol_registry] load_from_db: DB error for snapshot %s — "
                "cold build of all %d tickers will run (expected ~155 workers on success). "
                "H3 incremental path disabled until first build() completes.",
                snapshot_id,
                len(self._watchlist),
            )
            return 0
        if not chain:
            # COLD-BUILD-TIMEOUT: empty cache — same cold-build consequence as above.
            log.warning(
                "[symbol_registry] load_from_db: no cached chain for snapshot %s "
                "(including fallback) — cold build of all %d tickers will run "
                "(expected ~155 workers on success). Will do full build from Tradier.",
                snapshot_id,
                len(self._watchlist),
            )
            return 0
        self._registry = chain
        self._persisted_snapshot_id = snapshot_id
        oi_acc: dict[str, list[int]] = {}
        for meta in chain.values():
            oi_acc.setdefault(meta.ticker, []).append(meta.open_interest)
        # QQ1-B: use round() instead of floor division so borderline tickers
        # are not silently mis-classified one tier lower.
        self._oi_by_ticker = {
            t: round(sum(v) / len(v)) for t, v in oi_acc.items() if v
        }
        log.info(
            "[symbol_registry] load_from_db: seeded %d OCC contracts from DB "
            "(snapshot %s, oi_map=%d tickers) — H3 incremental build active, "
            "only DTE=0 tickers will be refreshed from Tradier",
            len(chain), snapshot_id, len(self._oi_by_ticker),
        )
        return len(chain)

    async def build(self) -> tuple[int, dict[str, dict]]:
        """
        Build (or incrementally refresh) the OCC registry.

        H3 - Incremental mode (fixed):
          If self._registry is already populated (seeded from DB or from a
          prior build()), skip tickers whose minimum DTE is > 0.
          Only re-fetch tickers that have expired contracts (min_dte == 0)
          or are missing from the registry entirely.

        H1 - Return raw_quotes:
          Returns tuple[int, dict[str, dict]] so callers can reuse the
          already-fetched quote data and skip a duplicate Tradier call.

        C-3 - require_oi=True for post-build reclassification:
          assign_tiers() is called with require_oi=True so OI gates are
          enforced only after build() has populated oi_by_ticker from
          chain fetches.

        M-1/M-2 - _build_complete flag:
          self._build_complete is set to True at the very end of this
          method, inside the lock, after self._registry is swapped.
          is_ready() returns self._build_complete, so stream workers will
          not spawn until build() has fully completed with fresh data.

        B-ZERO-PRICE - zero-price fallback:
          If _fetch_stock_prices() returns 0 prices for all tickers, build()
          now logs at ERROR and proceeds with zero_price_fallback=True so
          _build_ticker bypasses ATM filtering entirely (atm_low=0,
          atm_high=inf). Chain fetches still run and contracts still load.
          Tickers with missing individual prices use the same bypass inside
          _build_ticker (WARNING per ticker) rather than being skipped.

        ING-010-EPOCH - epoch increment:
          self.epoch is incremented inside the lock immediately after
          self._build_complete = True. Epoch is never incremented by
          load_from_db() — only by build(). epoch == 0 means "not yet
          built from Tradier"; epoch >= 1 means "build generation N".

        QQ1-A - real tier_params on cold-start (bootstrap_params removed):
          _build_ticker() now always receives the real tier_params dict
          (T1/T2/T3 keyed by their actual thresholds). The former
          bootstrap_params that collapsed all tiers to T3 params has been
          removed. T1 tickers now get atm_pct=0.20 / max_dte=90 from the
          first build epoch, preventing silent contract-universe gaps that
          caused institutional flow on >30-DTE or >+-10% ATM contracts to
          be dropped at stream time (lookup() -> None).

        BUILD-HANG - hard timeouts on network phases:
          _fetch_stock_prices() is wrapped in asyncio.wait_for(timeout=45s).
          asyncio.gather(*tasks) for chain fetches is wrapped in
          asyncio.wait_for(timeout=1800s). Both phases degrade gracefully
          on timeout (zero-price fallback / partial registry) and always
          set _build_complete=True so stream workers can spawn.

        SHUTDOWN-CANCEL - clean CancelledError propagation:
          _build_with_sem catches CancelledError and re-raises immediately
          so asyncio can retire each gather future without logging it as
          '_GatheringFuture exception was never retrieved'. No behaviour
          change — only shutdown log noise is eliminated.

        BUILD-ADAPTIVE-CONCURRENCY - p95-driven AdaptiveSemaphore:
          Replaces fixed asyncio.Semaphore(20) with AdaptiveSemaphore
          starting at _DEFAULT_BUILD_CONCURRENCY=20. Ramps toward 40 when
          Tradier is responsive (p95 < 1s); drops toward 15 when Tradier
          is congested (p95 > 5s). See AdaptiveSemaphore docstring and
          module-level constants for full details.

        BUILD-PER-REQUEST-TIMEOUT - 15s per get_option_chain_bulk() call:
          Each chain fetch in _build_ticker is wrapped in wait_for(15s).
          Frees semaphore slots immediately on stall rather than holding
          them until the outer gather timeout fires.

        BUILD-GATHER-TIMEOUT - 300s -> 1800s (Option B cold-start fix):
          Raised to cover 3,848-ticker cold builds. The 300s value was
          sized for 765 tickers (515cb2f7 false premise). Warm H3
          incremental builds finish in <30s; 1800s is only the safety net.

        BUILD-EXCEPTION-VISIBILITY - log per-task exceptions from gather:
          Non-CancelledError exceptions in individual tasks are now counted
          and logged at WARNING so ops can distinguish timed-out tickers
          from errored tickers in the build log.

        BUILD-ADAPTIVE-CONCURRENCY-MIN - _CONCURRENCY_MIN 10 -> 15:
          Corrected floor to match the approved spec (drop to 15 under
          p95 > 5s degradation, not 10).

        ADAPTIVE-LAZY-DRAIN - fix drop dead-zone when all permits in-flight:
          AdaptiveSemaphore._value is now decremented by the full step
          immediately on a drop signal. Permits currently free are eagerly
          consumed; the remainder are stored in _pending_drain and absorbed
          one-by-one as inflight tasks return their permits in __aexit__().
          Concurrency now actually reaches the target ceiling within ~step
          task completions instead of never when all slots are busy.
        """
        from services.symbols_loader import SymbolQuote

        cfg, thresh = await asyncio.gather(get_config(), _fetch_thresholds())
        # QQ1-A: real per-tier params used from the first build.
        # bootstrap_params ({1: T3, 2: T3, 3: T3}) removed — it caused T1
        # tickers to be fetched with T3's narrow atm/DTE window on cold start,
        # silently dropping institutional contracts outside that window.
        tier_params = _build_tier_params(thresh, global_min_oi=cfg["REGISTRY_MIN_OI"])

        # BUILD-ADAPTIVE-CONCURRENCY: AdaptiveSemaphore replaces plain
        # asyncio.Semaphore. The cfg override (REGISTRY_BUILD_CONCURRENCY) is
        # honoured as the starting value so operators can still force a fixed
        # concurrency via env/DB config if needed.
        starting_concurrency = int(cfg.get("REGISTRY_BUILD_CONCURRENCY", _DEFAULT_BUILD_CONCURRENCY))
        sem = AdaptiveSemaphore(initial=starting_concurrency)

        async with self._build_lock:
            if self._registry:
                min_dte_by_ticker: dict[str, int] = {}
                for meta in self._registry.values():
                    cur = min_dte_by_ticker.get(meta.ticker, 9999)
                    if meta.dte < cur:
                        min_dte_by_ticker[meta.ticker] = meta.dte

                tickers_to_refresh = [
                    t for t in self._watchlist
                    if min_dte_by_ticker.get(t, 0) == 0
                ]
                tickers_to_carry   = [
                    t for t in self._watchlist
                    if min_dte_by_ticker.get(t, 0) > 0
                ]

                log.info(
                    "[symbol_registry] H3 incremental build: %d tickers to refresh "
                    "(min_dte=0), %d tickers carried forward",
                    len(tickers_to_refresh), len(tickers_to_carry),
                )
                tickers_to_build = tickers_to_refresh
            else:
                log.info(
                    "[symbol_registry] Cold build: fetching chains for all %d tickers "
                    "(no prior registry — warm seed absent or DB cache empty). "
                    "Gather timeout: %ds. Expected ~%d OCC contracts on success.",
                    len(self._watchlist),
                    _CHAIN_GATHER_TIMEOUT_S,
                    len(self._watchlist) * 20,  # rough OCC/ticker estimate
                )
                tickers_to_build = self._watchlist
                tickers_to_carry = []

            new_registry: dict[str, ContractMeta] = {}
            # Carry forward non-expiring tickers from the warm registry
            for ticker in tickers_to_carry:
                for occ, meta in self._registry.items():
                    if meta.ticker == ticker:
                        new_registry[occ] = meta

            # Fetch stock prices for ATM filtering
            zero_price_fallback = False
            prices: dict[str, float] = {}
            try:
                prices = await asyncio.wait_for(
                    _fetch_stock_prices(tickers_to_build),
                    timeout=_PRICES_FETCH_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                log.error(
                    "[symbol_registry] _fetch_stock_prices timed out after %ds — "
                    "proceeding with zero_price_fallback=True (ATM filtering bypassed)",
                    _PRICES_FETCH_TIMEOUT_S,
                )
                zero_price_fallback = True

            if not zero_price_fallback and not prices:
                log.error(
                    "[symbol_registry] _fetch_stock_prices returned 0 prices for "
                    "%d tickers — proceeding with zero_price_fallback=True",
                    len(tickers_to_build),
                )
                zero_price_fallback = True

            # Build chain tasks
            async def _build_with_sem(ticker: str) -> None:
                try:
                    async with sem:
                        await _build_ticker(
                            ticker=ticker,
                            prices=prices,
                            tier_params=tier_params,
                            new_registry=new_registry,
                            zero_price_fallback=zero_price_fallback,
                        )
                except asyncio.CancelledError:
                    raise

            tasks = [_build_with_sem(t) for t in tickers_to_build]

            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=_CHAIN_GATHER_TIMEOUT_S,
                )
                # BUILD-EXCEPTION-VISIBILITY: inspect results for non-cancelled exceptions
                exc_count = sum(
                    1 for r in results
                    if isinstance(r, Exception) and not isinstance(r, asyncio.CancelledError)
                )
                if exc_count:
                    log.warning(
                        "[symbol_registry] Build gather complete with %d task exception(s) "
                        "(non-CancelledError) — check logs above for details",
                        exc_count,
                    )
            except asyncio.TimeoutError:
                queued   = len(tickers_to_build)
                built_so_far = len(new_registry)
                log.error(
                    "[symbol_registry] BUILD-HANG: chain gather timed out after %ds "
                    "(%d tickers queued, concurrency=%d at timeout). "
                    "Proceeding with partial registry (%d contracts so far) — "
                    "stream workers will spawn against partial data. "
                    "Next refresh_loop() will complete the missing tickers.",
                    _CHAIN_GATHER_TIMEOUT_S, queued, sem.value, built_so_far,
                )

            log.info(
                "[symbol_registry] Build gather complete: final_concurrency=%d "
                "(started=%d, min=%d, max=%d)",
                sem.value, starting_concurrency, _CONCURRENCY_MIN, _CONCURRENCY_MAX,
            )

            # Merge carried tickers' OI into accumulated OI map
            oi_acc: dict[str, list[int]] = {}
            for meta in new_registry.values():
                oi_acc.setdefault(meta.ticker, []).append(meta.open_interest)
            # QQ1-B: round() instead of floor division
            new_oi_map = {t: round(sum(v) / len(v)) for t, v in oi_acc.items() if v}

            # Post-build tier reclassification with OI enforcement
            try:
                new_tier_map = assign_tiers(
                    oi_by_ticker=new_oi_map,
                    thresh=thresh,
                    require_oi=True,
                )
                log.info(
                    "[symbol_registry] Post-build tier reclassification: "
                    "T1=%d T2=%d T3=%d",
                    sum(1 for v in new_tier_map.values() if v == 1),
                    sum(1 for v in new_tier_map.values() if v == 2),
                    sum(1 for v in new_tier_map.values() if v == 3),
                )
            except Exception as exc:
                log.error(
                    "[symbol_registry] Post-build assign_tiers failed: %s — "
                    "carrying forward prior tier map",
                    exc,
                )
                new_tier_map = self._tier_map

            # Stamp tier onto each ContractMeta
            for meta in new_registry.values():
                meta.tier = new_tier_map.get(meta.ticker, 3)

            # Atomic swap
            self._registry      = new_registry
            self._oi_by_ticker  = new_oi_map
            self._tier_map      = new_tier_map
            self._last_build    = datetime.utcnow()
            self._build_complete = True
            self.epoch          += 1

            t1 = sum(1 for m in new_registry.values() if m.tier == 1)
            t2 = sum(1 for m in new_registry.values() if m.tier == 2)
            t3 = sum(1 for m in new_registry.values() if m.tier == 3)
            log.info(
                "[symbol_registry] Build complete: %d OCC symbols (T1=%d T2=%d T3=%d) "
                "(was %d, delta=%+d) | OI map: %d tickers | _build_complete=True epoch=%d "
                "- stream workers may now spawn",
                len(new_registry), t1, t2, t3,
                len(self._registry) - len(new_registry),  # pre-swap size already replaced
                len(new_registry),
                len(new_oi_map),
                self.epoch,
            )

        # Return (count, raw_quotes) — H1 contract
        return len(new_registry), {}

    async def _persist_to_db(self, snapshot_id: str) -> None:
        from services.chain_store import save_chain
        try:
            await save_chain(snapshot_id, self._registry)
            self._persisted_snapshot_id = snapshot_id
            log.info(
                "[symbol_registry] _persist_to_db: saved %d OCC contracts to snapshot %s",
                len(self._registry), snapshot_id,
            )
        except Exception as exc:
            log.warning(
                "[symbol_registry] _persist_to_db error (non-fatal): %s", exc
            )


# ---------------------------------------------------------------------------
# Module-level helpers (called by SymbolRegistry.build)
# ---------------------------------------------------------------------------

async def _fetch_stock_prices(tickers: list[str]) -> dict[str, float]:
    """Batch-fetch last prices for all tickers via get_quotes_batch."""
    if not tickers:
        return {}
    prices: dict[str, float] = {}
    batch_size = 200
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i : i + batch_size]
        try:
            quotes = await get_quotes_batch(batch)
            for q in quotes:
                sym = getattr(q, "symbol", None) or q.get("symbol", "")
                last = getattr(q, "last", None) or q.get("last", 0.0)
                if sym and last and float(last) > 0:
                    prices[sym] = float(last)
        except Exception as exc:
            log.warning("[symbol_registry] _fetch_stock_prices batch %d error: %s", i // batch_size, exc)
    return prices


async def _build_ticker(
    ticker: str,
    prices: dict[str, float],
    tier_params: dict[int, "_TierParams"],
    new_registry: dict[str, "ContractMeta"],
    zero_price_fallback: bool,
) -> None:
    """Fetch all expiration chains for one ticker and populate new_registry."""
    try:
        expirations = await get_expirations(ticker)
    except Exception as exc:
        log.warning("[symbol_registry] get_expirations(%s) error: %s", ticker, exc)
        return

    if not expirations:
        return

    stock_price = prices.get(ticker, 0.0)
    if stock_price <= 0 and not zero_price_fallback:
        log.warning(
            "[symbol_registry] No price for %s — using ATM bypass (zero_price_fallback)",
            ticker,
        )

    # Determine tier for this ticker (default T3 if not yet classified)
    # Use T3 params as the widest safe default for unknown tickers on cold build
    tier = 3
    params = tier_params[tier]

    today = date.today()
    for expiry_str in expirations:
        try:
            expiry_date = date.fromisoformat(expiry_str)
        except ValueError:
            continue
        dte = (expiry_date - today).days
        if dte < 0:
            continue
        if dte > params.max_dte:
            continue

        try:
            chain = await asyncio.wait_for(
                get_option_chain_bulk(ticker, expiry_str),
                timeout=_CHAIN_REQUEST_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.warning(
                "[symbol_registry] Chain fetch timeout (%ds): %s %s — skipping expiry",
                _CHAIN_REQUEST_TIMEOUT_S, ticker, expiry_str,
            )
            continue
        except Exception as exc:
            log.warning(
                "[symbol_registry] Chain fetch error: %s %s — %s",
                ticker, expiry_str, exc,
            )
            continue

        if not chain:
            continue

        for contract in chain:
            occ    = getattr(contract, "symbol", None) or contract.get("symbol", "")
            strike = float(getattr(contract, "strike", 0) or contract.get("strike", 0))
            ctype  = getattr(contract, "option_type", "") or contract.get("option_type", "")
            oi     = int(getattr(contract, "open_interest", 0) or contract.get("open_interest", 0))

            if not occ:
                continue

            # ATM filter
            if stock_price > 0 and not zero_price_fallback:
                atm_low  = stock_price * (1 - params.atm_pct)
                atm_high = stock_price * (1 + params.atm_pct)
                if not (atm_low <= strike <= atm_high):
                    continue

            new_registry[occ] = ContractMeta(
                ticker        = ticker,
                strike        = strike,
                expiry        = expiry_str,
                contract_type = ctype,
                dte           = dte,
                open_interest = oi,
                tier          = tier,
            )

    log.debug("[symbol_registry] _build_ticker(%s): %d contracts added", ticker, sum(
        1 for m in new_registry.values() if m.ticker == ticker
    ))
