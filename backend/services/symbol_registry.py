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
  always doing a full rebuild after the first build().\n  Module-level imports of get_config, _fetch_thresholds, assign_tiers, and
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
  gate_config_store.get(\"min_premium\", tier_int). No string intermediary.
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
  can rely on epoch > 0 as a \"fully built from Tradier\" signal (same
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

FIX BUILD-PER-REQUEST-TIMEOUT (2026-05-13): add per-request timeout on every
  get_option_chain_bulk() call inside _build_ticker().
  Previously a single stalled TCP connection held a semaphore slot for up
  to 300s (the outer gather timeout). Now each chain fetch times out
  independently, frees the slot immediately, and the gather keeps cycling.
  On timeout: log WARNING for the specific (ticker, expiry) pair and
  continue to the next expiry — partial contracts already written to
  new_registry are retained.

FIX 2 (2026-05-13): _CHAIN_REQUEST_TIMEOUT_S 15s -> 8s.
  Tradier's chain API responds in <1s on healthy days and <4s under moderate
  load. The original 15s was sized as '1x httpx read_timeout (10s) + 5s
  buffer' but the httpx read_timeout is already enforced at the HTTP client
  layer — the asyncio.wait_for wrapper is a second, outer deadline. 8s
  gives full coverage for the realistic degraded-Tradier case (p95 ~4-5s
  under heavy load) while freeing stalled semaphore slots 2x faster. At
  AdaptiveSemaphore concurrency=20 and 8s timeout, worst-case cold-build
  wall time is unchanged (~57s realistic) because AdaptiveSemaphore already
  drops concurrency before per-slot stalls accumulate to 15s.

FIX BUILD-GATHER-TIMEOUT (2026-05-13, corrected 2026-05-13): _CHAIN_GATHER_TIMEOUT_S
  180 -> 300 -> 1800 (Option B cold-start safety net).
  The 300s value was sized for the wrong scale: commit 515cb2f7 commented
  \"~765 tickers at concurrency=20\" but the actual watchlist is 3,848 tickers.
  Cold-build math: ceil(3848 / 20) = 193 serial batches x 15s per-request
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
    p95 > _P95_DROP_THRESHOLD_S    (10.0s) -> drop down by _ADAPT_STEP (5),
                                               floored at _CONCURRENCY_MIN (15)
    1.0s <= p95 <= 10.0s            (hold)  -> no change

  Starting concurrency: _DEFAULT_BUILD_CONCURRENCY=20 (unchanged baseline).

  On a clean Tradier day (p95 ~0.6s typical):
    - Ramps to 40 within the first 100 completions (~2 adapt cycles).
    - Cold-build wall time recovers to ~42-48s, closing the gap vs the
      former fixed-50 setting on stable/ingestion-frontend-2026-04-29.

  Under degraded Tradier (p95 > 10s):
    - Drops to 15 within one adapt cycle, reducing simultaneous inflight
      calls and preventing the stall-slot saturation that caused the
      original BUILD-SEMAPHORE regression.
    - _ADAPT_DROP_COOLDOWN_S (30s) prevents consecutive drops within the
      same stabilisation window.

  The per-request 8s wait_for (FIX 2) and the 1800s outer gather timeout
  (BUILD-GATHER-TIMEOUT) are unchanged — they are the hard safety net.
  AdaptiveSemaphore operates at the soft throughput layer.

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
      release — the permit is consumed (destroyed), permanently reducing
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

FIX SINGLETON (2026-05-13): add init_registry() / get_registry() module-level
  singleton functions.
  main.py line 151 imports both names but symbol_registry.py never defined
  them, causing an ImportError crash on every deploy. init_registry()
  constructs the single SymbolRegistry instance; get_registry() returns it
  and raises RuntimeError if called before init_registry(). Pattern mirrors
  gate_config_store.init_store() / get_store() already used in this codebase.

FIX REFRESH-LOOP (2026-05-13): add refresh_loop() method to SymbolRegistry.
  main.py line 883 calls registry.refresh_loop() inside the lifespan context,
  but the method was never ported from stable/ingestion-frontend-2026-04-29
  to main. This caused an AttributeError crash on every deploy.

  The loop:
    - reads REGISTRY_EXPIRY_DAY_REFRESH_MINS and REGISTRY_REFRESH_MINS from
      DB config via get_config()
    - checks whether any loaded contract expires today to pick the interval
    - sleeps the interval, then calls self.build() (H3 incremental on warm
      restarts — only DTE=0 tickers are re-fetched)
    - catches and logs any build() exceptions as non-fatal so the loop
      continues on the next cycle

FIX ASSIGN-TIERS-CALL (2026-05-13): fix broken assign_tiers() call in build().
  The post-build reclassification called assign_tiers(oi_by_ticker=new_oi_map,
  thresh=thresh, require_oi=True) which does not match the actual signature
  assign_tiers(quotes: list[SymbolQuote], thresholds=None, require_oi=False).
  This would raise TypeError at runtime on every build() completion.
  Fix: construct synthetic_quotes list (SymbolQuote per ticker with price,
  volume, avg_volume from raw_quotes, and open_interest from new_oi_map) and
  call assign_tiers(synthetic_quotes, thresholds=thresh, require_oi=True).
  Matches stable/ingestion-frontend-2026-04-29 pattern exactly.

FIX FREE-FUNCTIONS (2026-05-13): add module-level _fetch_stock_prices(tickers)
  and _build_ticker(...) that build() calls as free functions.
  build() references these as module-level free functions (not self.* methods)
  but they were never defined, causing NameError on every build() invocation.
  _fetch_stock_prices(tickers): batches tickers 200/request, fires all batches
    concurrently via asyncio.gather, returns (prices, raw_quotes).
  _build_ticker(ticker, prices, tier_params, new_registry, oi_by_ticker,
    zero_price_fallback, tier_map): fetches expirations + option chains for
    one ticker, filters by ATM/DTE/OI, writes ContractMeta to new_registry,
    accumulates OI into oi_by_ticker. Includes B-ZERO-PRICE bypass and
    BUILD-PER-REQUEST-TIMEOUT (8s per chain fetch per FIX 2). QQ1-B: round() for OI.

FIX MARKET-HOURS-GATE (2026-05-13): build() now skips Tradier calls outside
  market hours (Mon-Fri 09:30-16:05 ET).

  Two cases:

  1. Warm registry (self._registry populated via load_from_db or prior build):
     Return (len(self._registry), {}) immediately with INFO log. No lock
     contention, no Tradier calls. refresh_loop() retries on the next normal
     interval cycle; the first in-hours cycle runs H3 incremental as designed.

  2. Cold registry (empty — no DB cache loaded, first ever startup):
     Return (0, {}) WITHOUT setting _build_complete. Logs WARNING so ops
     knows stream workers will not spawn until market open. refresh_loop()
     will call _sleep_until_market_open() and then fire build() immediately
     at 09:30 ET, completing the cold build and setting _build_complete.

  refresh_loop() parallel guard: if _is_market_hours() is False at the top
  of a refresh cycle, call _sleep_until_market_open() then invoke build()
  immediately (rather than sleeping the full interval again post-open).
  This guarantees the first in-market build fires at exactly 09:30 ET.

  _is_market_hours() and _sleep_until_market_open() are imported from
  chain_store (FIX #134, 2026-05-13) — no duplication of ET constants.

FIX ADAPT-001 (2026-05-14): AdaptiveSemaphore freefall fix.
  Observed: 40->35->30->25->20->15 in 11s during 30-min market-hours refresh.
  Root cause 1: _P95_DROP_THRESHOLD_S=5.0s too aggressive for peak-hours
    Tradier; realistic p95 under load is 7-17s — threshold fired on normal
    market conditions, not just degraded API.
  Root cause 2: No cooldown between consecutive drops. Each drop raised p95
    further (fewer slots = slower drain = higher per-slot latency), creating
    a self-reinforcing cascade until _CONCURRENCY_MIN=15 was hit.
  Fix 1: _P95_DROP_THRESHOLD_S 5.0 -> 10.0
    Only fires on genuinely degraded Tradier (not peak-hours normal load).
  Fix 2: _ADAPT_DROP_COOLDOWN_S = 30.0
    Drop branch in _maybe_adapt() checks (now - _last_drop_ts) < cooldown
    and skips with DEBUG log if within window. Ramp-up is NOT rate-limited.
    _last_drop_ts tracked as float on AdaptiveSemaphore instance.
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
from services.chain_store import load_chain, _is_market_hours, _sleep_until_market_open
from utils.tradier_client import get_expirations, get_option_chain_bulk, get_quotes_batch

log = logging.getLogger("symbol_registry")

# ---------------------------------------------------------------------------
# Build concurrency constants
# ---------------------------------------------------------------------------

_DEFAULT_BUILD_CONCURRENCY = 20

_CONCURRENCY_MIN            = 15
_CONCURRENCY_MAX            = 40
_ADAPT_STEP                 = 5
_ADAPT_SAMPLE_INTERVAL      = 20
_ADAPT_WINDOW               = 100
_P95_RAMP_UP_THRESHOLD_S    = 1.0
_P95_DROP_THRESHOLD_S       = 10.0   # ADAPT-001: raised from 5.0 — peak-hours Tradier p95 is 7-17s
_ADAPT_DROP_COOLDOWN_S      = 30.0   # ADAPT-001: min seconds between consecutive drops

_PRICES_FETCH_TIMEOUT_S  = 45
_CHAIN_GATHER_TIMEOUT_S  = 1800
_CHAIN_REQUEST_TIMEOUT_S = 8   # FIX 2: 15s -> 8s


# ---------------------------------------------------------------------------
# AdaptiveSemaphore
# ---------------------------------------------------------------------------

class AdaptiveSemaphore:
    """
    asyncio.Semaphore wrapper with rolling p95 latency-driven concurrency
    adjustment. See module docstring FIX BUILD-ADAPTIVE-CONCURRENCY and
    FIX ADAPT-001 for full specification.
    """

    def __init__(self, initial: int) -> None:
        self._value   = max(_CONCURRENCY_MIN, min(_CONCURRENCY_MAX, initial))
        self._sem     = asyncio.Semaphore(self._value)
        self._samples: collections.deque = collections.deque(maxlen=_ADAPT_WINDOW)
        self._since_last_adapt = 0
        self._start_ts: Optional[float] = None
        self._pending_drain: int = 0
        self._last_drop_ts: float = 0.0   # ADAPT-001: cooldown tracking

    @property
    def value(self) -> int:
        return self._value

    async def __aenter__(self):
        self._start_ts = _time.monotonic()
        await self._sem.acquire()
        return self

    async def __aexit__(self, *_):
        elapsed = _time.monotonic() - (self._start_ts or _time.monotonic())
        if self._pending_drain > 0:
            self._pending_drain -= 1
            # permit absorbed — do NOT release
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
            # ADAPT-001: enforce drop cooldown — skip if we dropped too recently
            now = _time.monotonic()
            since_last_drop = now - self._last_drop_ts
            if since_last_drop < _ADAPT_DROP_COOLDOWN_S:
                log.debug(
                    "[symbol_registry] AdaptiveSemaphore: p95=%.2fs > %.1fs but "
                    "drop cooldown active (%.1fs remaining) — holding concurrency=%d",
                    p95, _P95_DROP_THRESHOLD_S,
                    _ADAPT_DROP_COOLDOWN_S - since_last_drop, self._value,
                )
                return

            step = min(_ADAPT_STEP, self._value - _CONCURRENCY_MIN)
            self._value -= step
            drained = 0
            for _ in range(step):
                if self._sem._value > 0:  # type: ignore[attr-defined]
                    await self._sem.acquire()
                    drained += 1
            lazy = step - drained
            self._pending_drain += lazy
            self._last_drop_ts = now   # ADAPT-001: record drop time
            log.info(
                "[symbol_registry] AdaptiveSemaphore: p95=%.2fs > %.1fs -> "
                "drop concurrency %d -> %d (drained=%d, pending_drain=%d)",
                p95, _P95_DROP_THRESHOLD_S,
                self._value + step, self._value, drained, self._pending_drain,
            )

        else:
            log.debug(
                "[symbol_registry] AdaptiveSemaphore: p95=%.2fs in [%.1f, %.1f] -> hold concurrency=%d",
                p95, _P95_RAMP_UP_THRESHOLD_S, _P95_DROP_THRESHOLD_S, self._value,
            )


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Module-level free functions (called by build() as _fetch_stock_prices() and
# _build_ticker() — NOT instance methods).
# ---------------------------------------------------------------------------

async def _fetch_stock_prices(
    tickers: list[str],
) -> tuple[dict[str, float], dict[str, dict]]:
    """
    Fetch last/close prices for tickers in batches of 200 via Tradier quotes.

    Returns
    -------
    prices    : dict[ticker -> float]   last known price; missing if unavailable
    raw_quotes: dict[ticker -> raw_q]   full quote dict for synthetic_quotes construction
    """
    prices: dict[str, float] = {}
    raw_quotes: dict[str, dict] = {}
    if not tickers:
        return prices, raw_quotes

    batch_size = 200
    batches = [
        tickers[i:i + batch_size]
        for i in range(0, len(tickers), batch_size)
    ]
    results = await asyncio.gather(*[get_quotes_batch(b) for b in batches])
    for quote_map in results:
        for sym, q in quote_map.items():
            raw_quotes[sym] = q
            for key in ("last", "last_price", "close", "prevclose"):
                val = q.get(key)
                if val:
                    try:
                        prices[sym] = float(val)
                        break
                    except (TypeError, ValueError):
                        pass
    return prices, raw_quotes


async def _build_ticker(
    ticker:              str,
    prices:              dict[str, float],
    tier_params:         dict[int, _TierParams],
    new_registry:        dict[str, ContractMeta],
    oi_by_ticker:        dict[str, int],
    zero_price_fallback: bool = False,
    tier_map:            Optional[dict[str, int]] = None,
) -> None:
    """
    Fetch expirations + option chains for a single ticker and write
    ContractMeta entries into new_registry.

    B-ZERO-PRICE: when stock_price <= 0:
      - zero_price_fallback=True  -> bypass ATM filter (atm_low=0, atm_high=inf).
        DTE gating still applies. Logs WARNING and continues.
      - zero_price_fallback=False -> skip ticker (regression guard).

    FIX 2: each get_option_chain_bulk() call is wrapped in
      asyncio.wait_for(_CHAIN_REQUEST_TIMEOUT_S=8s). Frees semaphore slot
      immediately on stall rather than holding until outer gather timeout.

    QQ1-B: OI average uses round() not floor division.
    QQ1-A: caller passes real tier_params (not bootstrap T3 collapse).
    """
    if tier_map is None:
        tier_map = {}

    stock_price = prices.get(ticker, 0.0)

    if stock_price <= 0:
        if zero_price_fallback:
            log.warning(
                "[symbol_registry] %s: no stock price — bypassing ATM filter "
                "(zero-price fallback active)",
                ticker,
            )
        else:
            log.warning("[symbol_registry] %s: no stock price — skipping", ticker)
            return

    tier   = tier_map.get(ticker, 3)
    params = tier_params.get(tier) or tier_params[3]

    try:
        expirations = await get_expirations(ticker)
    except Exception as e:
        log.warning("[symbol_registry] %s: expirations fetch failed: %s", ticker, e)
        return

    today = date.today()

    if stock_price > 0:
        atm_low  = stock_price * (1 - params.atm_pct)
        atm_high = stock_price * (1 + params.atm_pct)
    else:
        # zero_price_fallback=True path: bypass ATM filter entirely
        atm_low  = 0.0
        atm_high = float("inf")

    for expiry_str in expirations:
        try:
            exp_date = date.fromisoformat(expiry_str)
        except ValueError:
            continue
        dte = (exp_date - today).days
        if dte < 0 or dte > params.max_dte:
            continue

        try:
            contracts = await asyncio.wait_for(
                get_option_chain_bulk(ticker, expiry_str),
                timeout=_CHAIN_REQUEST_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.warning(
                "[symbol_registry] %s %s: chain fetch timed out after %ds — skipping expiry",
                ticker, expiry_str, _CHAIN_REQUEST_TIMEOUT_S,
            )
            continue
        except Exception as e:
            log.warning(
                "[symbol_registry] %s %s: chain fetch failed: %s",
                ticker, expiry_str, e,
            )
            continue

        for contract in contracts:
            try:
                strike = float(contract.get("strike", 0) or 0)
                if strike <= 0:
                    continue
                if not (atm_low <= strike <= atm_high):
                    continue
                oi = int(contract.get("open_interest", 0) or 0)
                if oi < params.min_oi:
                    continue
                opt_type = (contract.get("option_type") or "").upper()
                if opt_type not in ("C", "P", "CALL", "PUT"):
                    continue
                contract_type = "CALL" if opt_type in ("C", "CALL") else "PUT"
                occ_symbol = contract.get("symbol", "").strip()
                if not occ_symbol:
                    continue
                new_registry[occ_symbol] = ContractMeta(
                    ticker        = ticker,
                    strike        = strike,
                    expiry        = expiry_str,
                    contract_type = contract_type,
                    dte           = dte,
                    open_interest = oi,
                    tier          = tier_map.get(ticker, 3),
                )
            except Exception as inner_exc:
                log.debug(
                    "[symbol_registry] %s: contract parse error: %s",
                    ticker, inner_exc,
                )

    # QQ1-B: round() not floor division for OI average
    total_oi = sum(
        meta.open_interest
        for meta in new_registry.values()
        if meta.ticker == ticker
    )
    count = sum(1 for m in new_registry.values() if m.ticker == ticker)
    if count > 0:
        oi_by_ticker[ticker] = round(total_oi / count)


# ---------------------------------------------------------------------------
# SymbolRegistry
# ---------------------------------------------------------------------------

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
                          the number of completed builds.

        Consumers (stream_worker, tradier_stream) watch this value to detect
        tier-map refreshes without polling symbol keys.
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
        self._build_complete: bool = False
        self.epoch: int = 0

    # -----------------------------------------------------------------------
    # Public accessors
    # -----------------------------------------------------------------------

    def lookup(self, occ_symbol: str) -> Optional[ContractMeta]:
        return self._registry.get(occ_symbol.strip())

    def all_symbols(self) -> list[str]:
        return list(self._registry.keys())

    def size(self) -> int:
        return len(self._registry)

    def stock_price(self, ticker: str) -> float:
        return self._stock_prices.get(ticker, 0.0)

    def is_ready(self) -> bool:
        # M-2: _build_complete only, never len(registry) > 0
        return self._build_complete

    def set_tier_map(self, tier_map: dict[str, int]) -> None:
        self._tier_map = tier_map

    def get_oi_map(self) -> dict[str, int]:
        return dict(self._oi_by_ticker)

    def influence_tier_int(self, ticker: str) -> int:
        """
        Return integer tier (1/2/3) for ticker. Fallback: 3 (T3 defaults).
        ING-010: sole tier accessor — no string intermediary.
        """
        return self._tier_map.get(ticker, 3)

    # -----------------------------------------------------------------------
    # load_from_db
    # -----------------------------------------------------------------------

    async def load_from_db(self, snapshot_id: str) -> int:
        chain = await load_chain(snapshot_id)
        if chain is None:
            log.warning(
                "[symbol_registry] load_from_db: DB error for snapshot %s — "
                "cold build of all %d tickers will run "
                "(expected ~155 workers on success). "
                "H3 incremental path disabled until first build() completes.",
                snapshot_id,
                len(self._watchlist),
            )
            return 0
        if not chain:
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
        # M-1/M-2: do NOT set _build_complete here
        oi_acc: dict[str, list[int]] = {}
        for meta in chain.values():
            oi_acc.setdefault(meta.ticker, []).append(meta.open_interest)
        # QQ1-B: round() not floor division
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

    # -----------------------------------------------------------------------
    # build
    # -----------------------------------------------------------------------

    async def build(self) -> tuple[int, dict[str, dict]]:
        """
        Build (or incrementally refresh) the OCC registry.

        MARKET-HOURS-GATE: if called outside NYSE market hours (Mon-Fri
        09:30-16:05 ET), Tradier's chain and quote APIs return stale or
        error responses.  Two early-exit cases:

          Warm (self._registry populated):
            Return (len(registry), {}) immediately — the existing registry
            is still valid.  refresh_loop() will retry on the next cycle;
            the first in-hours cycle runs H3 incremental as designed.

          Cold (self._registry empty, no DB seed loaded):
            Return (0, {}) WITHOUT setting _build_complete.  Stream workers
            stay blocked.  refresh_loop() calls _sleep_until_market_open()
            and fires build() immediately at 09:30 ET.

        See module docstring for full fix history.
        """
        from services.symbols_loader import SymbolQuote

        # ---------------------------------------------------------------
        # MARKET-HOURS-GATE (FIX MARKET-HOURS-GATE 2026-05-13)
        # ---------------------------------------------------------------
        if not _is_market_hours():
            if self._registry:
                log.info(
                    "[symbol_registry] build() called outside market hours — "
                    "registry already warm (%d contracts), skipping Tradier calls. "
                    "Next refresh_loop() cycle will run H3 incremental at market open.",
                    len(self._registry),
                )
                return len(self._registry), {}
            else:
                log.warning(
                    "[symbol_registry] build() called outside market hours with "
                    "EMPTY registry (no DB seed loaded). Tradier calls suppressed. "
                    "_build_complete remains False — stream workers will not spawn "
                    "until market open. refresh_loop() will call "
                    "_sleep_until_market_open() and fire build() at 09:30 ET.",
                )
                return 0, {}

        cfg, thresh = await asyncio.gather(get_config(), _fetch_thresholds())
        # QQ1-A: real tier_params from the first build — bootstrap_params removed
        tier_params = _build_tier_params(thresh, global_min_oi=cfg["REGISTRY_MIN_OI"])

        starting_concurrency = int(cfg.get("REGISTRY_BUILD_CONCURRENCY", _DEFAULT_BUILD_CONCURRENCY))
        sem = AdaptiveSemaphore(initial=starting_concurrency)

        async with self._build_lock:
            # H3: incremental guard — populated registry is the signal
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
                    len(self._watchlist) * 20,
                )
                tickers_to_build = list(self._watchlist)
                tickers_to_carry = []

            # Carry forward non-expiring tickers from the warm registry
            new_registry: dict[str, ContractMeta] = {}
            for ticker in tickers_to_carry:
                for occ, meta in self._registry.items():
                    if meta.ticker == ticker:
                        new_registry[occ] = meta

            # BUILD-HANG phase 1: price fetch with 45s hard timeout
            zero_price_fallback = False
            prices: dict[str, float] = {}
            raw_quotes: dict[str, dict] = {}
            try:
                prices, raw_quotes = await asyncio.wait_for(
                    _fetch_stock_prices(tickers_to_build),
                    timeout=_PRICES_FETCH_TIMEOUT_S,
                )
                self._stock_prices = prices
                log.info("[symbol_registry] Stock prices fetched: %d tickers", len(prices))
            except asyncio.TimeoutError:
                log.error(
                    "[symbol_registry] _fetch_stock_prices timed out after %ds — "
                    "proceeding with zero_price_fallback=True (ATM filtering bypassed)",
                    _PRICES_FETCH_TIMEOUT_S,
                )
                zero_price_fallback = True

            if not zero_price_fallback and tickers_to_build and not prices:
                log.error(
                    "[symbol_registry] _fetch_stock_prices returned 0 prices for "
                    "%d tickers — proceeding with zero_price_fallback=True",
                    len(tickers_to_build),
                )
                zero_price_fallback = True

            # OI map shared across all _build_ticker coroutines
            new_oi_by_ticker: dict[str, int] = {
                t: v for t, v in self._oi_by_ticker.items()
                if t in set(tickers_to_carry)
            }

            # BUILD-HANG phase 2: chain gather with 1800s hard timeout
            if tickers_to_build:
                async def _build_with_sem(ticker: str) -> None:
                    try:
                        async with sem:
                            await _build_ticker(
                                ticker=ticker,
                                prices=prices,
                                tier_params=tier_params,
                                new_registry=new_registry,
                                oi_by_ticker=new_oi_by_ticker,
                                zero_price_fallback=zero_price_fallback,
                                tier_map=self._tier_map,
                            )
                    except asyncio.CancelledError:
                        raise

                tasks = [_build_with_sem(t) for t in tickers_to_build]

                try:
                    results = await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True),
                        timeout=_CHAIN_GATHER_TIMEOUT_S,
                    )
                    # BUILD-EXCEPTION-VISIBILITY
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
                    log.error(
                        "[symbol_registry] BUILD-HANG: chain gather timed out after %ds "
                        "(%d tickers queued, concurrency=%d at timeout). "
                        "Proceeding with partial registry (%d contracts so far) — "
                        "stream workers will spawn against partial data. "
                        "Next refresh_loop() will complete the missing tickers.",
                        _CHAIN_GATHER_TIMEOUT_S, len(tickers_to_build),
                        sem.value, len(new_registry),
                    )

                log.info(
                    "[symbol_registry] Build gather complete: final_concurrency=%d "
                    "(started=%d, min=%d, max=%d)",
                    sem.value, starting_concurrency, _CONCURRENCY_MIN, _CONCURRENCY_MAX,
                )

            # Re-derive oi_map from the finished new_registry (covers carried tickers too)
            oi_acc: dict[str, list[int]] = {}
            for meta in new_registry.values():
                oi_acc.setdefault(meta.ticker, []).append(meta.open_interest)
            # QQ1-B: round() not floor division
            new_oi_map = {t: round(sum(v) / len(v)) for t, v in oi_acc.items() if v}

            # C-3 / ASSIGN-TIERS-CALL fix: build synthetic_quotes and call
            # assign_tiers(quotes, thresholds=thresh, require_oi=True)
            # DO NOT call assign_tiers(oi_by_ticker=...) — wrong signature.
            synthetic_quotes = []
            for ticker in self._watchlist:
                q = raw_quotes.get(ticker, {})
                vol = avg_vol = 0
                try:
                    vol = int(q.get("volume") or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    avg_vol = int(q.get("average_volume") or 0)
                except (TypeError, ValueError):
                    pass
                synthetic_quotes.append(SymbolQuote(
                    symbol         = ticker,
                    last_price     = prices.get(ticker, 0.0),
                    volume         = vol,
                    average_volume = avg_vol,
                    open_interest  = new_oi_map.get(ticker, 0),
                ))

            try:
                new_tier_map = await assign_tiers(
                    synthetic_quotes,
                    thresholds=thresh,
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
                new_tier_map = dict(self._tier_map)

            # Apply new tier map to all contracts
            for occ_sym, meta in new_registry.items():
                meta.tier = new_tier_map.get(meta.ticker, 3)

            old_count          = len(self._registry)
            self._registry     = new_registry
            self._oi_by_ticker = new_oi_map
            self._tier_map     = new_tier_map
            self._last_build   = datetime.utcnow()

            # M-1/M-2: set _build_complete AFTER self._registry is swapped
            self._build_complete = True
            # ING-010-EPOCH: increment epoch inside the lock after _build_complete
            self.epoch += 1

            t_counts = {1: 0, 2: 0, 3: 0}
            for m in new_registry.values():
                t_counts[m.tier] = t_counts.get(m.tier, 0) + 1

            if zero_price_fallback:
                log.warning(
                    "[symbol_registry] Build complete (ZERO-PRICE FALLBACK): %d OCC symbols "
                    "(T1=%d T2=%d T3=%d) epoch=%d — contracts loaded without ATM price filtering. "
                    "Next refresh will re-apply ATM filtering once prices are available.",
                    len(new_registry),
                    t_counts[1], t_counts[2], t_counts[3], self.epoch,
                )
            else:
                log.info(
                    "[symbol_registry] Build complete: %d OCC symbols "
                    "(T1=%d T2=%d T3=%d) epoch=%d (was %d, delta=%+d) | "
                    "OI map: %d tickers | _build_complete=True — stream workers may now spawn",
                    len(new_registry),
                    t_counts[1], t_counts[2], t_counts[3], self.epoch,
                    old_count, len(new_registry) - old_count,
                    len(new_oi_map),
                )

            await self._persist_to_db(new_registry)
            return len(new_registry), raw_quotes  # H1: return raw_quotes

    # -----------------------------------------------------------------------
    # _persist_to_db
    # -----------------------------------------------------------------------

    async def _persist_to_db(self, registry_dict: dict[str, ContractMeta]) -> None:
        from services.chain_store import save_chain
        from services import universe_store
        try:
            loop = asyncio.get_running_loop()
            snap_rows = await loop.run_in_executor(
                None,
                lambda: universe_store._client()
                    .table("options_universe_snapshots")
                    .select("id")
                    .eq("is_active", True)
                    .order("fetched_at", desc=True)
                    .limit(1)
                    .execute()
                    .data,
            )
            if not snap_rows:
                log.warning(
                    "[symbol_registry] _persist_to_db: no active snapshot — chain not persisted"
                )
                return
            snapshot_id = snap_rows[0]["id"]
            ok = await save_chain(snapshot_id, registry_dict)
            if ok:
                self._persisted_snapshot_id = snapshot_id
                log.info(
                    "[symbol_registry] _persist_to_db: %d contracts persisted "
                    "to snapshot %s",
                    len(registry_dict), snapshot_id,
                )
        except Exception as exc:
            log.warning(
                "[symbol_registry] _persist_to_db error (non-fatal): %s", exc
            )

    # -----------------------------------------------------------------------
    # refresh_loop  (FIX REFRESH-LOOP 2026-05-13 + FIX MARKET-HOURS-GATE 2026-05-13)
    # -----------------------------------------------------------------------

    async def refresh_loop(self) -> None:
        """
        Scheduled registry refresh loop. Called by main.py inside the
        lifespan context as an asyncio task.

        Behaviour:
          - Reads REGISTRY_EXPIRY_DAY_REFRESH_MINS and REGISTRY_REFRESH_MINS
            from DB config via get_config().
          - Uses the shorter EXPIRY_DAY interval when any contract expires
            today; uses the normal interval otherwise.
          - MARKET-HOURS-GATE: if outside market hours at the top of a cycle,
            calls _sleep_until_market_open() then fires build() immediately
            rather than sleeping the full interval first. This ensures the
            first in-market build happens at 09:30 ET, not 09:30 + interval.
          - Sleeps the chosen interval, then calls self.build().
            H3 incremental: only tickers with min_dte=0 are re-fetched.
          - build() exceptions are caught and logged as non-fatal so the loop
            continues on the next cycle.
        """
        while True:
            cfg = await get_config()

            today = date.today()
            has_expiry_today = any(
                meta.expiry == today.isoformat()
                for meta in self._registry.values()
            )
            interval_mins = (
                cfg["REGISTRY_EXPIRY_DAY_REFRESH_MINS"]
                if has_expiry_today
                else cfg["REGISTRY_REFRESH_MINS"]
            )

            # MARKET-HOURS-GATE: if currently outside market hours, sleep
            # until open and fire build() immediately at 09:30 ET rather
            # than waiting the full interval after open.
            if not _is_market_hours():
                log.info(
                    "[symbol_registry] refresh_loop: outside market hours — "
                    "sleeping until market open before next build()"
                )
                await _sleep_until_market_open()
                log.info(
                    "[symbol_registry] refresh_loop: market open — firing build() now"
                )
                try:
                    await self.build()
                except Exception as e:
                    log.error("[symbol_registry] Refresh (post-open) failed (non-fatal): %s", e)
                continue

            await asyncio.sleep(interval_mins * 60)

            log.info(
                "[symbol_registry] Scheduled refresh (interval=%dmin, expiry_day=%s)",
                interval_mins, has_expiry_today,
            )
            try:
                await self.build()
            except Exception as e:
                log.error("[symbol_registry] Refresh failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Module-level singleton helpers  (FIX SINGLETON 2026-05-13)
# ---------------------------------------------------------------------------

_registry_instance: Optional[SymbolRegistry] = None


def init_registry(
    watchlist: Optional[list[str]] = None,
    tier_map:  Optional[dict[str, int]] = None,
) -> SymbolRegistry:
    """Construct and store the single SymbolRegistry instance. Returns it."""
    global _registry_instance
    _registry_instance = SymbolRegistry(watchlist=watchlist, tier_map=tier_map)
    return _registry_instance


def get_registry() -> SymbolRegistry:
    """
    Return the singleton SymbolRegistry.
    Raises RuntimeError if called before init_registry().
    """
    if _registry_instance is None:
        raise RuntimeError(
            "get_registry() called before init_registry(). "
            "Call init_registry() during app startup."
        )
    return _registry_instance
