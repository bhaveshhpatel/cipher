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
  element; _post_build_upsert passes raw_quotes to
  _post_build_upsert so it can skip the duplicate _fetch_batch_quotes call.

FIX H3 (2026-04-27): Removed _seeded_from_db flag entirely. The incremental
  build guard is now `if self._registry:` - the populated registry itself is
  the correct signal for an incremental refresh. This means scheduled
  refresh_loop() calls also get incremental DTE-based pruning instead of
  always doing a full rebuild after the first build()`.
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

  - asyncio.gather(*tasks) for chain fetches: 600s timeout (covers full
    3949-ticker universe at concurrency=10 with per-request 45s timeouts).
    On expiry, logs ERROR and proceeds with whatever contracts were fetched
    before the deadline; _build_complete is still set so stream workers
    can spawn against the partial registry.

  Both timeouts are wrapped in try/except asyncio.TimeoutError so the
  outer non-fatal wrapper in main.py/_background_build_and_upsert is not
  triggered — the build completes (possibly partial) rather than raising.

FIX BUILD-HANG-PER-REQUEST (2026-05-14): Each individual get_option_chain_bulk()
  call inside _build_ticker() is now wrapped in asyncio.wait_for(timeout=45s).
  Without this, a single stalled TCP connection held a semaphore slot for the
  entire gather window. With concurrency=10, a handful of stalled connections
  reduced effective throughput to near zero.

  With per-request timeouts:
  - Each slot is freed within 45s max regardless of Tradier TCP behaviour.
  - Semaphore stays productive at full concurrency=10 throughout.
  - Expected build time: 5-8 min on a clean day, ~10 min on degraded days.
  - BUILD-HANG gather timeout (600s) becomes a true last-resort safety net
    that should never fire under normal operating conditions.

FIX POOL-MISMATCH (2026-05-14): max_connections raised to 75 (for sem=50).
  CONCURRENCY-10 (2026-05-14): reverted to max_connections=30 (3× sem=10).

FIX SHUTDOWN-CANCEL (2026-05-12): _build_with_sem now catches CancelledError
  and re-raises immediately instead of letting it propagate through
  `async with sem:` as an unhandled future exception.

LOG-CHAIN (2026-05-14): Chain-pull progress, per-request timeout, and
  elapsed-time logging added to build() and _build_ticker().

  - _build_with_sem: shared atomic counter logs progress every 250 tickers
    (and at 100% completion) showing count/total/%, contracts accumulated so
    far, and elapsed seconds since chain gather started. Gives real-time
    visibility into cold-start chain pull with zero logic changes.

  - _build_ticker: asyncio.TimeoutError on get_option_chain_bulk() now logs
    at WARNING with ticker + expiry string so stalling tickers are
    identifiable (was silently `continue`-ing with no trace).

  - build(): chain gather phase timed with time.monotonic(). Elapsed seconds
    logged on both clean completion and gather-timeout path.

LOG-CHAIN-V2 (2026-05-14): Granular per-ticker and per-expiry logging.

  - Per-ticker START: logs ticker + tier when semaphore slot is acquired so
    each of the 10 concurrent slots is visible in real time.

  - Per-ticker DONE: logs ticker + elapsed ms + contracts found for that
    ticker immediately after _build_ticker returns.

  - Per-expiry inside _build_ticker: each expiry fetched logs contract count
    so dead expiries (0 contracts) and productive ones are distinguishable.

  - ETA in progress line: every _CHAIN_PROGRESS_INTERVAL tickers the
    progress log includes estimated seconds to completion based on current
    rate (contracts/s).

  - Slot starvation warning: _CHAIN_STALL_WARN_S=10s inner deadline logs
    WARNING with ticker name if a chain fetch exceeds 10s but has not yet
    hit the 45s hard timeout. Implemented via a two-stage wait_for cascade
    in _build_ticker so the full 45s budget is preserved.

FLUSH-PERIODIC (2026-05-14): Flush partial registry to DB every
  _CHAIN_FLUSH_INTERVAL=500 tickers during the gather phase instead of
  waiting until the full build is complete.

  - A background asyncio.Task runs _periodic_flush() alongside the gather.
  - _periodic_flush() wakes every _CHAIN_FLUSH_INTERVAL_S=30s, snapshots
    the current new_registry dict, and calls save_chain() with whatever
    contracts have been fetched so far.
  - The final full save_chain() call in _persist_to_db() is preserved as
    the authoritative complete write; periodic flushes are best-effort
    (errors are logged as WARNING, never raised).
  - On Render cold-start this means contracts start appearing in DB within
    ~30s of build() starting rather than only after the full ~60-120s
    completes. Warm restarts benefit too since the DB snapshot is more
    current if a process is killed mid-build.

CHAIN-ALL (2026-05-14): Switch _build_ticker to single all-expiry call.
  Previously: get_expirations() (call 1) + get_option_chain_bulk() per expiry
  (calls 2..N). For a ticker with 4.5 avg expiries that is 5.5 calls; across
  3,900 tickers = ~21,450 total API calls.

  Now: get_option_chain_bulk_all() (1 call, no expiration param). Tradier
  returns all expiries in one response; each contract carries expiration_date.
  Client-side grouping by expiration_date, then DTE / ATM / OI / type filters
  applied identically to the per-expiry loop. 3,900 total API calls.

  - 82% fewer calls: ~21,450 -> ~3,900
  - ~66-76% faster build: ~343s clean -> ~117s; ~1,287s degraded -> ~312s
  - HTTP 400 terminates the whole ticker (correct: 400 = no listed options)
  - DTE filter moves from pre-fetch to post-fetch (client-side) — no
    behaviour change, minor extra memory per in-flight response (~180KB peak)
  - get_expirations() import removed from symbol_registry; get_option_chain_bulk
    import also removed (still used by chain_store refresh worker via
    tradier_stream path — not symbol_registry).
  - Two-stage stall warning (10s/45s) carried over unchanged.

CONCURRENCY-10 (2026-05-14): Lower _DEFAULT_BUILD_CONCURRENCY from 50 → 10.
  Companion to tradier_client.py CONCURRENCY-10. Reduces Tradier API pressure
  and eliminates rate-limit-induced HTTP 400/429s that were preventing all
  ~3,900 tickers from completing chain fetches. Build time increases modestly
  (~5-8 min clean vs ~2-3 min at concurrency=50) but success rate improves
  from ~130 tickers to full ~3,900 coverage.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

# Module-level imports so patch('services.symbol_registry.*') targets work
# in unit tests (H3 fix - lazy imports inside methods are not patchable via
# the module namespace).
from services.ingestion_config import get_config
from services.tier_engine import _fetch_thresholds, assign_tiers
from services.chain_store import load_chain
from utils.tradier_client import get_option_chain_bulk_all, get_quotes_batch

log = logging.getLogger("symbol_registry")

# CONCURRENCY-10: lowered from 50 → 10 to stay under Tradier's 120 req/min
# rate limit and prevent HTTP 400/429s that blocked full ~3,900 ticker coverage.
# Companion: tradier_client._BULK_CHAIN_SEM=10, max_connections=30.
_DEFAULT_BUILD_CONCURRENCY = 10

# BUILD-HANG: hard timeouts for the two network-bound phases inside build().
_PRICES_FETCH_TIMEOUT_S = 45    # _fetch_stock_prices(): 3949 tickers x 200/batch = 20 batches

# POOL-MISMATCH / BUILD-HANG-PER-REQUEST: 600s gather ceiling.
# Per-request 45s timeouts keep all 10 semaphore slots productive.
# At concurrency=10 with typical Tradier latency (500ms-1.5s/ticker),
# 3949 tickers completes in ~200-600s. 600s is the true last-resort ceiling.
_CHAIN_GATHER_TIMEOUT_S = 600   # asyncio.gather(*tasks): full 3949-ticker universe

# Per-request timeout for each individual get_option_chain_bulk_all() call.
# 45s gives genuine stall headroom on Render cold-start.
# CHAIN-ALL: this now covers the single all-expiry call (previously per-expiry).
_CHAIN_REQUEST_TIMEOUT_S: float = 45.0   # was 30s

# LOG-CHAIN-V2: stall warning threshold — log WARNING if a single ticker
# chain fetch exceeds this many seconds before hitting the hard timeout.
_CHAIN_STALL_WARN_S: float = 10.0

# LOG-CHAIN: log chain-pull progress every N tickers.
_CHAIN_PROGRESS_INTERVAL = 250

# FLUSH-PERIODIC: flush partial registry to DB every N seconds during gather.
# Contracts start appearing in DB ~30s into cold-start instead of only after
# the full build completes (~60-120s).
_CHAIN_FLUSH_INTERVAL_S: int = 30


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
          - epoch == 0  -> registry has never completed a full build().
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
    # -----------------------------------------------------------------------

    def influence_tier_int(self, ticker: str) -> int:
        """
        Return the integer tier (1/2/3) for ticker from _tier_map.
        Fallback: 3 (most conservative) for any ticker not in the map.
        """
        return self._tier_map.get(ticker, 3)

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
            t: round(sum(v) / len(v)) for t, v in oi_acc.items() if v
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
        See module docstring for full change history.
        """
        from services.symbols_loader import SymbolQuote

        cfg, thresh = await asyncio.gather(get_config(), _fetch_thresholds())
        tier_params = _build_tier_params(thresh, global_min_oi=cfg["REGISTRY_MIN_OI"])

        build_concurrency = int(cfg.get("REGISTRY_BUILD_CONCURRENCY", _DEFAULT_BUILD_CONCURRENCY))
        sem = asyncio.Semaphore(build_concurrency)

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

            # FIX-STALE-DTE: carried contracts have DTE values from the previous
            # build. DTE decrements by 1 per day; a contract with DTE=45 yesterday
            # has DTE=44 today. Without this fix, tier routing and the DTE gate
            # use a value that is always ≥1 day stale, which silently mis-slots
            # contracts approaching DTE thresholds (e.g. DTE=1 → carried as DTE=1
            # instead of being evicted as DTE=0). Recompute from date.today() and
            # evict contracts that have now expired (DTE < 0).
            if tickers_to_carry:
                today_date = date.today()
                stale_occ = []
                for occ, meta in new_registry.items():
                    try:
                        exp_date = date.fromisoformat(meta.expiry)
                        fresh_dte = (exp_date - today_date).days
                        if fresh_dte < 0:
                            stale_occ.append(occ)
                        else:
                            meta.dte = fresh_dte
                    except (ValueError, AttributeError):
                        stale_occ.append(occ)
                for occ in stale_occ:
                    del new_registry[occ]
                if stale_occ:
                    log.info(
                        "[symbol_registry] FIX-STALE-DTE: evicted %d now-expired "
                        "carried contracts; %d remain",
                        len(stale_occ), len(new_registry),
                    )

            # BUILD-HANG: hard 45s timeout on _fetch_stock_prices().
            zero_price_fallback = False
            raw_quotes: dict[str, dict] = {}
            try:
                prices, raw_quotes = await asyncio.wait_for(
                    self._fetch_stock_prices(),
                    timeout=_PRICES_FETCH_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                log.error(
                    "[symbol_registry] BUILD-HANG: _fetch_stock_prices() timed out "
                    "after %ds — falling back to zero-price mode so chain fetches "
                    "still run (ATM filter bypassed).",
                    _PRICES_FETCH_TIMEOUT_S,
                )
                prices = {}
                zero_price_fallback = True

            self._stock_prices = prices
            log.info("[symbol_registry] Stock prices fetched: %d tickers", len(prices))

            if tickers_to_refresh and not prices and not zero_price_fallback:
                log.error(
                    "[symbol_registry] B-ZERO-PRICE: _fetch_stock_prices() returned 0 prices "
                    "for %d tickers - Tradier quote API may be down or rate-limited. "
                    "Falling back: ATM filter bypassed so chain fetches still run.",
                    len(tickers_to_refresh),
                )
                zero_price_fallback = True

            if tickers_to_refresh:
                total_tickers = len(tickers_to_refresh)
                # Shared mutable state for progress tracking across concurrent tasks.
                # Lists used so nested closures can mutate via index (nonlocal alternative).
                _completed   = [0]
                _chain_start = time.monotonic()
                # BUILD-METRICS: structured counters for post-build summary log.
                # These replace invisible per-ticker warnings with a single summary
                # that makes degraded builds immediately diagnosable.
                _metric_429s        = [0]   # 429 responses received
                _metric_timeouts    = [0]   # per-ticker hard timeout hits
                _metric_zero_chain  = [0]   # tickers returning 0 contracts
                _metric_errors      = [0]   # other fetch errors

                # ------------------------------------------------------------------
                # FLUSH-PERIODIC: background task that snapshots new_registry every
                # _CHAIN_FLUSH_INTERVAL_S seconds and calls save_chain() so contracts
                # start appearing in DB well before the full build completes.
                #
                # FIX-FLUSH-DELTA: previously did dict(new_registry) — a full
                # shallow copy of up to 50K items every 30s while 10 concurrent
                # coroutines were writing to it. Now tracks the last-flushed
                # registry size and only triggers a flush when new contracts have
                # been added, avoiding redundant full-registry copies.
                # Errors are best-effort — never propagated to the gather.
                # ------------------------------------------------------------------
                _flush_stop = asyncio.Event()
                _last_flush_size = [0]

                async def _periodic_flush(snapshot_id: str) -> None:
                    from services.chain_store import save_chain as _save_chain
                    flush_num = 0
                    while not _flush_stop.is_set():
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(asyncio.ensure_future(_flush_stop.wait())),
                                timeout=_CHAIN_FLUSH_INTERVAL_S,
                            )
                            # Event was set — exit cleanly
                            break
                        except asyncio.TimeoutError:
                            pass  # interval elapsed — do a flush
                        if _flush_stop.is_set():
                            break
                        current_size = len(new_registry)
                        if current_size <= _last_flush_size[0]:
                            # No new contracts since last flush — skip the copy.
                            log.debug(
                                "[symbol_registry] FLUSH-PERIODIC: no new contracts "
                                "since last flush (%d), skipping",
                                current_size,
                            )
                            continue
                        flush_num += 1
                        snapshot = dict(new_registry)
                        if not snapshot:
                            continue
                        try:
                            ok = await _save_chain(snapshot_id, snapshot)
                            _last_flush_size[0] = len(snapshot)
                            log.info(
                                "[symbol_registry] FLUSH-PERIODIC #%d: flushed %d contracts "
                                "to DB mid-build (%d/%d tickers done, ok=%s)",
                                flush_num, len(snapshot),
                                _completed[0], total_tickers, ok,
                            )
                        except Exception as flush_exc:
                            log.warning(
                                "[symbol_registry] FLUSH-PERIODIC #%d: flush failed — %s",
                                flush_num, flush_exc,
                            )
