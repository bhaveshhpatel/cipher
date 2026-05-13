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
  always doing a full rebuild after the first build().
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

FIX ING-010 (2026-05-07): Add influence_tier_int() and influence_tier_string().
  _resolve_min_premium() in tradier_stream.py calls influence_tier_string(ticker)
  to resolve the per-ticker flow-influence bucket (WHALE/INSTITUTIONAL/LARGE/
  RETAIL) before looking up the gate floor in gate_config_store.
  influence_tier_int() exposes the raw _tier_map int for callers that need
  the numeric key directly (e.g. gate_config_store.get("min_premium", tier_int)).
  Both methods fall back to the most conservative value (3 / "RETAIL") when
  the ticker is absent from _tier_map, matching the T3 defaults in
  gate_config_store and the _DEFAULT_TIER_INT constant in tradier_stream.

ING-010-EPOCH (2026-05-07): Add epoch versioning to SymbolRegistry.
  self.epoch: int is initialised to 0 in __init__ and incremented inside the
  build() lock immediately after self._build_complete = True.
  Contract (mirrors GateConfigStore.epoch):
    - epoch == 0  → registry has never completed a full build().
    - epoch > 0   → at least one build() has completed; value is the build
                    generation count (1, 2, 3, …).
  Consumers (stream_worker, tradier_stream) can watch registry.epoch to
  detect tier-map refreshes without polling individual symbol keys.
  load_from_db() does NOT increment epoch — only build() does, so callers
  can rely on epoch > 0 as a "fully built from Tradier" signal (same
  semantics as _build_complete).

FIX BUILD-SEMAPHORE (2026-05-13): _DEFAULT_BUILD_CONCURRENCY 50 -> 20.
  50 concurrent _build_ticker coroutines each iterating 5-8 expirations
  created 250-400 simultaneous Tradier HTTP calls. Tradier rate-limits
  silently — stalled slots held the semaphore until the gather timeout fired,
  killing all remaining tasks. 20 stays within Tradier's safe rate-limit
  headroom; AdaptiveSemaphore ramps higher on clean days.

FIX BUILD-HANG (2026-05-13): Hard timeouts on both network phases inside
  build(). _fetch_stock_prices() wrapped in wait_for(45s); asyncio.gather
  for chain fetches wrapped in wait_for(1800s). Both phases degrade
  gracefully on timeout (zero-price fallback / partial registry) and always
  set _build_complete=True so stream workers can spawn.
  1800s sized for 3,848-ticker cold builds at concurrency=20 (300s fired
  too early on this stable branch).

FIX BUILD-PER-REQUEST-TIMEOUT (2026-05-13): 15s wait_for per
  get_option_chain_bulk() call inside _build_ticker(). Frees semaphore slots
  immediately on TCP stall instead of holding until the outer gather timeout.

FIX SHUTDOWN-CANCEL (2026-05-13): _build_with_sem catches CancelledError
  and re-raises immediately — eliminates '_GatheringFuture exception was
  never retrieved' log noise on shutdown.

FIX BUILD-EXCEPTION-VISIBILITY (2026-05-13): gather(return_exceptions=True)
  results inspected post-gather; non-CancelledError exception count logged
  at WARNING so ops can distinguish timed-out vs errored tickers.

FIX BUILD-ADAPTIVE-CONCURRENCY (2026-05-13): Replace fixed
  asyncio.Semaphore(20) with AdaptiveSemaphore. p95 < 1s -> ramp to 40;
  p95 > 5s -> drop to floor 15. On clean Tradier days cold-build wall time
  recovers to ~42-48s. Under Tradier degradation concurrency floors at 15
  preventing the stall-slot saturation seen in today's BUILD-HANG.

FIX ADAPTIVE-LAZY-DRAIN (2026-05-13): Fix AdaptiveSemaphore drop dead-zone
  when all permits are in-flight. self._value decremented immediately;
  pending permits absorbed lazily in __aexit__ as tasks complete and return
  their slots. Concurrency now actually converges to the target ceiling.
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

# BUILD-SEMAPHORE: baseline concurrency. Reduced 50 -> 20 on 2026-05-13.
# AdaptiveSemaphore ramps above this on clean days and drops under pressure.
_DEFAULT_BUILD_CONCURRENCY = 20

