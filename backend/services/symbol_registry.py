"""
services/symbol_registry.py — Layer 1: OCC Symbol Registry

Builds and maintains a mapping of ALL active OCC option contract symbols
to their metadata (ticker, strike, expiry, contract_type, DTE).

This is the key architectural component that enables correct options flow
streaming. Instead of streaming underlying ticker symbols (which returns
equity trade events), we stream the full OCC contract symbols
(e.g. "AAPL  260117C00180000") which returns actual option trade events.

HOW IT WORKS:
  1. For each ticker in watchlist → fetch current stock price
  2. Fetch all active expiration dates (DTE ≤ MAX_DTE)
  3. For each expiry → fetch full option chain
  4. Filter contracts where strike is within ATM_RANGE of stock price
  5. Build OCC symbol → metadata dict (O(1) lookup at parse time)
  6. Refresh every 30 minutes — diff new vs old, update stream workers

USAGE:
  registry = SymbolRegistry()
  await registry.build()
  meta = registry.lookup("AAPL  260117C00180000")
  # → {"ticker": "AAPL", "strike": 180.0, "expiry": "2026-01-17",
  #    "contract_type": "CALL", "dte": 30, "open_interest": 5000}
  occ_symbols = registry.all_symbols()  # → list of OCC strings for streaming

REFRESH:
  asyncio.create_task(registry.refresh_loop())
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from config import settings
from utils.tradier_client import get_expirations, get_option_chain, get_quotes_batch

log = logging.getLogger("symbol_registry")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MAX_DTE        = 90     # only include contracts expiring within 90 days
ATM_RANGE_PCT  = 0.15   # ±15% of current stock price
MIN_OI         = 0      # minimum open interest (0 = include all)
REFRESH_MINS   = 30     # rebuild registry every 30 minutes
EXPIRY_DAY_REFRESH_MINS = 15  # faster refresh on expiry day


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
    """

    def __init__(self, watchlist: Optional[list[str]] = None):
        self._watchlist: list[str] = watchlist or list(settings.priority_symbols)
        self._registry: dict[str, ContractMeta] = {}
        self._stock_prices: dict[str, float] = {}
        self._last_build: Optional[datetime] = None
        self._build_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def lookup(self, occ_symbol: str) -> Optional[ContractMeta]:
        """O(1) lookup of OCC symbol metadata. Returns None if not in registry."""
        return self._registry.get(occ_symbol.strip())

    def all_symbols(self) -> list[str]:
        """Return all OCC symbols currently in the registry."""
        return list(self._registry.keys())

    def size(self) -> int:
        return len(self._registry)

    def stock_price(self, ticker: str) -> float:
        return self._stock_prices.get(ticker, 0.0)

    def is_ready(self) -> bool:
        return len(self._registry) > 0

    # ------------------------------------------------------------------
    # Build / Refresh
    # ------------------------------------------------------------------

    async def build(self) -> int:
        """
        Full registry build. Fetches stock prices → expirations → chains.
        Returns count of OCC symbols loaded.
        """
        async with self._build_lock:
            log.info(f"[symbol_registry] Building OCC registry for {len(self._watchlist)} tickers...")
            new_registry: dict[str, ContractMeta] = {}

            # Step 1: Batch fetch stock prices for all watchlist tickers
            # Split into 200-symbol batches
            prices = await self._fetch_stock_prices()
            self._stock_prices = prices
            log.info(f"[symbol_registry] Stock prices fetched: {len(prices)} tickers")

            # Step 2 + 3: For each ticker, fetch expirations then chains
            tasks = [
                self._build_ticker(ticker, prices.get(ticker, 0.0), new_registry)
                for ticker in self._watchlist
                if ticker in prices and prices[ticker] > 0
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

            old_count = len(self._registry)
            self._registry = new_registry
            self._last_build = datetime.utcnow()

            added   = len(new_registry) - old_count
            log.info(
                f"[symbol_registry] Build complete: {len(new_registry):,} OCC symbols "
                f"(was {old_count:,}, delta={added:+,})"
            )
            return len(new_registry)

    async def refresh_loop(self):
        """
        Background task — rebuilds registry on schedule.
        Run as: asyncio.create_task(registry.refresh_loop())
        """
        while True:
            # Determine refresh interval — faster on expiry days
            today = date.today()
            # Check if any tracked expiry matches today
            has_expiry_today = any(
                meta.expiry == today.isoformat()
                for meta in self._registry.values()
            )
            interval_mins = EXPIRY_DAY_REFRESH_MINS if has_expiry_today else REFRESH_MINS
            await asyncio.sleep(interval_mins * 60)

            log.info(f"[symbol_registry] Scheduled refresh (interval={interval_mins}min)")
            try:
                await self.build()
            except Exception as e:
                log.error(f"[symbol_registry] Refresh failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _fetch_stock_prices(self) -> dict[str, float]:
        """Batch fetch current stock prices for all watchlist tickers."""
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
    ):
        """
        Fetch expirations + chains for one ticker and populate registry.
        Runs with rate-limit semaphore inside get_option_chain.
        """
        if stock_price <= 0:
            log.warning(f"[symbol_registry] {ticker}: no stock price — skipping")
            return

        try:
            expirations = await get_expirations(ticker)
        except Exception as e:
            log.warning(f"[symbol_registry] {ticker}: expirations fetch failed: {e}")
            return

        today = date.today()
        atm_low  = stock_price * (1 - ATM_RANGE_PCT)
        atm_high = stock_price * (1 + ATM_RANGE_PCT)

        for expiry_str in expirations:
            try:
                exp_date = date.fromisoformat(expiry_str)
            except ValueError:
                continue
            dte = (exp_date - today).days
            if dte < 0 or dte > MAX_DTE:
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
                    # ATM filter
                    if not (atm_low <= strike <= atm_high):
                        continue
                    # Open interest filter
                    oi = int(contract.get("open_interest", 0) or 0)
                    if oi < MIN_OI:
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


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_registry: Optional[SymbolRegistry] = None


def get_registry() -> Optional[SymbolRegistry]:
    return _registry


def init_registry(watchlist: list[str]) -> SymbolRegistry:
    global _registry
    _registry = SymbolRegistry(watchlist=watchlist)
    return _registry
