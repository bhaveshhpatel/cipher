"""
services/symbol_registry.py — Layer 1: OCC Symbol Registry

Builds and maintains a mapping of ALL active OCC option contract symbols
to their metadata (ticker, strike, expiry, contract_type, DTE).

Config (C-019):
  All filter constants are now read from the `ingestion_config` Supabase
  table via services.ingestion_config.get_config() on every build/refresh.
  Knobs (MAX_DTE, ATM_RANGE_PCT, MIN_OI, REFRESH_MINS, etc.) can be changed
  from the admin UI without restarting the service.
  A 60-second TTL cache in ingestion_config.py prevents DB hammering.
"""
import asyncio
import logging
from dataclasses import dataclass
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


class SymbolRegistry:
    """
    Thread-safe in-memory OCC symbol registry.
    Rebuilt on a schedule; safe for concurrent reads during rebuild
    (atomic swap of the internal dict on completion).

    All filter thresholds are read from ingestion_config DB table on
    every build so admin UI changes take effect without restart.
    """

    def __init__(self, watchlist: Optional[list[str]] = None):
        self._watchlist: list[str] = watchlist or list(settings.priority_symbols)
        self._registry: dict[str, ContractMeta] = {}
        self._stock_prices: dict[str, float] = {}
        self._last_build: Optional[datetime] = None
        self._build_lock = asyncio.Lock()

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

    async def build(self) -> int:
        """
        Full registry build. Reads live config from DB, then:
        fetches stock prices -> expirations -> chains.
        Returns count of OCC symbols loaded.
        """
        from services.ingestion_config import get_config
        cfg = await get_config()

        async with self._build_lock:
            log.info(
                f"[symbol_registry] Building OCC registry for {len(self._watchlist)} tickers "
                f"[max_dte={cfg['REGISTRY_MAX_DTE']}, atm_range=+/-{cfg['REGISTRY_ATM_RANGE_PCT']:.0%}, "
                f"min_oi={cfg['REGISTRY_MIN_OI']}]"
            )
            new_registry: dict[str, ContractMeta] = {}

            prices = await self._fetch_stock_prices()
            self._stock_prices = prices
            log.info(f"[symbol_registry] Stock prices fetched: {len(prices)} tickers")

            tasks = [
                self._build_ticker(ticker, prices.get(ticker, 0.0), new_registry, cfg)
                for ticker in self._watchlist
                if ticker in prices and prices[ticker] > 0
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

            old_count = len(self._registry)
            self._registry = new_registry
            self._last_build = datetime.utcnow()

            added = len(new_registry) - old_count
            log.info(
                f"[symbol_registry] Build complete: {len(new_registry):,} OCC symbols "
                f"(was {old_count:,}, delta={added:+,})"
            )
            return len(new_registry)

    async def refresh_loop(self):
        """
        Background task — rebuilds registry on schedule.
        Refresh interval is read from DB config on every cycle so changes
        to REGISTRY_REFRESH_MINS take effect without restart.
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

            log.info(f"[symbol_registry] Scheduled refresh (interval={interval_mins}min)")
            try:
                await self.build()
            except Exception as e:
                log.error(f"[symbol_registry] Refresh failed (non-fatal): {e}")

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
        ticker: str,
        stock_price: float,
        registry: dict[str, ContractMeta],
        cfg: dict,
    ):
        """
        Fetch expirations + chains for one ticker and populate registry.
        Uses config snapshot passed in from build() so all tickers in one
        build cycle use the same consistent config values.
        """
        if stock_price <= 0:
            log.warning(f"[symbol_registry] {ticker}: no stock price — skipping")
            return

        max_dte       = cfg["REGISTRY_MAX_DTE"]
        atm_range_pct = cfg["REGISTRY_ATM_RANGE_PCT"]
        min_oi        = cfg["REGISTRY_MIN_OI"]

        try:
            expirations = await get_expirations(ticker)
        except Exception as e:
            log.warning(f"[symbol_registry] {ticker}: expirations fetch failed: {e}")
            return

        today    = date.today()
        atm_low  = stock_price * (1 - atm_range_pct)
        atm_high = stock_price * (1 + atm_range_pct)

        for expiry_str in expirations:
            try:
                exp_date = date.fromisoformat(expiry_str)
            except ValueError:
                continue
            dte = (exp_date - today).days
            if dte < 0 or dte > max_dte:
                continue

            try:
                contracts = await get_option_chain(ticker, expiry_str)
            except Exception as e:
                log.warning(f"[symbol_registry] {ticker} {expiry_str}: chain fetch failed: {e}")
                continue

            for contract in contracts:
                try:
                    strike = float(contract.get("strike", 0) or 0)
                    if strike <= 0:
                        continue
                    if not (atm_low <= strike <= atm_high):
                        continue
                    oi = int(contract.get("open_interest", 0) or 0)
                    if oi < min_oi:
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
                    )
                except Exception:
                    continue

        ticker_count = sum(1 for m in registry.values() if m.ticker == ticker)
        log.debug(f"[symbol_registry] {ticker}: {ticker_count} contracts loaded (price=${stock_price:.2f})")


_registry: Optional[SymbolRegistry] = None


def get_registry() -> Optional[SymbolRegistry]:
    return _registry


def init_registry(watchlist: list[str]) -> SymbolRegistry:
    global _registry
    _registry = SymbolRegistry(watchlist=watchlist)
    return _registry
