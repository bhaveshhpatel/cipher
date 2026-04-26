"""
Coverage boost for services/symbol_registry.py.

Covers:
  - SymbolRegistry construction and property methods
  - lookup(), all_symbols(), size(), stock_price(), is_ready()
  - set_tier_map(), get_oi_map()
  - init_registry() / get_registry() module singletons
  - build() full path with mocked tradier calls
  - _build_ticker() happy path and skip cases
  - refresh_loop() one iteration with mocked sleep
  - _build_tier_params() parameter derivation
"""
import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.symbol_registry import (
    SymbolRegistry,
    ContractMeta,
    _build_tier_params,
    get_registry,
    init_registry,
)


# --- _build_tier_params ---

def test_build_tier_params_uses_defaults():
    params = _build_tier_params({}, global_min_oi=50)
    assert params[1].atm_pct == pytest.approx(0.20)
    assert params[1].max_dte == 90
    assert params[2].max_dte == 60
    assert params[3].max_dte == 30

def test_build_tier_params_overrides():
    thresh = {"t1_atm_pct": "0.25", "t1_max_dte": "45", "t1_min_oi": "200"}
    params = _build_tier_params(thresh, global_min_oi=100)
    assert params[1].atm_pct == pytest.approx(0.25)
    assert params[1].max_dte == 45
    assert params[1].min_oi  == 200

def test_build_tier_params_global_min_oi_wins():
    thresh = {"t1_min_oi": "10"}
    params = _build_tier_params(thresh, global_min_oi=500)
    assert params[1].min_oi == 500


# --- SymbolRegistry basic properties ---

def _reg(watchlist=None, tier_map=None):
    return SymbolRegistry(watchlist=watchlist or ["AAPL", "TSLA"], tier_map=tier_map or {})


def test_registry_is_not_ready_when_empty():
    r = _reg()
    assert r.is_ready() is False

def test_registry_lookup_missing_returns_none():
    r = _reg()
    assert r.lookup("AAPL231215C00180000") is None

def test_registry_all_symbols_empty():
    assert _reg().all_symbols() == []

def test_registry_size_zero():
    assert _reg().size() == 0

def test_registry_stock_price_missing():
    assert _reg().stock_price("AAPL") == 0.0

def test_registry_set_tier_map():
    r = _reg()
    r.set_tier_map({"AAPL": 1})
    assert r._tier_map["AAPL"] == 1

def test_registry_get_oi_map_empty():
    assert _reg().get_oi_map() == {}


# --- init_registry / get_registry ---

def test_init_and_get_registry():
    r = init_registry(["AAPL", "NVDA"], tier_map={"AAPL": 1})
    assert get_registry() is r
    assert r._tier_map["AAPL"] == 1


# --- build() full path ---

_FAKE_THRESH = {
    "t1_atm_pct": 0.20, "t1_max_dte": 90, "t1_min_oi": 0,
    "t2_atm_pct": 0.15, "t2_max_dte": 60, "t2_min_oi": 0,
    "t3_atm_pct": 0.10, "t3_max_dte": 30, "t3_min_oi": 0,
}

_FAKE_CONFIG = {
    "REGISTRY_MIN_OI": 0,
    "REGISTRY_REFRESH_MINS": 60,
    "REGISTRY_EXPIRY_DAY_REFRESH_MINS": 10,
}

_NEAR_EXPIRY = (date.today() + timedelta(days=14)).isoformat()

_FAKE_CHAIN = [
    {"symbol": "AAPL231215C00180000", "strike": 180.0,
     "option_type": "C", "open_interest": 1000},
    {"symbol": "AAPL231215P00175000", "strike": 175.0,
     "option_type": "P", "open_interest": 500},
    {"symbol": "AAPL231215C00999000", "strike": 999.0,
     "option_type": "C", "open_interest": 100},  # out of ATM range
]


def test_registry_build_populates_contracts():
    r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

    async def _run():
        with patch("services.symbol_registry.get_config",     new=AsyncMock(return_value=_FAKE_CONFIG)), \
             patch("services.symbol_registry._fetch_thresholds", new=AsyncMock(return_value=_FAKE_THRESH)), \
             patch("services.symbol_registry.get_quotes_batch",  new=AsyncMock(return_value={"AAPL": {"last": 185.0}})), \
             patch("services.symbol_registry.get_expirations",   new=AsyncMock(return_value=[_NEAR_EXPIRY])), \
             patch("services.symbol_registry.get_option_chain",  new=AsyncMock(return_value=_FAKE_CHAIN)):
            return await r.build()

    count = asyncio.get_event_loop().run_until_complete(_run())
    assert count >= 1
    assert r.is_ready()
    assert r.size() == count
    assert r.stock_price("AAPL") == pytest.approx(185.0)


