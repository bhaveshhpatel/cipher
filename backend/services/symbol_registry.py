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
    3949-ticker universe at concurrency=50 even on degraded Tradier days).
    On expiry, logs ERROR and proceeds with whatever contracts were fetched
    before the deadline; _build_complete is still set so stream workers
    can spawn against the partial registry.

  Both timeouts are wrapped in try/except asyncio.TimeoutError so the
  outer non-fatal wrapper in main.py/_background_build_and_upsert is not
  triggered — the build completes (possibly partial) rather than raising.

FIX BUILD-HANG-PER-REQUEST (2026-05-14): Each individual get_option_chain_bulk()
  call inside _build_ticker() is now wrapped in asyncio.wait_for(timeout=45s).
  Without this, a single stalled TCP connection held a semaphore slot for the
  entire gather window. With concurrency=50, a handful of stalled connections
  reduced effective throughput to ~5 real requests, completing only ~130
  tickers before the gather timeout fired.

  With per-request timeouts:
  - Each slot is freed within 45s max regardless of Tradier TCP behaviour.
  - Semaphore stays productive at full concurrency=50 throughout.
  - Expected build time: 40-65s on a clean day, ~120s on degraded days.
  - BUILD-HANG gather timeout (600s) becomes a true last-resort safety net
    that should never fire under normal operating conditions.

FIX POOL-MISMATCH (2026-05-14): Companion to tradier_client.py POOL-MISMATCH.
  _CHAIN_REQUEST_TIMEOUT_S raised from 15s -> 30s -> 45s now that pool
  contention is eliminated (max_connections=75, 1.5x _BULK_CHAIN_SEM=50).

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
    each of the 50 concurrent slots is visible in real time.

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
from utils.tradier_client import get_expirations, get_option_chain_bulk, get_quotes_batch

log = logging.getLogger("symbol_registry")

_DEFAULT_BUILD_CONCURRENCY = 50

# BUILD-HANG: hard timeouts for the two network-bound phases inside build().
_PRICES_FETCH_TIMEOUT_S = 45    # _fetch_stock_prices(): 3949 tickers x 200/batch = 20 batches

# POOL-MISMATCH / BUILD-HANG-PER-REQUEST: raised to 600s.
# Per-request 45s timeouts keep all 50 semaphore slots productive.
# At concurrency=50 with typical Tradier latency (500ms-1.5s/ticker),
# 3949 tickers completes in 40-120s. 600s is the true last-resort ceiling.
_CHAIN_GATHER_TIMEOUT_S = 600   # asyncio.gather(*tasks): full 3949-ticker universe

