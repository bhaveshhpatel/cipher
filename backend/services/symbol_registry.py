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
  build guard is now `if self._registry:` — the populated registry itself is
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
  fresh-Tradier registry — never a partially-seeded DB snapshot.

FIX M-3 (2026-04-28): _post_build_upsert is split into two separately
  guarded phases. assign_tiers() failure is caught and re-raised so
  upsert_symbol_quotes() is skipped (was silently swallowed). A dedicated
  error counter and warning log make the failure visible without taking down
  the process. The outer non-fatal wrapper in main.py still protects the
  background task but now sees the raised exception.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

# Module-level imports so patch('services.symbol_registry.*') targets work
# in unit tests (H3 fix — lazy imports inside methods are not patchable via
# the module namespace).
from services.ingestion_config import get_config
from services.tier_engine import _fetch_thresholds, assign_tiers
from services.chain_store import load_chain
from utils.tradier_client import get_expirations, get_option_chain_bulk, get_quotes_batch

log = logging.getLogger("symbol_registry")

_DEFAULT_BUILD_CONCURRENCY = 50


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
        # load_from_db() does NOT set this — it only populates the registry
        # with stale DB data. build() sets it at the very end, inside the
        # lock, after self._registry is swapped to the freshly-built data.
        # stream_options_flow() polls is_ready() which checks this flag.
        self._build_complete: bool = False
        # H3: _seeded_from_db removed — self._registry itself is the guard

    def lookup(self, occ_symbol: str) -> Optional[ContractMeta]:
        return self._registry.get(occ_symbol.strip())

    def all_symbols(self) -> list[str]:
        return list(self._registry.keys())

    def size(self) -> int:
        return len(self._registry)

    def stock_price(self, ticker: str) -> float:
        return self._stock_prices.get(ticker, 0.0)

    def is_ready(self) -> bool:
        # M-2: return _build_complete, NOT len(self._registry) > 0.
        # The registry may be non-empty from load_from_db() (stale DB seed)
        # long before build() has finished fetching fresh Tradier data.
        # Returning True too early causes stream workers to spawn against
        # a partially-seeded registry, leading to the M-1 worker-count
        # mismatch (all_symbols() called before build() completes).
        return self._build_complete

    def set_tier_map(self, tier_map: dict[str, int]) -> None:
        self._tier_map = tier_map

    def get_oi_map(self) -> dict[str, int]:
        return dict(self._oi_by_ticker)

    async def load_from_db(self, snapshot_id: str) -> int:
        chain = await load_chain(snapshot_id)
        if chain is None:
            log.info(
                "[symbol_registry] load_from_db: DB error for snapshot %s — "
                "skipping pre-seed, full build() will populate registry",
                snapshot_id,
            )
            return 0
        if not chain:
            log.info(
                "[symbol_registry] load_from_db: no cached chain for snapshot %s "
                "(including fallback) — will do full build from Tradier",
                snapshot_id,
            )
            return 0
        self._registry = chain
        self._persisted_snapshot_id = snapshot_id
        # M-1/M-2: do NOT set _build_complete here.
        # The chain is stale DB data — stream workers must not unblock yet.
        # H3: no _seeded_from_db flag — populated registry is the signal
        # Rebuild OI map from the loaded chain
        oi_acc: dict[str, list[int]] = {}
        for meta in chain.values():
            oi_acc.setdefault(meta.ticker, []).append(meta.open_interest)
        self._oi_by_ticker = {
            t: int(sum(v) / len(v)) for t, v in oi_acc.items() if v
        }
        log.info(
            "[symbol_registry] load_from_db: seeded %d OCC contracts from DB "
            "(snapshot %s, oi_map=%d tickers) — waiting for build() to set "
            "_build_complete before stream workers are allowed to spawn",
            len(chain), snapshot_id, len(self._oi_by_ticker),
        )
        return len(chain)

    async def build(self) -> tuple[int, dict[str, dict]]:
        """
        Build (or incrementally refresh) the OCC registry.

        H3 — Incremental mode (fixed):
          If self._registry is already populated (seeded from DB or from a
          prior build()), skip tickers whose minimum DTE is > 0.
          Only re-fetch tickers that have expired contracts (min_dte == 0)
          or are missing from the registry entirely.

        H1 — Return raw_quotes:
          Returns tuple[int, dict[str, dict]] so callers can reuse the
          already-fetched quote data and skip a duplicate Tradier call.

        C-3 — require_oi=True for post-build reclassification:
          assign_tiers() is called with require_oi=True so OI gates are
          enforced only after build() has populated oi_by_ticker from
          chain fetches.

        M-1/M-2 — _build_complete flag:
          self._build_complete is set to True at the very end of this
          method, inside the lock, after self._registry is swapped.
          is_ready() returns self._build_complete, so stream workers will
          not spawn until build() has fully completed with fresh data.
        """
        from services.symbols_loader import SymbolQuote

        cfg, thresh = await asyncio.gather(get_config(), _fetch_thresholds())
        tier_params      = _build_tier_params(thresh, global_min_oi=cfg["REGISTRY_MIN_OI"])
        bootstrap_params = {1: tier_params[3], 2: tier_params[3], 3: tier_params[3]}

        build_concurrency = int(cfg.get("REGISTRY_BUILD_CONCURRENCY", _DEFAULT_BUILD_CONCURRENCY))
        sem = asyncio.Semaphore(build_concurrency)

        async with self._build_lock:
            # H3 fix: use populated registry as the incremental guard
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

            prices, raw_quotes = await self._fetch_stock_prices()
            self._stock_prices = prices
            log.info("[symbol_registry] Stock prices fetched: %d tickers", len(prices))

            if tickers_to_refresh:
                async def _build_with_sem(ticker):
                    async with sem:
                        await self._build_ticker(
                            ticker,
                            prices.get(ticker, 0.0),
                            new_registry,
                            new_oi_by_ticker,
                            bootstrap_params,
                        )

                tasks = [
                    _build_with_sem(ticker)
                    for ticker in tickers_to_refresh
                    if ticker in prices and prices[ticker] > 0
                ]
                await asyncio.gather(*tasks, return_exceptions=True)

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

            # C-3: require_oi=True — OI gate enforced now that build() has
            # populated new_oi_by_ticker from chain fetches.
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

            # M-1/M-2: set _build_complete AFTER self._registry is swapped.
            self._build_complete = True

            t_counts = {1: 0, 2: 0, 3: 0}
            for m in new_registry.values():
                t_counts[m.tier] = t_counts.get(m.tier, 0) + 1
            log.info(
                "[symbol_registry] Build complete: %d OCC symbols "
                "(T1=%d T2=%d T3=%d) (was %d, delta=%+d) | OI map: %d tickers "
                "| _build_complete=True — stream workers may now spawn",
                len(new_registry),
                t_counts[1], t_counts[2], t_counts[3],
                old_count, len(new_registry) - old_count,
                len(new_oi_by_ticker),
            )

            await self._persist_to_db(new_registry)
            return len(new_registry), raw_quotes  # H1: return raw_quotes

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
        ticker:          str,
        stock_price:     float,
        registry:        dict[str, ContractMeta],
        oi_by_ticker:    dict[str, int],
        tier_params:     dict[int, _TierParams],
    ):
        if stock_price <= 0:
            log.warning("[symbol_registry] %s: no stock price — skipping", ticker)
            return

        tier   = self._tier_map.get(ticker, 3)
        params = tier_params.get(tier) or tier_params[3]

        try:
            expirations = await get_expirations(ticker)
        except Exception as e:
            log.warning("[symbol_registry] %s: expirations fetch failed: %s", ticker, e)
            return

        today    = date.today()
        atm_low  = stock_price * (1 - params.atm_pct)
        atm_high = stock_price * (1 + params.atm_pct)

        for expiry_str in expirations:
            try:
                exp_date = date.fromisoformat(expiry_str)
            except ValueError:
                continue
            dte = (exp_date - today).days
            if dte < 0 or dte > params.max_dte:
                continue

            try:
                contracts = await get_option_chain_bulk(ticker, expiry_str)
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