def test_registry_build_skips_ticker_with_no_price():
    r = SymbolRegistry(watchlist=["AAPL", "TSLA"], tier_map={})

    async def _run():
        with patch("services.symbol_registry.get_config",      new=AsyncMock(return_value=_FAKE_CONFIG)), \
             patch("services.symbol_registry._fetch_thresholds", new=AsyncMock(return_value=_FAKE_THRESH)), \
             patch("services.symbol_registry.get_quotes_batch",  new=AsyncMock(return_value={"AAPL": {"last": 185.0}})):
            return await r.build()

    asyncio.get_event_loop().run_until_complete(_run())
    assert "TSLA" not in r._stock_prices or r._stock_prices.get("TSLA", 0) == 0


def test_registry_build_ticker_expiry_fetch_error():
    r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

    async def _run():
        with patch("services.symbol_registry.get_config",      new=AsyncMock(return_value=_FAKE_CONFIG)), \
             patch("services.symbol_registry._fetch_thresholds", new=AsyncMock(return_value=_FAKE_THRESH)), \
             patch("services.symbol_registry.get_quotes_batch",  new=AsyncMock(return_value={"AAPL": {"last": 185.0}})), \
             patch("services.symbol_registry.get_expirations",   new=AsyncMock(side_effect=RuntimeError("api down"))):
            return await r.build()

    count = asyncio.get_event_loop().run_until_complete(_run())
    assert count == 0


def test_registry_build_chain_fetch_error_continues():
    r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

    async def _run():
        with patch("services.symbol_registry.get_config",      new=AsyncMock(return_value=_FAKE_CONFIG)), \
             patch("services.symbol_registry._fetch_thresholds", new=AsyncMock(return_value=_FAKE_THRESH)), \
             patch("services.symbol_registry.get_quotes_batch",  new=AsyncMock(return_value={"AAPL": {"last": 185.0}})), \
             patch("services.symbol_registry.get_expirations",   new=AsyncMock(return_value=[_NEAR_EXPIRY])), \
             patch("services.symbol_registry.get_option_chain",  new=AsyncMock(side_effect=RuntimeError("chain down"))):
            return await r.build()

    count = asyncio.get_event_loop().run_until_complete(_run())
    assert count == 0


def test_registry_lookup_after_build():
    r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

    async def _run():
        with patch("services.symbol_registry.get_config",      new=AsyncMock(return_value=_FAKE_CONFIG)), \
             patch("services.symbol_registry._fetch_thresholds", new=AsyncMock(return_value=_FAKE_THRESH)), \
             patch("services.symbol_registry.get_quotes_batch",  new=AsyncMock(return_value={"AAPL": {"last": 185.0}})), \
             patch("services.symbol_registry.get_expirations",   new=AsyncMock(return_value=[_NEAR_EXPIRY])), \
             patch("services.symbol_registry.get_option_chain",  new=AsyncMock(return_value=_FAKE_CHAIN)):
            await r.build()

    asyncio.get_event_loop().run_until_complete(_run())
    syms = r.all_symbols()
    assert len(syms) > 0
    meta = r.lookup(syms[0])
    assert isinstance(meta, ContractMeta)
    assert meta.ticker == "AAPL"
    oi_map = r.get_oi_map()
    assert "AAPL" in oi_map


# --- refresh_loop: one iteration then cancel ---

def test_refresh_loop_runs_one_iter_and_cancels():
    r = SymbolRegistry(watchlist=["AAPL"], tier_map={})

    async def _run():
        with patch("services.symbol_registry.get_config",      new=AsyncMock(return_value=_FAKE_CONFIG)), \
             patch("services.symbol_registry._fetch_thresholds", new=AsyncMock(return_value=_FAKE_THRESH)), \
             patch("services.symbol_registry.get_quotes_batch",  new=AsyncMock(return_value={"AAPL": {"last": 185.0}})), \
             patch("services.symbol_registry.get_expirations",   new=AsyncMock(return_value=[_NEAR_EXPIRY])), \
             patch("services.symbol_registry.get_option_chain",  new=AsyncMock(return_value=_FAKE_CHAIN)), \
             patch("asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            try:
                await r.refresh_loop()
            except asyncio.CancelledError:
                pass

    asyncio.get_event_loop().run_until_complete(_run())
