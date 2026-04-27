"""
Issue-6 regression: build() must NOT call get_quotes_batch when
pre_fetched_quotes is supplied.

Patches follow the same style as test_symbol_registry_coverage.py:
  - patch the source module directly, not the import site
  - all async deps are AsyncMock
"""
import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.symbol_registry import SymbolRegistry, ContractMeta


_NEAR_EXPIRY = (date.today() + timedelta(days=14)).isoformat()

_FAKE_CONFIG = {
    "REGISTRY_MIN_OI": 0,
    "REGISTRY_REFRESH_MINS": 60,
    "REGISTRY_EXPIRY_DAY_REFRESH_MINS": 10,
    "REGISTRY_OI_DELTA_THRESHOLD": 0.20,
}

_FAKE_THRESH = {
    "t1_atm_pct": 0.20, "t1_max_dte": 90, "t1_min_oi": 0,
    "t2_atm_pct": 0.15, "t2_max_dte": 60, "t2_min_oi": 0,
    "t3_atm_pct": 0.10, "t3_max_dte": 30, "t3_min_oi": 0,
}

_FAKE_CHAIN = [
    {"symbol": "AAPL231215C00180000", "strike": 180.0,
     "option_type": "C", "open_interest": 1000},
    {"symbol": "AAPL231215P00175000", "strike": 175.0,
     "option_type": "P", "open_interest": 500},
]

_PRE_FETCHED = {"AAPL": MagicMock(last=185.0, volume=5_000_000)}


def _base_patches(quotes_mock):
    """Patches for all external I/O except get_quotes_batch."""
    return [
        patch("services.ingestion_config.get_config",  new=AsyncMock(return_value=_FAKE_CONFIG)),
        patch("services.tier_engine._fetch_thresholds", new=AsyncMock(return_value=_FAKE_THRESH)),
        patch("services.symbol_registry.get_quotes_batch", new=quotes_mock),
        patch("services.symbol_registry.get_expirations",  new=AsyncMock(return_value=[_NEAR_EXPIRY])),
        patch("services.symbol_registry.get_option_chain", new=AsyncMock(return_value=_FAKE_CHAIN)),
    ]


# ---------------------------------------------------------------------------
# Core regression: pre_fetched_quotes bypasses Tradier quote fetch
# ---------------------------------------------------------------------------

class TestSingleQuotesCall:
    """build() called with pre_fetched_quotes must never call get_quotes_batch."""

    def test_no_tradier_call_when_pre_fetched_supplied(self):
        quotes_mock = AsyncMock()
        r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

        async def _run():
            with _base_patches(quotes_mock)[0], \
                 _base_patches(quotes_mock)[1], \
                 _base_patches(quotes_mock)[2], \
                 _base_patches(quotes_mock)[3], \
                 _base_patches(quotes_mock)[4]:
                return await r.build(pre_fetched_quotes=_PRE_FETCHED)

        asyncio.run(_run())
        quotes_mock.assert_not_called()

    def test_stock_price_populated_from_pre_fetched(self):
        quotes_mock = AsyncMock()
        r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

        async def _run():
            p = _base_patches(quotes_mock)
            with p[0], p[1], p[2], p[3], p[4]:
                await r.build(pre_fetched_quotes=_PRE_FETCHED)

        asyncio.run(_run())
        assert r.stock_price("AAPL") == pytest.approx(185.0)

    def test_contracts_populated_when_pre_fetched_supplied(self):
        quotes_mock = AsyncMock()
        r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

        async def _run():
            p = _base_patches(quotes_mock)
            with p[0], p[1], p[2], p[3], p[4]:
                return await r.build(pre_fetched_quotes=_PRE_FETCHED)

        count = asyncio.run(_run())
        assert count >= 1
        assert r.is_ready()

    def test_registry_ready_after_pre_fetched_build(self):
        quotes_mock = AsyncMock()
        r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

        async def _run():
            p = _base_patches(quotes_mock)
            with p[0], p[1], p[2], p[3], p[4]:
                await r.build(pre_fetched_quotes=_PRE_FETCHED)

        asyncio.run(_run())
        syms = r.all_symbols()
        assert len(syms) > 0
        meta = r.lookup(syms[0])
        assert isinstance(meta, ContractMeta)
        assert meta.ticker == "AAPL"

    def test_tradier_called_when_no_pre_fetched(self):
        """Fallback path: if no pre_fetched, get_quotes_batch IS called."""
        quotes_mock = AsyncMock(return_value={"AAPL": {"last": 185.0}})
        r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

        async def _run():
            p = _base_patches(quotes_mock)
            with p[0], p[1], p[2], p[3], p[4]:
                return await r.build()  # no pre_fetched_quotes

        asyncio.run(_run())
        quotes_mock.assert_called()

    def test_pre_fetched_ticker_not_in_watchlist_ignored(self):
        """Extra tickers in pre_fetched that aren't in watchlist are silently dropped."""
        extra = {"AAPL": MagicMock(last=185.0, volume=5_000_000),
                 "NVDA": MagicMock(last=900.0, volume=10_000_000)}
        quotes_mock = AsyncMock()
        r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

        async def _run():
            p = _base_patches(quotes_mock)
            with p[0], p[1], p[2], p[3], p[4]:
                await r.build(pre_fetched_quotes=extra)

        asyncio.run(_run())
        # NVDA is not in watchlist — registry should not blow up
        assert r.stock_price("NVDA") == 0.0
        assert r.is_ready()

    def test_pre_fetched_missing_ticker_falls_through(self):
        """Ticker in watchlist but missing from pre_fetched is skipped gracefully."""
        incomplete = {}  # AAPL is in watchlist but not in pre_fetched
        quotes_mock = AsyncMock()
        r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

        async def _run():
            p = _base_patches(quotes_mock)
            with p[0], p[1], p[2], p[3], p[4]:
                return await r.build(pre_fetched_quotes=incomplete)

        count = asyncio.run(_run())
        # No price → no chain fetch → 0 contracts registered
        assert count == 0
        quotes_mock.assert_not_called()
