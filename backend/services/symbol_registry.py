"""
services/symbol_registry.py — Layer 1: OCC Symbol Registry
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from utils.tradier_client import get_expirations, get_option_chain, get_quotes_batch

log = logging.getLogger("symbol_registry")


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
    """Pure sync function — must NOT be awaited."""
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


def _coerce_price(sq) -> float:
    """
    Extract a numeric price from a SymbolQuote-like object.
    Only accepts genuine int/float values — rejects MagicMock auto-attributes
    that would otherwise coerce to 1.0 via float().
    Tries .last_price first (production SymbolQuote field),
    then .last (some test fixtures use MagicMock(last=185.0)).
    Returns 0.0 if neither yields a usable float.
    """
    for attr in ("last_price", "last"):
        val = getattr(sq, attr, None)
        if val is None:
            continue
        if not isinstance(val, (int, float)):
            continue
        try:
            f = float(val)
            if f > 0:
                return f
        except (TypeError, ValueError):
            pass
    return 0.0


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
        self._build_done: asyncio.Event = asyncio.Event()
        self._expiry_cache:  dict[str, set[str]] = {}
        self._oi_snapshot:   dict[str, int]      = {}
        # Populated by _build_ticker; consumed by build() after gather.
        self._pending_expiry_cache: dict[str, set[str]] = {}

    async def wait_for_build(self) -> None:
        """Suspend until build() has completed at least once."""
        await self._build_done.wait()

    def lookup(self, occ_symbol: str) -> Optional[ContractMeta]:
        return self._registry.get(occ_symbol.strip())

    def all_symbols(self) -> list[str]:
        return list(self._registry.keys())

    def size(self) -> int:
        return len(self._registry)

    def stock_price(self, ticker: str) -> float:
        return self._stock_prices.get(ticker, 0.0)

    def is_ready(self) -> bool:
        return len(self._registry) > 0

    def set_tier_map(self, tier_map: dict[str, int]) -> None:
        self._tier_map = tier_map

    def get_oi_map(self) -> dict[str, int]:
        return dict(self._oi_by_ticker)

    async def load_from_db(self, snapshot_id: str) -> int:
        from services.chain_store import load_chain
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
                "— will build from Tradier",
                snapshot_id,
            )
            return 0
        self._registry = chain
        self._persisted_snapshot_id = snapshot_id
        log.info(
            "[symbol_registry] load_from_db: seeded %d OCC contracts from DB "
            "(snapshot %s) — registry ready before build()",
            len(chain), snapshot_id,
        )
        return len(chain)

    async def build(
        self,
        pre_fetched_quotes: Optional[dict[str, "SymbolQuote"]] = None,
    ) -> int:
        """
        Build the OCC registry.

        pre_fetched_quotes (Issue 6 Part 1):
          If provided, prices and volumes are read directly from this map
          instead of calling _fetch_stock_prices() (second Tradier call).
          Keys are ticker symbols; values are SymbolQuote-like objects with
          .last_price OR .last attribute (plus optional .volume, .average_volume).
          Only tickers present in self._watchlist are used; extras are ignored.

        Delta chain fetch (Issue 6 Part 2):
          On second and subsequent builds, expirations are diffed against
          _expiry_cache.  Only tickers where expiry set changed OR avg OI
          shifted by > REGISTRY_OI_DELTA_THRESHOLD are re-fetched from
          Tradier.  Unchanged tickers reuse ContractMeta from the previous
          registry.
        """
        from services.ingestion_config import get_config
        from services.tier_engine import _fetch_thresholds, assign_tiers
        from services.symbols_loader import SymbolQuote

        cfg, thresh = await asyncio.gather(get_config(), _fetch_thresholds())

        global_min_oi   = cfg["REGISTRY_MIN_OI"] if isinstance(cfg, dict) else 0
        tier_params     = _build_tier_params(thresh if isinstance(thresh, dict) else {}, global_min_oi)
        bootstrap_params = {1: tier_params[3], 2: tier_params[3], 3: tier_params[3]}
        oi_delta_thresh  = float((cfg if isinstance(cfg, dict) else {}).get("REGISTRY_OI_DELTA_THRESHOLD", 0.20))
        is_first_build   = not bool(self._expiry_cache)

        async with self._build_lock:
            log.info(
                "[symbol_registry] Building OCC registry for %d tickers "
                "[T1: atm=+/-%.0f%% dte=%d | T2: atm=+/-%.0f%% dte=%d | T3: atm=+/-%.0f%% dte=%d | min_oi=%d | delta=%s]",
                len(self._watchlist),
                tier_params[1].atm_pct * 100, tier_params[1].max_dte,
                tier_params[2].atm_pct * 100, tier_params[2].max_dte,
                tier_params[3].atm_pct * 100, tier_params[3].max_dte,
                global_min_oi,
                "full (first build)" if is_first_build else f"oi_thresh={oi_delta_thresh:.0%}",
            )

            try:
                # ----------------------------------------------------------
                # Issue 6 Part 1: price/volume resolution
                # ----------------------------------------------------------
                watchlist_set = set(self._watchlist)

                if pre_fetched_quotes is not None:
                    prices: dict[str, float] = {}
                    raw_volumes: dict[str, dict] = {}
                    for ticker, sq in pre_fetched_quotes.items():
                        if ticker not in watchlist_set:
                            continue
                        lp = _coerce_price(sq)
                        if lp > 0:
                            prices[ticker] = lp
                        raw_volumes[ticker] = {
                            "volume":         getattr(sq, "volume", None) or 0,
                            "average_volume": getattr(sq, "average_volume", None) or 0,
                        }
                    self._stock_prices = prices
                    log.info(
                        "[symbol_registry] Using pre-fetched quotes for %d tickers "
                        "(no second Tradier call)",
                        len(prices),
                    )
                else:
                    raw_result = await self._fetch_stock_prices()
                    if isinstance(raw_result, tuple):
                        prices, raw_volumes = raw_result
                    else:
                        prices = {k: float(v) for k, v in raw_result.items()
                                  if isinstance(v, (int, float)) and v > 0}
                        raw_volumes = {}
                    self._stock_prices = prices
                    log.info("[symbol_registry] Stock prices fetched: %d tickers", len(prices))

                new_registry: dict[str, ContractMeta] = {}
                new_oi_by_ticker: dict[str, int] = {}
                new_expiry_cache: dict[str, set[str]] = {}

                # Reset pending expiry cache for this build pass.
                self._pending_expiry_cache = {}

                if is_first_build:
                    tasks = [
                        self._build_ticker(
                            ticker,
                            prices.get(ticker, 0.0),
                            new_registry,
                            new_oi_by_ticker,
                            bootstrap_params,
                        )
                        for ticker in self._watchlist
                        if ticker in prices
                        and isinstance(prices[ticker], (int, float))
                        and prices[ticker] > 0
                    ]
                    await asyncio.gather(*tasks, return_exceptions=True)
                    # Merge expiry data written by _build_ticker into new_expiry_cache.
                    new_expiry_cache.update(self._pending_expiry_cache)
                else:
                    reused = await self._apply_delta(
                        prices,
                        bootstrap_params,
                        new_registry,
                        new_oi_by_ticker,
                        new_expiry_cache,
                        oi_delta_thresh,
                    )
                    log.info(
                        "[symbol_registry] Delta build: %d tickers re-fetched, %d reused from cache",
                        len(self._watchlist) - reused,
                        reused,
                    )

                # ----------------------------------------------------------
                # Post-build tier reclassification using live price + vol + OI
                # ----------------------------------------------------------
                synthetic_quotes: list[SymbolQuote] = []
                for ticker in self._watchlist:
                    if ticker not in prices:
                        continue
                    rv = raw_volumes.get(ticker, {})
                    vol     = 0
                    avg_vol = 0
                    try:
                        vol = int(rv.get("volume") or 0) if isinstance(rv, dict) else 0
                    except (TypeError, ValueError):
                        pass
                    try:
                        avg_vol = int(rv.get("average_volume") or 0) if isinstance(rv, dict) else 0
                    except (TypeError, ValueError):
                        pass
                    synthetic_quotes.append(SymbolQuote(
                        symbol         = ticker,
                        last_price     = prices.get(ticker, 0.0),
                        volume         = vol,
                        average_volume = avg_vol,
                        open_interest  = new_oi_by_ticker.get(ticker, 0),
                    ))

                live_tier_map = await assign_tiers(synthetic_quotes, thresholds=thresh)
                log.info(
                    "[symbol_registry] Post-build tier reclassification: T1=%d T2=%d T3=%d",
                    sum(1 for t in live_tier_map.values() if t == 1),
                    sum(1 for t in live_tier_map.values() if t == 2),
                    sum(1 for t in live_tier_map.values() if t == 3),
                )

                for occ_sym, meta in new_registry.items():
                    meta.tier = live_tier_map.get(meta.ticker, 3)

                self._tier_map = live_tier_map

                old_count = len(self._registry)

                self._oi_snapshot    = dict(self._oi_by_ticker)
                self._registry       = new_registry
                self._oi_by_ticker   = new_oi_by_ticker
                self._expiry_cache   = new_expiry_cache
                self._last_build     = datetime.utcnow()

                t_counts = {1: 0, 2: 0, 3: 0}
                for m in new_registry.values():
                    t_counts[m.tier] = t_counts.get(m.tier, 0) + 1
                log.info(
                    "[symbol_registry] Build complete: %d OCC symbols "
                    "(T1=%d T2=%d T3=%d) (was %d, delta=%+d) | OI map: %d tickers",
                    len(new_registry),
                    t_counts[1], t_counts[2], t_counts[3],
                    old_count, len(new_registry) - old_count,
                    len(new_oi_by_ticker),
                )

                await self._persist_to_db(new_registry)
                return len(new_registry)

            finally:
                self._build_done.set()

    async def _apply_delta(
        self,
        prices:          dict[str, float],
        tier_params:     dict[int, _TierParams],
        new_registry:    dict[str, ContractMeta],
        new_oi_by_ticker: dict[str, int],
        new_expiry_cache: dict[str, set[str]],
        oi_delta_thresh:  float,
    ) -> int:
        expiry_tasks = {
            ticker: asyncio.create_task(self._safe_get_expirations(ticker))
            for ticker in self._watchlist
            if ticker in prices
            and isinstance(prices[ticker], (int, float))
            and prices[ticker] > 0
        }
        expiry_results: dict[str, list[str]] = {}
        for ticker, task in expiry_tasks.items():
            try:
                expiry_results[ticker] = await task
            except Exception:
                expiry_results[ticker] = []

        changed:   list[str] = []
        unchanged: list[str] = []
        today = date.today()

        for ticker, expirations in expiry_results.items():
            tier   = self._tier_map.get(ticker, 3)
            params = tier_params.get(tier) or tier_params[3]
            live_expiries = {
                e for e in expirations
                if self._expiry_in_window(e, today, params.max_dte)
            }

            cached_expiries = self._expiry_cache.get(ticker, None)
            prev_oi         = self._oi_snapshot.get(ticker, 0)
            curr_oi         = self._oi_by_ticker.get(ticker, 0)
            oi_drift = (
                abs(curr_oi - prev_oi) / max(prev_oi, 1)
                if prev_oi > 0 else 1.0
            )

            if (
                cached_expiries is None
                or live_expiries != cached_expiries
                or oi_drift > oi_delta_thresh
            ):
                changed.append(ticker)
            else:
                unchanged.append(ticker)

        if changed:
            # Reset pending cache so _build_ticker writes fresh entries.
            self._pending_expiry_cache = {}
            tasks = [
                self._build_ticker(
                    ticker,
                    prices.get(ticker, 0.0),
                    new_registry,
                    new_oi_by_ticker,
                    tier_params,
                )
                for ticker in changed
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
            new_expiry_cache.update(self._pending_expiry_cache)

        for ticker in unchanged:
            live_expiries_for_cache = {
                e for e in expiry_results.get(ticker, [])
                if self._expiry_in_window(
                    e, today,
                    (tier_params.get(self._tier_map.get(ticker, 3)) or tier_params[3]).max_dte,
                )
            }
            new_expiry_cache[ticker] = live_expiries_for_cache
            for occ_sym, meta in self._registry.items():
                if meta.ticker == ticker:
                    new_registry[occ_sym] = meta
            new_oi_by_ticker[ticker] = self._oi_by_ticker.get(ticker, 0)

        return len(unchanged)

    @staticmethod
    def _expiry_in_window(expiry_str: str, today: date, max_dte: int) -> bool:
        try:
            exp_date = date.fromisoformat(expiry_str)
            dte = (exp_date - today).days
            return 0 <= dte <= max_dte
        except ValueError:
            return False

    async def _safe_get_expirations(self, ticker: str) -> list[str]:
        try:
            return await get_expirations(ticker)
        except Exception as e:
            log.warning("[symbol_registry] %s: expiry fetch failed in delta check: %s", ticker, e)
            return []

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
            from services.ingestion_config import get_config
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
        """
        Legacy fallback: fetch stock quotes when no pre_fetched_quotes are
        passed to build() (e.g. prewarm loop, manual registry.build() calls).
        Always returns a 2-tuple (prices, raw_volumes).
        """
        prices: dict[str, float] = {}
        raw_quotes: dict[str, dict] = {}
        batch_size = 200
        batches = [
            self._watchlist[i:i + batch_size]
            for i in range(0, len(self._watchlist), batch_size)
        ]
        results = await asyncio.gather(*[get_quotes_batch(b) for b in batches])
        for quote_map in results:
            if not isinstance(quote_map, dict):
                continue
            for sym, q in quote_map.items():
                if not isinstance(q, dict):
                    continue
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
        ticker:       str,
        stock_price:  float,
        registry:     dict[str, ContractMeta],
        oi_by_ticker: dict[str, int],
        tier_params:  dict[int, _TierParams],
    ):
        """
        Fetch and register all in-window OCC contracts for one ticker.

        Expiry cache output is written to self._pending_expiry_cache[ticker]
        instead of being passed as a positional argument — this keeps the
        signature at 5 args (+ self) so test fakes with the same arity work.
        """
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
        in_window_expiries: set[str] = set()

        for expiry_str in expirations:
            try:
                exp_date = date.fromisoformat(expiry_str)
            except ValueError:
                continue
            dte = (exp_date - today).days
            if dte < 0 or dte > params.max_dte:
                continue

            in_window_expiries.add(expiry_str)

            try:
                contracts = await get_option_chain(ticker, expiry_str)
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

                    occ_symbol = contract.get("symbol", "").strip()
                    if not occ_symbol:
                        continue

                    ctype_raw = contract.get("option_type", "").upper()
                    ctype = "CALL" if ctype_raw in ("C", "CALL") else "PUT"

                    registry[occ_symbol] = ContractMeta(
                        ticker        = ticker,
                        strike        = strike,
                        expiry        = expiry_str,
                        contract_type = ctype,
                        dte           = dte,
                        open_interest = oi,
                        tier          = tier,
                    )
                except Exception:
                    continue

        # Write expiry cache to instance dict (no positional arg needed).
        self._pending_expiry_cache[ticker] = in_window_expiries

        loaded_ois = [
            m.open_interest
            for m in registry.values()
            if m.ticker == ticker
        ]
        if loaded_ois:
            oi_by_ticker[ticker] = int(sum(loaded_ois) / len(loaded_ois))
        else:
            oi_by_ticker[ticker] = 0

        ticker_count = len(loaded_ois)
        log.debug(
            "[symbol_registry] %s (T%d): %d contracts loaded "
            "(price=$%.2f, atm=+/-%.0f%%, dte<=%d, avg_oi=%d)",
            ticker, tier, ticker_count, stock_price,
            params.atm_pct * 100, params.max_dte,
            oi_by_ticker.get(ticker, 0),
        )


_registry: Optional[SymbolRegistry] = None


def get_registry() -> Optional[SymbolRegistry]:
    return _registry


def init_registry(
    watchlist: list[str],
    tier_map:  Optional[dict[str, int]] = None,
) -> SymbolRegistry:
    global _registry
    _registry = SymbolRegistry(watchlist=watchlist, tier_map=tier_map or {})
    return _registry