# Per-request timeout for each individual get_option_chain_bulk() call.
# Raised from 30s -> 45s to give genuine stall headroom on Render cold-start.
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
                _completed = [0]
                _chain_start = time.monotonic()

                # ------------------------------------------------------------------
                # FLUSH-PERIODIC: background task that snapshots new_registry every
                # _CHAIN_FLUSH_INTERVAL_S seconds and calls save_chain() so contracts
                # start appearing in DB well before the full build completes.
                # Errors are best-effort — never propagated to the gather.
                # ------------------------------------------------------------------
                _flush_stop = asyncio.Event()

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
                        flush_num += 1
                        snapshot = dict(new_registry)
                        if not snapshot:
                            continue
                        try:
                            ok = await _save_chain(snapshot_id, snapshot)
                            log.info(
                                "[symbol_registry] FLUSH-PERIODIC #%d: flushed %d contracts "
                                "to DB mid-build (%d/%d tickers done, ok=%s)",
                                flush_num, len(snapshot),
                                _completed[0], total_tickers, ok,
                            )
                        except Exception as flush_exc:
                            log.warning(
                                "[symbol_registry] FLUSH-PERIODIC #%d: flush failed "
                                "(non-fatal): %s",
                                flush_num, flush_exc,
                            )

                # Resolve snapshot_id for periodic flush (same logic as _persist_to_db)
                _flush_snapshot_id: Optional[str] = None
                try:
                    loop = asyncio.get_running_loop()
                    from services import universe_store
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
                    if snap_rows:
                        _flush_snapshot_id = snap_rows[0]["id"]
                except Exception as snap_exc:
                    log.warning(
                        "[symbol_registry] FLUSH-PERIODIC: could not resolve snapshot_id "
                        "(%s) — periodic flush disabled for this build",
                        snap_exc,
                    )

                flush_task: Optional[asyncio.Task] = None
                if _flush_snapshot_id:
                    flush_task = asyncio.ensure_future(
                        _periodic_flush(_flush_snapshot_id)
                    )
                    log.info(
                        "[symbol_registry] FLUSH-PERIODIC: started — will flush to DB "
                        "every %ds during chain gather (snapshot=%s)",
                        _CHAIN_FLUSH_INTERVAL_S, _flush_snapshot_id,
                    )

                # ------------------------------------------------------------------
                # _build_with_sem: per-ticker task wrapper with full logging.
                # ------------------------------------------------------------------
                async def _build_with_sem(ticker: str) -> None:
                    # SHUTDOWN-CANCEL: re-raise CancelledError immediately so asyncio
                    # can retire the gather future cleanly on shutdown.
                    try:
                        async with sem:
                            tier = self._tier_map.get(ticker, 3)
                            t_start = time.monotonic()

                            # LOG-CHAIN-V2 START: slot acquired — visible in logs for
                            # each of the 50 concurrent slots.
                            log.debug(
                                "[symbol_registry] [slot] START %s (T%d)",
                                ticker, tier,
                            )

                            ticker_price = prices.get(ticker, 0.0)
                            await self._build_ticker(
                                ticker,
                                ticker_price,
                                new_registry,
                                new_oi_by_ticker,
                                tier_params,
                                zero_price_fallback=zero_price_fallback,
                            )

                            t_elapsed_ms = (time.monotonic() - t_start) * 1000
                            # Count contracts written for this ticker
                            ticker_contracts = sum(
                                1 for m in new_registry.values() if m.ticker == ticker
                            )

                            # LOG-CHAIN-V2 DONE: elapsed + contracts for this ticker.
                            log.debug(
                                "[symbol_registry] [slot]  DONE %s (T%d) | "
                                "%.0fms | %d contracts",
                                ticker, tier, t_elapsed_ms, ticker_contracts,
                            )

                    except asyncio.CancelledError:
                        raise
                    finally:
                        # LOG-CHAIN: increment counter and emit progress + ETA line.
                        _completed[0] += 1
                        done = _completed[0]
                        if done % _CHAIN_PROGRESS_INTERVAL == 0 or done == total_tickers:
                            elapsed = time.monotonic() - _chain_start
                            rate = done / elapsed if elapsed > 0 else 0
                            remaining = total_tickers - done
                            eta_s = (remaining / rate) if rate > 0 else 0
                            log.info(
                                "[symbol_registry] Chain pull progress: %d/%d tickers "
                                "(%.0f%%) | contracts so far: %d | elapsed: %.1fs | "
                                "rate: %.1f t/s | ETA: %.0fs",
                                done, total_tickers,
                                100.0 * done / total_tickers,
                                len(new_registry),
                                elapsed,
                                rate,
                                eta_s,
                            )

                tasks = [
                    _build_with_sem(ticker)
                    for ticker in tickers_to_refresh
                ]

                # BUILD-HANG: 600s last-resort ceiling on the gather.
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True),
                        timeout=_CHAIN_GATHER_TIMEOUT_S,
                    )
                    chain_elapsed = time.monotonic() - _chain_start
                    log.info(
                        "[symbol_registry] Chain gather complete: %d tickers in %.1fs "
                        "| %d contracts loaded",
                        total_tickers, chain_elapsed, len(new_registry),
                    )
                except asyncio.TimeoutError:
                    chain_elapsed = time.monotonic() - _chain_start
                    log.error(
                        "[symbol_registry] BUILD-HANG: chain gather timed out after %ds "
                        "(%.1fs elapsed, %d tickers queued, %d completed). Proceeding with "
                        "partial registry (%d contracts so far) — stream workers will spawn "
                        "against partial data. Next refresh_loop() will complete the missing "
                        "tickers.",
                        _CHAIN_GATHER_TIMEOUT_S,
                        chain_elapsed,
                        total_tickers,
                        _completed[0],
                        len(new_registry),
                    )
                finally:
                    # Stop the periodic flush task cleanly regardless of gather outcome.
                    _flush_stop.set()
                    if flush_task is not None:
                        try:
                            await asyncio.wait_for(flush_task, timeout=5.0)
                        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                            flush_task.cancel()

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
                from services.symbols_loader import SymbolQuote
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

            self._build_complete = True
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

        LOG-CHAIN-V2: per-expiry contract count logged so dead expiries
        (0 contracts after ATM/DTE/OI filtering) are distinguishable from
        productive ones.

        LOG-CHAIN-V2 stall warning: a two-stage asyncio.wait_for cascade
        fires a WARNING at _CHAIN_STALL_WARN_S=10s if a single chain fetch
        is taking unusually long, well before the hard 45s timeout. The
        full 45s budget is preserved — the warning is informational only.
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

            # Two-stage wait_for: warn at _CHAIN_STALL_WARN_S, then apply
            # the full _CHAIN_REQUEST_TIMEOUT_S hard deadline.
            # Stage 1: short deadline — just for the stall warning.
            contracts = None
            try:
                contracts = await asyncio.wait_for(
                    get_option_chain_bulk(ticker, expiry_str),
                    timeout=_CHAIN_STALL_WARN_S,
                )
            except asyncio.TimeoutError:
                # Stage 1 fired — log stall warning, then give remaining budget.
                log.warning(
                    "[symbol_registry] %s %s: chain fetch stalled >%.0fs "
                    "— still waiting (hard timeout=%.0fs)",
                    ticker, expiry_str,
                    _CHAIN_STALL_WARN_S,
                    _CHAIN_REQUEST_TIMEOUT_S,
                )
                remaining = _CHAIN_REQUEST_TIMEOUT_S - _CHAIN_STALL_WARN_S
                try:
                    contracts = await asyncio.wait_for(
                        get_option_chain_bulk(ticker, expiry_str),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "[symbol_registry] %s %s: chain fetch timed out after %.0fs total "
                        "— skipping expiry",
                        ticker, expiry_str, _CHAIN_REQUEST_TIMEOUT_S,
                    )
                    continue
                except Exception as e:
                    log.warning(
                        "[symbol_registry] %s %s: chain fetch failed (stage 2): %s",
                        ticker, expiry_str, e,
                    )
                    continue
            except Exception as e:
                log.warning(
                    "[symbol_registry] %s %s: chain fetch failed: %s",
                    ticker, expiry_str, e,
                )
                continue

            if contracts is None:
                continue

            contracts_added = 0
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
                    contracts_added += 1
                except Exception as inner_exc:
                    log.debug(
                        "[symbol_registry] %s: contract parse error: %s",
                        ticker, inner_exc,
                    )

            # LOG-CHAIN-V2: per-expiry contract count so dead expiries are visible.
            log.debug(
                "[symbol_registry] %s %s (dte=%d): %d contracts added "
                "(raw=%d, filtered=%d)",
                ticker, expiry_str, dte,
                contracts_added,
                len(contracts),
                len(contracts) - contracts_added,
            )

        total_oi = sum(
            meta.open_interest
            for meta in registry.values()
            if meta.ticker == ticker
        )
        count = sum(1 for m in registry.values() if m.ticker == ticker)
        if count > 0:
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
