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
  always doing a full rebuild after the first build()`.\
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
  have been removed (ING-012). The int→string→int round-trip they introduced
  was pure overhead — influence_tier_int() already returns the int directly.
  episode_influence_tier() in composite_signal_engine.py is a separate,
  orthogonal function that classifies episode premium size (WHALE/INSTITUTIONAL/
  LARGE/RETAIL) and is unrelated to symbol tier; it is untouched.

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

  - asyncio.gather(*tasks) for chain fetches: 300s timeout (covers full
    3949-ticker universe at concurrency=50 even on degraded Tradier days).
    On expiry, logs ERROR and proceeds with whatever contracts were fetched
    before the deadline; _build_complete is still set so stream workers
    can spawn against the partial registry.

  Both timeouts are wrapped in try/except asyncio.TimeoutError so the
  outer non-fatal wrapper in main.py/_background_build_and_upsert is not
  triggered — the build completes (possibly partial) rather than raising.

FIX BUILD-HANG-PER-REQUEST (2026-05-14): Each individual get_option_chain_bulk()
  call inside _build_ticker() is now wrapped in asyncio.wait_for(timeout=15s).
  Without this, a single stalled TCP connection held a semaphore slot for the
  entire gather window. With concurrency=50, a handful of stalled connections
  reduced effective throughput to ~5 real requests, completing only ~130
  tickers before the gather timeout fired.

  With per-request timeouts:
  - Each slot is freed within 15s max regardless of Tradier TCP behaviour.
  - Semaphore stays productive at full concurrency=50 throughout.
  - Expected build time: 40-65s on a clean day, ~120s on degraded days.
  - BUILD-HANG gather timeout (300s) becomes a true last-resort safety net
    that should never fire under normal operating conditions.

  Root cause of 2026-05-14 incident: stream spawned with 1570/3949 OCC
  contracts (~40% of tickers, ~2% of full OCC universe) because stalled
  TCP slots made the 180s gather fire after only ~130 alphabetically-first
  tickers completed.

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
"""
import asyncio
import logging
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

_DEFAULT_BUILD_CONCURRENCY = 50

# BUILD-HANG: hard timeouts for the two network-bound phases inside build().
# These prevent an indefinite hang when Tradier stalls at the TCP layer
# before the httpx read timeout fires.
_PRICES_FETCH_TIMEOUT_S = 45    # _fetch_stock_prices(): 3949 tickers × 200/batch = 20 batches

# BUILD-HANG-PER-REQUEST: raised from 180s to 300s.
# Per-request 15s timeouts on each get_option_chain_bulk() call (see
# _build_ticker) keep all 50 semaphore slots productive. At concurrency=50
# with typical Tradier latency (500ms-1.5s/ticker), 3949 tickers completes
# in 40-120s. 300s is the true last-resort ceiling that should never fire
# under normal operating conditions.
_CHAIN_GATHER_TIMEOUT_S = 300   # asyncio.gather(*tasks): full 3949-ticker universe

# Per-request timeout for each individual get_option_chain_bulk() call.
# Frees the semaphore slot immediately on TCP stall — keeps concurrency=50
# slots productive throughout the entire build window.
_CHAIN_REQUEST_TIMEOUT_S = 15


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
        # QQ1-B: use round() instead of floor division so borderline tickers
        # are not silently mis-classified one tier lower.
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
          caused institutional flow on >30-DTE or >±10% ATM contracts to
          be dropped at stream time (lookup() → None).

        BUILD-HANG - hard timeouts on network phases:
          _fetch_stock_prices() is wrapped in asyncio.wait_for(timeout=45s).
          asyncio.gather(*tasks) for chain fetches is wrapped in
          asyncio.wait_for(timeout=300s). Both phases degrade gracefully
          on timeout (zero-price fallback / partial registry) and always
          set _build_complete=True so stream workers can spawn.

        BUILD-HANG-PER-REQUEST - per-request chain timeout:
          Each get_option_chain_bulk() call in _build_ticker() is wrapped
          in asyncio.wait_for(timeout=15s). Stalled TCP connections no
          longer hold semaphore slots — all 50 slots stay productive.

        SHUTDOWN-CANCEL - clean CancelledError propagation:
          _build_with_sem catches CancelledError and re-raises immediately
          so asyncio can retire each gather future without logging it as
          '_GatheringFuture exception was never retrieved'. No behaviour
          change — only shutdown log noise is eliminated.
        """
        from services.symbols_loader import SymbolQuote

        cfg, thresh = await asyncio.gather(get_config(), _fetch_thresholds())
        # QQ1-A: real per-tier params used from the first build.
        # bootstrap_params ({1: T3, 2: T3, 3: T3}) removed — it caused T1
        # tickers to be fetched with T3's narrow atm/DTE window on cold start,
        # silently dropping institutional contracts outside that window.
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

            # BUILD-HANG: hard 45s timeout on _fetch_stock_prices().
            # A TCP-level stall on the Tradier quotes API will not block build()
            # indefinitely. On timeout, degrade to zero_price_fallback=True
            # (existing B-ZERO-PRICE path) so chain fetches still run.
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
                # B-ZERO-PRICE: explicit all-missing case (prices returned empty
                # without timeout). zero_price_fallback already set on timeout above.
                log.error(
                    "[symbol_registry] B-ZERO-PRICE: _fetch_stock_prices() returned 0 prices "
                    "for %d tickers - Tradier quote API may be down or rate-limited. "
                    "Falling back: ATM filter bypassed so chain fetches still run.",
                    len(tickers_to_refresh),
                )
                zero_price_fallback = True

            if tickers_to_refresh:
                async def _build_with_sem(ticker):
                    # SHUTDOWN-CANCEL: catch CancelledError and re-raise immediately
                    # so asyncio can retire the gather future cleanly without logging
                    # '_GatheringFuture exception was never retrieved' on shutdown.
                    try:
                        async with sem:
                            ticker_price = prices.get(ticker, 0.0)
                            await self._build_ticker(
                                ticker,
                                ticker_price,
                                new_registry,
                                new_oi_by_ticker,
                                tier_params,
                                zero_price_fallback=zero_price_fallback,
                            )
                    except asyncio.CancelledError:
                        raise

                tasks = [
                    _build_with_sem(ticker)
                    for ticker in tickers_to_refresh
                ]
                # BUILD-HANG: hard 300s timeout on the chain-fetch gather.
                # Per-request 15s timeouts in _build_ticker keep all semaphore
                # slots productive — this outer timeout is the last-resort
                # safety net that should not fire under normal conditions.
                # On timeout, proceed with whatever contracts were fetched
                # before the deadline; _build_complete is still set.
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True),
                        timeout=_CHAIN_GATHER_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    log.error(
                        "[symbol_registry] BUILD-HANG: chain gather timed out after %ds "
                        "(%d tickers queued). Proceeding with partial registry (%d contracts "
                        "so far) — stream workers will spawn against partial data. "
                        "Next refresh_loop() will complete the missing tickers.",
                        _CHAIN_GATHER_TIMEOUT_S,
                        len(tickers_to_refresh),
                        len(new_registry),
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
            # ING-010-EPOCH: advance epoch so consumers can detect this
            # tier-map refresh without polling individual symbol keys.
            # Incremented here (inside the lock, after _build_complete=True)
            # so any reader that sees epoch N is guaranteed to also see the
            # fully-built _tier_map and _registry for generation N.
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

        B-ZERO-PRICE: when stock_price <= 0:
          - zero_price_fallback=True  -> bypass ATM filter entirely
            (atm_low=0, atm_high=inf). DTE gating still applies.
            Log WARNING and continue; do NOT skip.
          - zero_price_fallback=False -> skip as before (regression guard).

        QQ1-A: tier_params now always carries real per-tier thresholds
          (T1: atm_pct=0.20/max_dte=90, T2: 0.15/60, T3: 0.10/30).
          The former bootstrap_params collapse to T3 for all tiers has
          been removed in build(). _build_ticker() is unchanged here —
          it always read tier_params[tier]; the fix is in the caller.

        BUILD-HANG-PER-REQUEST: each get_option_chain_bulk() call is wrapped
          in asyncio.wait_for(timeout=_CHAIN_REQUEST_TIMEOUT_S=15s).
          A stalled TCP connection frees its semaphore slot within 15s
          instead of holding it for the full gather window. This keeps
          all concurrency=50 slots productive throughout the build.
          Timeout is logged at WARNING and the expiry is skipped (same
          behaviour as a failed chain fetch).
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

            # BUILD-HANG-PER-REQUEST: wrap each chain fetch in a 15s deadline.
            # Without this, a stalled TCP connection holds the semaphore slot
            # for the entire outer gather window (300s), starving other tickers.
            # On timeout: log WARNING and skip this expiry (same as a network
            # error). The ticker's other expiries are still attempted.
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
            # QQ1-B: round() instead of floor division so borderline tickers
            # are not silently mis-classified one tier lower by truncation.
            oi_by_ticker[ticker] = round(total_oi / count)


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
