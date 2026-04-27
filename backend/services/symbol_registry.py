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
        # snapshot_id of the last successfully persisted build
        self._persisted_snapshot_id: Optional[str] = None

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
        """
        Pre-seed the registry from the DB chain cache for snapshot_id.
        Called on startup (HIT path) so lookup() works before build() finishes.
        Returns number of contracts loaded (0 on empty or error).
        """
        from services.chain_store import load_chain
        chain = await load_chain(snapshot_id)
        if not chain:
            log.info(
                "[symbol_registry] load_from_db: no cached chain for snapshot %s",
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

    async def build(self) -> int:
        from services.ingestion_config import get_config
        from services.tier_engine import _fetch_thresholds

        cfg, thresh = await asyncio.gather(get_config(), _fetch_thresholds())
        tier_params  = _build_tier_params(thresh, global_min_oi=cfg["REGISTRY_MIN_OI"])

        async with self._build_lock:
            log.info(
                "[symbol_registry] Building OCC registry for %d tickers "
                "[T1: atm=+/-%.0f%% dte=%d | T2: atm=+/-%.0f%% dte=%d | T3: atm=+/-%.0f%% dte=%d | min_oi=%d]",
                len(self._watchlist),
                tier_params[1].atm_pct * 100, tier_params[1].max_dte,
                tier_params[2].atm_pct * 100, tier_params[2].max_dte,
                tier_params[3].atm_pct * 100, tier_params[3].max_dte,
                cfg["REGISTRY_MIN_OI"],
            )
            new_registry: dict[str, ContractMeta] = {}
            new_oi_by_ticker: dict[str, int] = {}

            prices = await self._fetch_stock_prices()
            self._stock_prices = prices
            log.info("[symbol_registry] Stock prices fetched: %d tickers", len(prices))

            tasks = [
                self._build_ticker(
                    ticker,
                    prices.get(ticker, 0.0),
                    new_registry,
                    new_oi_by_ticker,
                    tier_params,
                )
                for ticker in self._watchlist
                if ticker in prices and prices[ticker] > 0
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

            old_count           = len(self._registry)
            self._registry      = new_registry
            self._oi_by_ticker  = new_oi_by_ticker
            self._last_build    = datetime.utcnow()

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

            # Persist to DB so restarts can fast-seed from cache
            await self._persist_to_db(new_registry)

            return len(new_registry)

    async def _persist_to_db(self, registry_dict: dict[str, ContractMeta]) -> None:
        """
        Persist chain to options_chain_cache for the current active snapshot.
        Non-fatal — a failure here never breaks the in-memory registry.
        """
        from services.chain_store import save_chain
        from services import universe_store
        try:
            # Resolve the active snapshot_id
            loop = asyncio.get_event_loop()
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

    async def _fetch_stock_prices(self) -> dict[str, float]:
        prices: dict[str, float] = {}
        batch_size = 200
        batches = [
            self._watchlist[i:i + batch_size]
            for i in range(0, len(self._watchlist), batch_size)
        ]
        results = await asyncio.gather(*[get_quotes_batch(b) for b in batches])
        for quote_map in results:
            for sym, q in quote_map.items():
                for key in ("last", "last_price", "close", "prevclose"):
                    val = q.get(key)
                    if val:
                        try:
                            prices[sym] = float(val)
                            break
                        except (TypeError, ValueError):
                            pass
        return prices

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
