"""
services/symbol_registry.py — Layer 1: OCC Symbol Registry

Builds and maintains a mapping of ALL active OCC option contract symbols
to their metadata (ticker, strike, expiry, contract_type, DTE).

Config (C-019):
  All filter constants are read from the `ingestion_config` Supabase table
  via services.ingestion_config.get_config() on every build/refresh.
  Knobs (MIN_OI, REFRESH_MINS, etc.) can be changed from the admin UI
  without restarting the service.

Feature 4A — Per-tier ATM range + max DTE:
  ATM_RANGE_PCT and MAX_DTE are no longer flat globals. On each build,
  tier_engine fetches the active tier_thresholds row from DB and assembles
  a per-tier filter dict. Each ticker is built with the ATM/DTE params
  matching its tier (T1/T2/T3). REGISTRY_ATM_RANGE_PCT and REGISTRY_MAX_DTE
  in ingestion_config are kept as the T3 / fallback defaults for symbols
  whose tier is unknown.

Feature 4A-OI — Per-symbol average chain OI roll-up:
  After each registry build, _oi_by_ticker holds the average open_interest
  across all loaded (post-filter) contracts for each underlying ticker.
  This is exposed via get_oi_map() so main.py can populate
  SymbolQuote.open_interest before calling tier_engine.assign_tiers(),
  making the t1_min_oi / t2_min_oi / t3_min_oi thresholds in
  tier_thresholds effective at the symbol classification step.

  Formula: avg_oi(ticker) = sum(contract.open_interest) / count(loaded contracts)
  Only contracts that passed ATM + DTE + min_oi filters are included,
  so the average reflects the liquidity of contracts Cipher actually monitors.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from config import settings
from utils.tradier_client import get_expirations, get_option_chain, get_quotes_batch

log = logging.getLogger("symbol_registry")


@dataclass
class ContractMeta:
    ticker:        str
    strike:        float
    expiry:        str    # YYYY-MM-DD
    contract_type: str    # CALL | PUT
    dte:           int
    open_interest: int
    tier:          int = 3   # 4A: tier of the underlying symbol (1 | 2 | 3)


# ---------------------------------------------------------------------------
# Per-tier filter params (assembled at build time from tier_thresholds)
# ---------------------------------------------------------------------------

@dataclass
class _TierParams:
    atm_pct: float
    max_dte: int
    min_oi:  int


def _build_tier_params(thresh: dict, global_min_oi: int) -> dict[int, _TierParams]:
    """
    Convert a tier_thresholds dict (from tier_engine._fetch_thresholds)
    into a {tier -> _TierParams} map used by _build_ticker.
    global_min_oi is the REGISTRY_MIN_OI value from ingestion_config
    (applies as a floor across all tiers).
    """
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
    Thread-safe in-memory OCC symbol registry.
    Rebuilt on a schedule; safe for concurrent reads during rebuild
    (atomic swap of the internal dict on completion).

    All filter thresholds are read from ingestion_config + tier_thresholds
    DB tables on every build so admin UI changes take effect without restart.

    After each build, get_oi_map() returns avg chain OI per underlying ticker
    (Feature 4A-OI) for use by tier_engine.assign_tiers().
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
        # 4A-OI: avg open_interest across loaded contracts per underlying ticker.
        # Populated on every build; reset at build start inside the lock.
        self._oi_by_ticker: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public read API
    # ------------------------------------------------------------------

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
        """Hot-swap the tier map without rebuilding the registry."""
        self._tier_map = tier_map

    def get_oi_map(self) -> dict[str, int]:
        """
        Return avg chain OI per underlying ticker from the most recent build.

        Value = mean open_interest across all contracts that passed the
        ATM + DTE + min_oi filters for that ticker.  Returns 0 for any
        ticker with no loaded contracts.

        Used by main.py to populate SymbolQuote.open_interest before
        calling tier_engine.assign_tiers(), activating the t*_min_oi
        thresholds in tier_thresholds.
        """
        return dict(self._oi_by_ticker)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    async def build(self) -> int:
        """
        Full registry build. Reads live config + tier thresholds from DB, then:
        fetches stock prices -> expirations -> chains per ticker.
        Returns count of OCC symbols loaded.

        After this method returns, get_oi_map() reflects avg chain OI for
        every ticker that was built (Feature 4A-OI).
        """
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
            new_oi_by_ticker: dict[str, int] = {}  # 4A-OI: reset each build

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
            self._oi_by_ticker  = new_oi_by_ticker  # 4A-OI: atomic swap
            self._last_build    = datetime.utcnow()

            # Log tier breakdown of loaded contracts
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
            return len(new_registry)

    # ------------------------------------------------------------------
    # Refresh loop
    # ------------------------------------------------------------------

    async def refresh_loop(self):
        """
        Background task — rebuilds registry on schedule.
        Refresh interval is read from DB config on every cycle.
        """
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
        """
        Fetch expirations + chains for one ticker and populate registry.
        Uses the symbol's tier from self._tier_map to select per-tier
        ATM range and max DTE. Unknown-tier symbols fall back to T3 params.

        4A-OI: After loading all contracts for this ticker, computes avg OI
        across loaded contracts and stores it in oi_by_ticker[ticker].
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

        # 4A-OI: compute avg OI across all contracts loaded for this ticker.
        # Only contracts that passed ATM + DTE + min_oi filters are included.
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


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

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