# BUILD-ADAPTIVE-CONCURRENCY: bounds and p95 thresholds for AdaptiveSemaphore.
_CONCURRENCY_MIN         = 15    # hard floor (spec: drop to 15 under p95 > 5s)
_CONCURRENCY_MAX         = 40    # hard ceiling
_ADAPT_STEP              = 5     # concurrency delta per adapt cycle
_ADAPT_SAMPLE_INTERVAL   = 20    # evaluate p95 every N slot completions
_ADAPT_WINDOW            = 100   # rolling window size for latency samples
_P95_RAMP_UP_THRESHOLD_S = 1.0   # p95 below this -> ramp up
_P95_DROP_THRESHOLD_S    = 5.0   # p95 above this -> drop

# BUILD-HANG: hard timeouts for the two network-bound phases inside build().
_PRICES_FETCH_TIMEOUT_S = 45    # _fetch_stock_prices()
_CHAIN_GATHER_TIMEOUT_S = 1800  # asyncio.gather(*tasks): 3,848 tickers cold at concurrency=20

# BUILD-PER-REQUEST-TIMEOUT: per get_option_chain_bulk() call inside _build_ticker().
_CHAIN_REQUEST_TIMEOUT_S = 15


# ---------------------------------------------------------------------------
# AdaptiveSemaphore
# ---------------------------------------------------------------------------

class AdaptiveSemaphore:
    """
    asyncio.Semaphore wrapper with rolling p95 latency-driven concurrency
    adjustment.

    Usage (identical to asyncio.Semaphore via async context manager)::

        sem = AdaptiveSemaphore(initial=20)
        async with sem:
            await do_work()

    Internals
    ---------
    - Tracks per-slot wall-clock duration (acquire -> release) in a
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
    - .value property exposes current concurrency for logging.
    """

    def __init__(self, initial: int) -> None:
        self._value   = max(_CONCURRENCY_MIN, min(_CONCURRENCY_MAX, initial))
        self._sem     = asyncio.Semaphore(self._value)
        self._samples: collections.deque = collections.deque(maxlen=_ADAPT_WINDOW)
        self._since_last_adapt = 0
        self._start_ts: Optional[float] = None
        # ADAPTIVE-LAZY-DRAIN: permits that must be absorbed before re-release.
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
        # ADAPTIVE-LAZY-DRAIN: absorb returning permit if a drop is pending.
        if self._pending_drain > 0:
            self._pending_drain -= 1
            # permit consumed — do NOT call self._sem.release()
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
            # ADAPTIVE-LAZY-DRAIN: decrement ceiling immediately; eagerly drain
            # free permits; store remainder for lazy absorption in __aexit__.
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


