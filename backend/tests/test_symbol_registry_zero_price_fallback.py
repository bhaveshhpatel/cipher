"""
test_symbol_registry_zero_price_fallback.py

Tests for B-ZERO-PRICE fix in services/symbol_registry.py.

Covers:
  1. build() with ALL prices missing -> zero_price_fallback=True, all tickers
     still submitted to _build_ticker, registry non-empty after build.
  2. build() with PARTIAL prices missing -> tickers with prices use normal ATM
     range; tickers without prices bypass ATM filter entirely.
  3. _build_ticker with stock_price=0 and zero_price_fallback=True -> ATM
     filter bypassed (atm_low=0, atm_high=inf), all strikes load.
  4. _build_ticker with stock_price=0 and zero_price_fallback=False (default)
     -> skips ticker as before (regression guard).
  5. _build_ticker with valid price -> normal ATM range applied, contracts
     outside range filtered out.
  6. build() sets _build_complete=True even when all prices missing.
  7. ATM bypass: all strikes pass including normally far-OTM values.
"""
from unittest.mock import AsyncMock, patch

import pytest

from services.symbol_registry import (
    SymbolRegistry,
    ContractMeta,
    _TierParams,
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
    """Tier params with wide ATM range - used in most tests."""
    p = _TierParams(atm_pct=0.50, max_dte=90, min_oi=0)
    return {1: p, 2: p, 3: p}


def _tier_params_narrow() -> dict[int, _TierParams]:
    """Tier params with narrow ATM range (10%) - used for filter tests."""
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
# Test 1: build() with ALL prices missing -> fallback activated, registry built
# ---------------------------------------------------------------------------

class TestBuildZeroPriceAllMissing:
    @pytest.mark.asyncio
    async def test_registry_non_empty_when_all_prices_missing(self):
        """
        When _fetch_stock_prices() returns {} for all tickers, build() must
        NOT silently complete with 0 contracts. ATM filter is bypassed and
        all contracts load regardless of strike.
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

        assert count > 0, "Registry must not be 0 when all prices missing - fallback should load contracts"
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
# Test 2: build() with PARTIAL prices -> mixed normal + fallback
# ---------------------------------------------------------------------------

class TestBuildZeroPricePartialMissing:
    @pytest.mark.asyncio
    async def test_partial_prices_both_tickers_load_contracts(self):
        """
        AAPL has a real price; TSLA does not. Both should produce contracts.
        TSLA has ATM filter bypassed entirely (atm_low=0, atm_high=inf).
        """
        registry = SymbolRegistry(watchlist=["AAPL", "TSLA"])
        expiry = "2026-05-16"

        aapl_contracts = [
            _make_contract("AAPL260516C00150000", 150.0, "C"),
            _make_contract("AAPL260516C00300000", 300.0, "C"),  # far OTM - filtered for AAPL
        ]
        tsla_contracts = [
            _make_contract("TSLA260516C00200000", 200.0, "C"),  # passes (bypass)
            _make_contract("TSLA260516C00800000", 800.0, "C"),  # passes (bypass)
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
# Test 3: _build_ticker zero_price_fallback=True -> ATM filter bypassed
# ---------------------------------------------------------------------------

class TestBuildTickerZeroPriceFallbackTrue:
    @pytest.mark.asyncio
    async def test_loads_contracts_with_bypass(self):
        """stock_price=0, zero_price_fallback=True -> ATM bypassed, all contracts load."""
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
                0.0,
                new_registry,
                new_oi,
                _tier_params_wide(),
                zero_price_fallback=True,
            )

        assert len(new_registry) == 2, "Both contracts should load when ATM filter is bypassed"

    @pytest.mark.asyncio
    async def test_all_strikes_pass_when_atm_bypassed(self):
        """
        With zero_price_fallback=True, ATM filter is bypassed entirely
        (atm_low=0, atm_high=inf). Both a low strike (500) and an extremely
        high strike (2_000_000) should be loaded.
        """
        registry = SymbolRegistry(watchlist=["META"])
        registry._tier_map = {"META": 3}
        expiry = "2026-05-16"
        contracts = [
            _make_contract("META260516C00000500", 500.0, "C"),
            _make_contract("META260516C02000000", 2_000_000.0, "C"),
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

        # Both strikes pass because ATM filter is bypassed entirely
        assert "META260516C00000500" in new_registry
        assert "META260516C02000000" in new_registry


# ---------------------------------------------------------------------------
# Test 4: _build_ticker zero_price_fallback=False (default) -> skips ticker
# ---------------------------------------------------------------------------

class TestBuildTickerZeroPriceFallbackFalse:
    @pytest.mark.asyncio
    async def test_skips_ticker_when_no_fallback(self):
        """stock_price=0, zero_price_fallback=False -> skips, no contracts added (regression guard)."""
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
        get_exp_mock.assert_not_called()  # short-circuits before expirations fetch


# ---------------------------------------------------------------------------
# Test 5: _build_ticker with valid price -> normal ATM filter
# ---------------------------------------------------------------------------

class TestBuildTickerNormalPrice:
    @pytest.mark.asyncio
    async def test_atm_filter_rejects_far_otm(self):
        """
        stock_price=100, tier3 atm_pct=10% -> only strikes 90-110 pass.
        Strike=200 should be rejected.
        """
        registry = SymbolRegistry(watchlist=["GOOG"])
        registry._tier_map = {"GOOG": 3}
        expiry = "2026-05-16"
        contracts = [
            _make_contract("GOOG260516C00100000", 100.0, "C"),  # ATM - passes
            _make_contract("GOOG260516C00200000", 200.0, "C"),  # far OTM - rejected
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
# Test 6: ATM bypass correctness - normal price path unaffected
# ---------------------------------------------------------------------------

class TestAtmBypassBehaviour:
    @pytest.mark.asyncio
    async def test_bypass_vs_normal_differ_on_far_otm(self):
        """
        Same ticker, same strike (5x ATM) - with fallback=False the contract is
        rejected by the 10% ATM filter; with fallback=True it loads.
        """
        registry = SymbolRegistry(watchlist=["TSLA"])
        registry._tier_map = {"TSLA": 3}
        expiry = "2026-05-16"
        far_strike = 1000.0  # 5x stock_price=200 -> outside 10% ATM band
        contracts = [_make_contract("TSLA260516C01000000", far_strike, "C")]

        # With fallback=False (normal): rejected
        reg_no_fallback: dict[str, ContractMeta] = {}
        with (
            patch("services.symbol_registry.get_expirations", new=AsyncMock(return_value=[expiry])),
            patch("services.symbol_registry.get_option_chain_bulk", new=AsyncMock(return_value=contracts)),
        ):
            await registry._build_ticker(
                "TSLA", 200.0, reg_no_fallback, {}, _tier_params_narrow(),
                zero_price_fallback=False,
            )
        assert "TSLA260516C01000000" not in reg_no_fallback

        # With fallback=True (zero-price bypass): accepted
        reg_fallback: dict[str, ContractMeta] = {}
        with (
            patch("services.symbol_registry.get_expirations", new=AsyncMock(return_value=[expiry])),
            patch("services.symbol_registry.get_option_chain_bulk", new=AsyncMock(return_value=contracts)),
        ):
            await registry._build_ticker(
                "TSLA", 0.0, reg_fallback, {}, _tier_params_narrow(),
                zero_price_fallback=True,
            )
        assert "TSLA260516C01000000" in reg_fallback
