"""
Coverage boost for services/symbol_registry.py.

get_config and _fetch_thresholds are imported at module level in
services/symbol_registry.py (H3 fix), so patches must target the
symbol_registry module namespace:
  - services.symbol_registry.get_config  (via services.ingestion_config)
  - services.symbol_registry._fetch_thresholds (via services.tier_engine)

FIX (2026-04-28):
  - Patch target changed from 'services.symbol_registry.get_option_chain'
    to 'services.symbol_registry.get_option_chain_bulk' (FIX P3).
  - build() now returns tuple[int, dict], not int.  All callers updated.
  - _persist_to_db patched to avoid live Supabase calls in tests.
"""
import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

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
    assert _reg().is_ready() is False

def test_registry_lookup_missing_returns_none():
    assert _reg().lookup("AAPL231215C00180000") is None

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


# --- Shared patch helpers ---

_FAKE_THRESH = {
    "t1_atm_pct": 0.20, "t1_max_dte": 90, "t1_min_oi": 0,
    "t2_atm_pct": 0.15, "t2_max_dte": 60, "t2_min_oi": 0,
    "t3_atm_pct": 0.10, "t3_max_dte": 30, "t3_min_oi": 0,
}

_FAKE_CONFIG = {
    "REGISTRY_MIN_OI": 0,
    "REGISTRY_REFRESH_MINS": 60,
    "REGISTRY_EXPIRY_DAY_REFRESH_MINS": 10,
    "REGISTRY_BUILD_CONCURRENCY": 4,
}

_NEAR_EXPIRY = (date.today() + timedelta(days=14)).isoformat()

_FAKE_CHAIN = [
    {"symbol": "AAPL231215C00180000", "strike": 180.0,
     "option_type": "C", "open_interest": 1000},
    {"symbol": "AAPL231215P00175000", "strike": 175.0,
     "option_type": "P", "open_interest": 500},
    # This strike is outside ATM window for price=185, so should be filtered
    {"symbol": "AAPL231215C00999000", "strike": 999.0,
     "option_type": "C", "open_interest": 100},
]


def _patches(chain=_FAKE_CHAIN, expirations=None, expirations_side_effect=None,
             chain_side_effect=None):
    """Return a list of patch() context managers for build() dependencies."""
    expiry_mock = AsyncMock(return_value=expirations if expirations is not None else [_NEAR_EXPIRY])
    if expirations_side_effect:
        expiry_mock.side_effect = expirations_side_effect

    chain_mock = AsyncMock(return_value=chain)
    if chain_side_effect:
        chain_mock.side_effect = chain_side_effect

    return [
        patch("services.ingestion_config.get_config",     new=AsyncMock(return_value=_FAKE_CONFIG)),
        patch("services.tier_engine._fetch_thresholds",   new=AsyncMock(return_value=_FAKE_THRESH)),
        patch("services.symbol_registry.get_quotes_batch",new=AsyncMock(return_value={"AAPL": {"last": 185.0}})),
        patch("services.symbol_registry.get_expirations", new=expiry_mock),
        # FIX: P3 renamed get_option_chain -> get_option_chain_bulk
        patch("services.symbol_registry.get_option_chain_bulk", new=chain_mock),
        patch.object(SymbolRegistry, "_persist_to_db",    new=AsyncMock()),
    ]


# --- build() tests ---

def test_registry_build_populates_contracts():
    r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

    async def _run2():
        p = _patches()
        with p[0], p[1], p[2], p[3], p[4], p[5]:
            return await r.build()

    # H1: build() returns tuple[int, dict]
    count, _raw = asyncio.run(_run2())
    assert count >= 1
    assert r.is_ready()
    assert r.size() == count
    assert r.stock_price("AAPL") == pytest.approx(185.0)


def test_registry_build_skips_ticker_with_no_price():
    r = SymbolRegistry(watchlist=["AAPL", "TSLA"], tier_map={})

    async def _run():
        p = _patches()
        with p[0], p[1], p[2]:
            return await r.build()

    asyncio.run(_run())
    assert r._stock_prices.get("TSLA", 0) == 0


def test_registry_build_ticker_expiry_fetch_error():
    r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

    async def _run():
        p = _patches(expirations_side_effect=RuntimeError("api down"))
        # Only need first 4 patches (no chain needed — expiry raises first)
        with p[0], p[1], p[2], p[3], p[5]:
            return await r.build()

    # H1: build() returns tuple[int, dict] — unpack and check count
    count, _raw = asyncio.run(_run())
    assert count == 0


def test_registry_build_chain_fetch_error_continues():
    r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

    async def _run():
        p = _patches(chain_side_effect=RuntimeError("chain down"))
        with p[0], p[1], p[2], p[3], p[4], p[5]:
            return await r.build()

    count, _raw = asyncio.run(_run())
    assert count == 0


def test_registry_lookup_after_build():
    r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

    async def _run():
        p = _patches()
        with p[0], p[1], p[2], p[3], p[4], p[5]:
            await r.build()

    asyncio.run(_run())
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
        p = _patches()
        with p[0], p[1], p[2], p[3], p[4], p[5]:
            with patch("asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
                try:
                    await r.refresh_loop()
                except asyncio.CancelledError:
                    pass

    asyncio.run(_run())