# ---------------------------------------------------------------------------
# ING-010: int tier -> influence string mapping.
# ---------------------------------------------------------------------------
_INT_TIER_TO_STRING: dict[int, str] = {
    1: "INSTITUTIONAL",
    2: "LARGE",
    3: "RETAIL",
}


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
          - epoch == 0  → no completed build() yet (may be DB-seeded via
                          load_from_db, but Tradier chain data not yet fresh).
          - epoch >= 1  → build() has completed at least once; value equals
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
    # ING-010: Tier accessors used by _resolve_min_premium() in
    # tradier_stream.py to resolve the per-ticker gate floor.
    # -----------------------------------------------------------------------

    def influence_tier_int(self, ticker: str) -> int:
        """
        Return the integer tier (1/2/3) for ticker from _tier_map.

        Fallback: 3 (RETAIL / most conservative floor) for any ticker not
        present in the map. This matches _DEFAULT_TIER_INT in tradier_stream
        and the T3 defaults seeded into gate_config_store.

        Thread-safe for reads: _tier_map is replaced atomically at the end of
        build() inside the build lock; dict.get() is safe under the GIL.
        """
        return self._tier_map.get(ticker, 3)

    def influence_tier_string(self, ticker: str) -> str:
        """
        Return the human-readable influence tier label for ticker.

        Resolution: _tier_map int -> _INT_TIER_TO_STRING.

        Return values: "INSTITUTIONAL" | "LARGE" | "RETAIL"
        Fallback:      "RETAIL" for unknown tickers or unexpected int values.
        """
        tier_int = self._tier_map.get(ticker, 3)
        return _INT_TIER_TO_STRING.get(tier_int, "RETAIL")

    async def load_from_db(self, snapshot_id: str) -> int:
        chain = await load_chain(snapshot_id)
        if chain is None:
            log.info(
                "[symbol_registry] load_from_db: DB error for snapshot %s - "
                "skipping pre-seed, full build() will populate registry",
                snapshot_id,
            )
            return 0
        if not chain:
            log.info(
                "[symbol_registry] load_from_db: no cached chain for snapshot %s "
                "(including fallback) - will do full build from Tradier",
                snapshot_id,
            )
            return 0
        self._registry = chain
        self._persisted_snapshot_id = snapshot_id
        oi_acc: dict[str, list[int]] = {}
        for meta in chain.values():
            oi_acc.setdefault(meta.ticker, []).append(meta.open_interest)
        self._oi_by_ticker = {
            t: int(sum(v) / len(v)) for t, v in oi_acc.items() if v
        }
        log.info(
            "[symbol_registry] load_from_db: seeded %d OCC contracts from DB "
            "(snapshot %s, oi_map=%d tickers) - waiting for build() to set "
            "_build_complete before stream workers are allowed to spawn",
            len(chain), snapshot_id, len(self._oi_by_ticker),
        )
        return len(chain)

    async def build(self) -> tuple[int, dict[str, dict]]:
        """
        Build (or incrementally refresh) the OCC registry.

        All fixes from stable baseline (P3/P4/C-3/H1/H3/M-1/M-2/M-3/
        B-ZERO-PRICE/ING-010/ING-010-EPOCH) retained unchanged.

        Additional fixes cherry-picked from main (2026-05-12/13):
          BUILD-SEMAPHORE          — concurrency 50 -> 20
          BUILD-ADAPTIVE-CONCURRENCY — AdaptiveSemaphore replaces plain Semaphore
          ADAPTIVE-LAZY-DRAIN      — drop dead-zone fix
          BUILD-HANG               — 45s prices timeout + 1800s gather timeout
          BUILD-PER-REQUEST-TIMEOUT— 15s per get_option_chain_bulk() call
          SHUTDOWN-CANCEL          — CancelledError re-raised in _build_with_sem
          BUILD-EXCEPTION-VISIBILITY — gather results inspected for exc count
        """
        from services.symbols_loader import SymbolQuote

        cfg, thresh = await asyncio.gather(get_config(), _fetch_thresholds())
        tier_params      = _build_tier_params(thresh, global_min_oi=cfg["REGISTRY_MIN_OI"])
        bootstrap_params = {1: tier_params[3], 2: tier_params[3], 3: tier_params[3]}

        # BUILD-ADAPTIVE-CONCURRENCY: AdaptiveSemaphore replaces plain
        # asyncio.Semaphore. cfg override (REGISTRY_BUILD_CONCURRENCY) still
        # honoured as starting value so env/DB config remains effective.
        build_concurrency = int(cfg.get("REGISTRY_BUILD_CONCURRENCY", _DEFAULT_BUILD_CONCURRENCY))
        sem = AdaptiveSemaphore(initial=build_concurrency)

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
                    "(expired today), %d carried forward (total watchlist=%d)",
                    len(tickers_to_refresh), len(tickers_to_carry), len(self._watchlist),
                )
            else:
                tickers_to_refresh = list(self._watchlist)
                tickers_to_carry   = []
                log.info(
                    "[symbol_registry] Full build: %d tickers (concurrency=%d) "
                    "[T1: atm=+/-%.0f%% dte=%d | T2: atm=+/-%.0f%% dte=%d | "
                    "T3: atm=+/-%.0f%% dte=%d | min_oi=%d]",
                    len(tickers_to_refresh),
                    build_concurrency,
                    tier_params[1].atm_pct * 100, tier_params[1].max_dte,
                    tier_params[2].atm_pct * 100, tier_params[2].max_dte,
                    tier_params[3].atm_pct * 100, tier_params[3].max_dte,
                    cfg["REGISTRY_MIN_OI"],
                )

            new_registry: dict[str, ContractMeta] = {
                occ: meta
                for occ, meta in self._registry.items()
                if meta.ticker in set(tickers_to_carry)
            }
            new_oi_by_ticker: dict[str, int] = {
                t: v
                for t, v in self._oi_by_ticker.items()
                if t in set(tickers_to_carry)
            }

            # BUILD-HANG: wrap _fetch_stock_prices in a hard 45s deadline.
            # On timeout, fall through to zero_price_fallback so chain
            # fetches still run (existing B-ZERO-PRICE path).
            prices: dict[str, float] = {}
            raw_quotes: dict[str, dict] = {}
            zero_price_fallback = False
            try:
                prices, raw_quotes = await asyncio.wait_for(
                    self._fetch_stock_prices(),
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

            if not zero_price_fallback and tickers_to_refresh and not prices:
                log.error(
                    "[symbol_registry] B-ZERO-PRICE: _fetch_stock_prices() returned 0 prices "
                    "for %d tickers - Tradier quote API may be down or rate-limited. "
                    "Falling back: ATM filter bypassed so chain fetches still run.",
                    len(tickers_to_refresh),
                )
                zero_price_fallback = True

            if tickers_to_refresh:
                async def _build_with_sem(ticker):
                    # SHUTDOWN-CANCEL: re-raise CancelledError immediately so
                    # asyncio can retire the gather future cleanly without
                    # logging it as '_GatheringFuture exception was never retrieved'.
                    try:
                        async with sem:
                            ticker_price = prices.get(ticker, 0.0)
                            await self._build_ticker(
                                ticker,
                                ticker_price,
                                new_registry,
                                new_oi_by_ticker,
                                bootstrap_params,
                                zero_price_fallback=zero_price_fallback,
                            )
                    except asyncio.CancelledError:
                        raise

                tasks = [
                    _build_with_sem(ticker)
                    for ticker in tickers_to_refresh
                ]

                # BUILD-HANG: wrap gather in hard 1800s deadline.
                # 1800s sized for 3,848-ticker cold build at concurrency=20.
                # On timeout: log BUILD-HANG error, proceed with partial registry.
                try:
                    results = await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True),
                        timeout=_CHAIN_GATHER_TIMEOUT_S,
                    )
                    # BUILD-EXCEPTION-VISIBILITY: count non-cancelled task exceptions.
                    exc_count = sum(
                        1 for r in results
                        if isinstance(r, Exception)
                        and not isinstance(r, asyncio.CancelledError)
                    )
                    if exc_count:
                        log.warning(
                            "[symbol_registry] Build gather complete with %d task exception(s) "
                            "(non-CancelledError) — check logs above for details",
                            exc_count,
                        )
                except asyncio.TimeoutError:
                    queued = len(tickers_to_refresh)
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
                sem.value, build_concurrency, _CONCURRENCY_MIN, _CONCURRENCY_MAX,
            )

            synthetic_quotes = []
            for ticker in self._watchlist:
                if ticker not in prices:
                    continue
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
                    open_interest  = new_oi_by_ticker.get(ticker, 0),
                ))

            live_tier_map = await assign_tiers(
                synthetic_quotes,
                thresholds=thresh,
                require_oi=True,
            )
            log.info(
                "[symbol_registry] Post-build tier reclassification: T1=%d T2=%d T3=%d",
                sum(1 for t in live_tier_map.values() if t == 1),
                sum(1 for t in live_tier_map.values() if t == 2),
                sum(1 for t in live_tier_map.values() if t == 3),
            )

            for occ_sym, meta in new_registry.items():
                meta.tier = live_tier_map.get(meta.ticker, 3)

            self._tier_map = live_tier_map

            old_count          = len(self._registry)
            self._registry     = new_registry
            self._oi_by_ticker = new_oi_by_ticker
            self._last_build   = datetime.utcnow()

            # M-1/M-2: mark registry as fully built.
            self._build_complete = True
            # ING-010-EPOCH: advance epoch.
            self.epoch += 1

            t_counts = {1: 0, 2: 0, 3: 0}
            for m in new_registry.values():
                t_counts[m.tier] = t_counts.get(m.tier, 0) + 1

            if zero_price_fallback:
                log.warning(
                    "[symbol_registry] Build complete (ZERO-PRICE FALLBACK): %d OCC symbols "
                    "(T1=%d T2=%d T3=%d) - contracts loaded without ATM price filtering. "
                    "Next refresh will re-apply ATM filtering once prices are available.",
                    len(new_registry),
                    t_counts[1], t_counts[2], t_counts[3],
                )
            else:
                log.info(
                    "[symbol_registry] Build complete: %d OCC symbols "
                    "(T1=%d T2=%d T3=%d) (was %d, delta=%+d) | OI map: %d tickers "
                    "| _build_complete=True epoch=%d - stream workers may now spawn",
                    len(new_registry),
                    t_counts[1], t_counts[2], t_counts[3],
                    old_count, len(new_registry) - old_count,
                    len(new_oi_by_ticker),
                    self.epoch,
                )

            await self._persist_to_db(new_registry)
            return len(new_registry), raw_quotes

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
                    "[symbol_registry] _persist_to_db: no active snapshot - chain not persisted"
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

    async def refresh_loop(self):
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
            await asyncio.sleep(interval_mins * 60)

            log.info("[symbol_registry] Scheduled refresh (interval=%dmin)", interval_mins)
            try:
                await self.build()
            except Exception as e:
                log.error("[symbol_registry] Refresh failed (non-fatal): %s", e)

    async def _fetch_stock_prices(self) -> tuple[dict[str, float], dict[str, dict]]:
        prices: dict[str, float] = {}
        raw_quotes: dict[str, dict] = {}
        batch_size = 200
        batches = [
            self._watchlist[i:i + batch_size]
            for i in range(0, len(self._watchlist), batch_size)
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
        self,
        ticker:              str,
        stock_price:         float,
        registry:            dict[str, ContractMeta],
        oi_by_ticker:        dict[str, int],
        tier_params:         dict[int, _TierParams],
        zero_price_fallback: bool = False,
    ):
        """
        Build OCC contracts for a single ticker.

        BUILD-PER-REQUEST-TIMEOUT: each get_option_chain_bulk() call is
        wrapped in asyncio.wait_for(_CHAIN_REQUEST_TIMEOUT_S=15s). Frees the
        semaphore slot immediately on TCP stall instead of holding it until
        the outer gather timeout fires.

        B-ZERO-PRICE: when stock_price <= 0:
          - zero_price_fallback=True  -> bypass ATM filter entirely.
          - zero_price_fallback=False -> skip as before.
        """
        if stock_price <= 0:
            if zero_price_fallback:
                log.warning(
                    "[symbol_registry] %s: no stock price - bypassing ATM filter "
                    "(zero-price fallback active)",
                    ticker,
                )
            else:
                log.warning("[symbol_registry] %s: no stock price - skipping", ticker)
                return

        tier   = self._tier_map.get(ticker, 3)
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

            # BUILD-PER-REQUEST-TIMEOUT: 15s hard deadline per chain fetch.
            # Frees the semaphore slot immediately on TCP stall.
            try:
                contracts = await asyncio.wait_for(
                    get_option_chain_bulk(ticker, expiry_str),
                    timeout=_CHAIN_REQUEST_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "[symbol_registry] Chain fetch timeout (%ds): %s %s — skipping expiry",
                    _CHAIN_REQUEST_TIMEOUT_S, ticker, expiry_str,
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
                    registry[occ_symbol] = ContractMeta(
                        ticker        = ticker,
                        strike        = strike,
                        expiry        = expiry_str,
                        contract_type = contract_type,
                        dte           = dte,
                        open_interest = oi,
                        tier          = self._tier_map.get(ticker, 3),
                    )
                except Exception as inner_exc:
                    log.debug(
                        "[symbol_registry] %s: contract parse error: %s",
                        ticker, inner_exc,
                    )

        total_oi = sum(
            meta.open_interest
            for meta in registry.values()
            if meta.ticker == ticker
        )
        count = sum(1 for m in registry.values() if m.ticker == ticker)
        if count > 0:
            oi_by_ticker[ticker] = total_oi // count


# ---------------------------------------------------------------------------
# Module-level singleton helpers
# ---------------------------------------------------------------------------
_registry_instance: Optional[SymbolRegistry] = None


def init_registry(
    watchlist: Optional[list[str]] = None,
    tier_map:  Optional[dict[str, int]] = None,
) -> SymbolRegistry:
    global _registry_instance
    _registry_instance = SymbolRegistry(watchlist=watchlist, tier_map=tier_map)
    return _registry_instance


def get_registry() -> Optional[SymbolRegistry]:
    return _registry_instance
