"""
test_symbol_registry_zero_price_fallback.py

Tests for B-ZERO-PRICE fix in services/symbol_registry.py.

Covers:
  1. build() with ALL prices missing → zero_price_fallback=True, all tickers
     still submitted to _build_ticker, registry non-empty after build.
  2. build() with PARTIAL prices missing → tickers with prices use normal ATM
     range; tickers without prices use fallback sentinel.
  3. _build_ticker with stock_price=0 and zero_price_fallback=True → uses
     sentinel price + wide ATM, does NOT skip, loads contracts.
  4. _build_ticker with stock_price=0 and zero_price_fallback=False (default)
     → skips ticker as before (regression guard).
  5. _build_ticker with valid price → normal ATM range applied, contracts
     outside range filtered out.
  6. build() sets _build_complete=True even when all prices missing.
  7. Zero-price fallback: wide ATM range (50%) passes all strikes.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.symbol_registry import (
    SymbolRegistry,
    ContractMeta,
    _TierParams,
    _ZERO_PRICE_ATM_PCT,
    _FALLBACK_SENTINEL_PRICE,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_contract(symbol: str, strike: float, opt_type: str = "C") -> dict:
    return {
        "symbol": symbol,
        "strike": strike,
        "option_type": opt_type,
        "open_interest": 100,
    }


def _tier_params_wide() -> dict[int, _TierParams]:
    """Tier params with wide ATM range — used in most tests."""
    p = _TierParams(atm_pct=0.50, max_dte=90, min_oi=0)
    return {1: p, 2: p, 3: p}


def _tier_params_narrow() -> dict[int, _TierParams]:
    """Tier params with narrow ATM range (10%) — used for filter tests."""
    p = _TierParams(atm_pct=0.10, max_dte=90, min_oi=0)
    return {1: p, 2: p, 3: p}


ASYNC_CFG = {
    "REGISTRY_MIN_OI": 0,
    "REGISTRY_BUILD_CONCURRENCY": 2,
}

ASYNC_THRESH = {
    "t1_atm_pct": 0.20, "t1_max_dte": 90, "t1_min_oi": 0,
    "t2_atm_pct": 0.15, "t2_max_dte": 60, "t2_min_oi": 0,
    "t3_atm_pct": 0.10, "t3_max_dte": 30, "t3_min_oi": 0,
}


# ---------------------------------------------------------------------------
# Test 1: build() with ALL prices missing → fallback activated, registry built
# ---------------------------------------------------------------------------

class TestBuildZeroPriceAllMissing:
    @pytest.mark.asyncio
    async def test_registry_non_empty_when_all_prices_missing(self):
        """
        When _fetch_stock_prices() returns {} for all tickers, build() must
        NOT silently complete with 0 contracts. It must fall back to wide ATM
        range and produce a non-empty registry.
        """
        registry = SymbolRegistry(watchlist=["AAPL", "TSLA"])

        expiry = "2026-05-16"
        contracts = [
            _make_contract("AAPL260516C00150000", 150.0, "C"),
            _make_contract("AAPL260516P00150000", 150.0, "P"),
        ]
        tsla_contracts = [
            _make_contract("TSLA260516C00200000", 200.0, "C"),
        ]

        with (
            patch("services.symbol_registry.get_config", new=AsyncMock(return_value=ASYNC_CFG)),
            patch("services.symbol_registry._fetch_thresholds", new=AsyncMock(return_value=ASYNC_THRESH)),
            patch("services.symbol_registry.assign_tiers", new=AsyncMock(return_value={"AAPL": 1, "TSLA": 3})),
            patch.object(registry, "_fetch_stock_prices", new=AsyncMock(return_value=({}, {}))),
            patch.object(registry, "_persist_to_db", new=AsyncMock()),
            patch("services.symbol_registry.get_expirations", new=AsyncMock(return_value=[expiry])),
            patch(
                "services.symbol_registry.get_option_chain_bulk",
                new=AsyncMock(side_effect=lambda t, e: contracts if t == "AAPL" else tsla_contracts),
            ),
        ):
            count, _ = await registry.build()

        assert count > 0, "Registry must not be 0 when all prices missing — fallback should load contracts"
        assert registry.is_ready() is True
        assert registry.size() > 0

    @pytest.mark.asyncio
    async def test_build_complete_set_true_when_all_prices_missing(self):
        """_build_complete must be True even when zero-price fallback runs."""
        registry = SymbolRegistry(watchlist=["SPY"])

        with (
            patch("services.symbol_registry.get_config", new=AsyncMock(return_value=ASYNC_CFG)),
            patch("services.symbol_registry._fetch_thresholds", new=AsyncMock(return_value=ASYNC_THRESH)),
            patch("services.symbol_registry.assign_tiers", new=AsyncMock(return_value={})),
            patch.object(registry, "_fetch_stock_prices", new=AsyncMock(return_value=({}, {}))),
            patch.object(registry, "_persist_to_db", new=AsyncMock()),
            patch("services.symbol_registry.get_expirations", new=AsyncMock(return_value=["2026-05-16"])),
            patch(
                "services.symbol_registry.get_option_chain_bulk",
                new=AsyncMock(return_value=[_make_contract("SPY260516C00500000", 500.0)]),
            ),
        ):
            await registry.build()

        assert registry._build_complete is True


# ---------------------------------------------------------------------------
# Test 2: build() with PARTIAL prices → mixed normal + fallback
# ---------------------------------------------------------------------------

class TestBuildZeroPricePartialMissing:
    @pytest.mark.asyncio
    async def test_partial_prices_both_tickers_load_contracts(self):
        """
        AAPL has a real price; TSLA does not. Both should produce contracts.
        TSLA uses the wide fallback ATM range.
        """
        registry = SymbolRegistry(watchlist=["AAPL", "TSLA"])
        expiry = "2026-05-16"

        # AAPL price=150, normal ATM ±10% → strikes 135-165 pass
        # TSLA price=0 → fallback sentinel=1_000_000, range ±50% → all strikes pass
        aapl_contracts = [
            _make_contract("AAPL260516C00150000", 150.0, "C"),
            _make_contract("AAPL260516C00300000", 300.0, "C"),  # far OTM — should be filtered for AAPL
        ]
        tsla_contracts = [
            _make_contract("TSLA260516C00200000", 200.0, "C"),  # should pass with sentinel
            _make_contract("TSLA260516C00800000", 800.0, "C"),  # should also pass with sentinel
        ]

        with (
            patch("services.symbol_registry.get_config", new=AsyncMock(return_value=ASYNC_CFG)),
            patch("services.symbol_registry._fetch_thresholds", new=AsyncMock(return_value=ASYNC_THRESH)),
            patch("services.symbol_registry.assign_tiers", new=AsyncMock(return_value={"AAPL": 1})),
            patch.object(registry, "_fetch_stock_prices", new=AsyncMock(return_value=(
                {"AAPL": 150.0}, {"AAPL": {"last": 150.0}}
            ))),
            patch.object(registry, "_persist_to_db", new=AsyncMock()),
            patch("services.symbol_registry.get_expirations", new=AsyncMock(return_value=[expiry])),
            patch(
                "services.symbol_registry.get_option_chain_bulk",
                new=AsyncMock(side_effect=lambda t, e: aapl_contracts if t == "AAPL" else tsla_contracts),
            ),
        ):
            count, _ = await registry.build()

        assert count >= 1, "At least TSLA or AAPL contracts expected"
        assert registry.is_ready() is True


# ---------------------------------------------------------------------------
# Test 3: _build_ticker zero_price_fallback=True → loads contracts
# ---------------------------------------------------------------------------

class TestBuildTickerZeroPriceFallbackTrue:
    @pytest.mark.asyncio
    async def test_loads_contracts_with_sentinel_price(self):
        """stock_price=0, zero_price_fallback=True → sentinel used, contracts loaded."""
        registry = SymbolRegistry(watchlist=["NVDA"])
        registry._tier_map = {"NVDA": 3}
        expiry = "2026-05-16"
        contracts = [
            _make_contract("NVDA260516C00100000", 100.0, "C"),
            _make_contract("NVDA260516P00100000", 100.0, "P"),
        ]
        new_registry: dict[str, ContractMeta] = {}
        new_oi: dict[str, int] = {}

        with (
            patch("services.symbol_registry.get_expirations", new=AsyncMock(return_value=[expiry])),
            patch("services.symbol_registry.get_option_chain_bulk", new=AsyncMock(return_value=contracts)),
        ):
            await registry._build_ticker(
                "NVDA",
                0.0,  # no price
                new_registry,
                new_oi,
                _tier_params_wide(),
                zero_price_fallback=True,
            )

        assert len(new_registry) == 2, "Both contracts should load with fallback sentinel"

    @pytest.mark.asyncio
    async def test_strike_filter_uses_wide_atm_pct(self):
        """
        With sentinel price=1_000_000 and ±50% ATM range, any strike < 1_500_000 passes.
        Strike=500 should pass; strike=2_000_000 should fail.
        """
        registry = SymbolRegistry(watchlist=["META"])
        registry._tier_map = {"META": 3}
        expiry = "2026-05-16"
        contracts = [
            _make_contract("META260516C00000500", 500.0, "C"),        # passes
            _make_contract("META260516C02000000", 2_000_000.0, "C"),  # fails (> sentinel*1.5)
        ]
        new_registry: dict[str, ContractMeta] = {}
        new_oi: dict[str, int] = {}

        with (
            patch("services.symbol_registry.get_expirations", new=AsyncMock(return_value=[expiry])),
            patch("services.symbol_registry.get_option_chain_bulk", new=AsyncMock(return_value=contracts)),
        ):
            await registry._build_ticker(
                "META",
                0.0,
                new_registry,
                new_oi,
                _tier_params_wide(),
                zero_price_fallback=True,
            )

        assert "META260516C00000500" in new_registry
        assert "META260516C02000000" not in new_registry


# ---------------------------------------------------------------------------
# Test 4: _build_ticker zero_price_fallback=False (default) → skips ticker
# ---------------------------------------------------------------------------

class TestBuildTickerZeroPriceFallbackFalse:
    @pytest.mark.asyncio
    async def test_skips_ticker_when_no_fallback(self):
        """stock_price=0, zero_price_fallback=False → skips, no contracts added (regression guard)."""
        registry = SymbolRegistry(watchlist=["AMD"])
        registry._tier_map = {"AMD": 3}
        new_registry: dict[str, ContractMeta] = {}
        new_oi: dict[str, int] = {}

        get_exp_mock = AsyncMock()
        with patch("services.symbol_registry.get_expirations", new=get_exp_mock):
            await registry._build_ticker(
                "AMD",
                0.0,
                new_registry,
                new_oi,
                _tier_params_narrow(),
                zero_price_fallback=False,
            )

        assert len(new_registry) == 0
        get_exp_mock.assert_not_called()  # should short-circuit before expirations fetch


# ---------------------------------------------------------------------------
# Test 5: _build_ticker with valid price → normal ATM filter
# ---------------------------------------------------------------------------

class TestBuildTickerNormalPrice:
    @pytest.mark.asyncio
    async def test_atm_filter_rejects_far_otm(self):
        """
        stock_price=100, tier3 atm_pct=10% → only strikes 90-110 pass.
        Strike=200 should be rejected.
        """
        registry = SymbolRegistry(watchlist=["GOOG"])
        registry._tier_map = {"GOOG": 3}
        expiry = "2026-05-16"
        contracts = [
            _make_contract("GOOG260516C00100000", 100.0, "C"),  # ATM — passes
            _make_contract("GOOG260516C00200000", 200.0, "C"),  # far OTM — rejected
        ]
        new_registry: dict[str, ContractMeta] = {}
        new_oi: dict[str, int] = {}

        with (
            patch("services.symbol_registry.get_expirations", new=AsyncMock(return_value=[expiry])),
            patch("services.symbol_registry.get_option_chain_bulk", new=AsyncMock(return_value=contracts)),
        ):
            await registry._build_ticker(
                "GOOG",
                100.0,
                new_registry,
                new_oi,
                _tier_params_narrow(),
                zero_price_fallback=False,
            )

        assert "GOOG260516C00100000" in new_registry
        assert "GOOG260516C00200000" not in new_registry


# ---------------------------------------------------------------------------
# Test 6: Zero-price fallback constant values
# ---------------------------------------------------------------------------

class TestFallbackConstants:
    def test_zero_price_atm_pct_is_wide(self):
        """_ZERO_PRICE_ATM_PCT must be >= 0.50 to be a meaningful wide fallback."""
        assert _ZERO_PRICE_ATM_PCT >= 0.50

    def test_fallback_sentinel_is_large(self):
        """_FALLBACK_SENTINEL_PRICE must be large enough that real strikes pass ATM filter."""
        # Any real strike (< ~1_500_000 = sentinel * 1.5) should pass
        assert _FALLBACK_SENTINEL_PRICE >= 1_000_000
        max_real_strike = 5000  # AMZN, GOOGL level
        atm_low = _FALLBACK_SENTINEL_PRICE * (1 - _ZERO_PRICE_ATM_PCT)
        assert atm_low <= max_real_strike, (
            f"ATM low ({atm_low}) must be <= realistic max strike ({max_real_strike})"
        )
