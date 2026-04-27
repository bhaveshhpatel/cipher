"""
Issue-6 regression: delta-chain fetch logic.

On a second build() call (warm restart), tickers whose expiries and OI
have NOT changed should reuse ContractMeta from the previous registry
(cache hit) and NOT call get_option_chain again.

Tickers with changed expiries OR OI drift > threshold must be re-fetched.
"""
import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.symbol_registry import SymbolRegistry, ContractMeta


_EXP_A = (date.today() + timedelta(days=14)).isoformat()
_EXP_B = (date.today() + timedelta(days=21)).isoformat()  # new expiry

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

_CHAIN_AAPL = [
    {"symbol": "AAPL231215C00180000", "strike": 180.0,
     "option_type": "C", "open_interest": 1000},
]

_CHAIN_TSLA = [
    {"symbol": "TSLA231215C00250000", "strike": 250.0,
     "option_type": "C", "open_interest": 800},
]

_PRE_FETCHED = {
    "AAPL": MagicMock(last=185.0, volume=5_000_000),
    "TSLA": MagicMock(last=250.0, volume=3_000_000),
}


def _make_chain_mock(ticker_chains: dict):
    """Return an AsyncMock that returns different chains per ticker."""
    async def _side_effect(ticker, expiry, **kwargs):
        return ticker_chains.get(ticker, [])
    m = AsyncMock(side_effect=_side_effect)
    return m


def _make_expiry_mock(ticker_expiries: dict):
    async def _side_effect(ticker, **kwargs):
        return ticker_expiries.get(ticker, [_EXP_A])
    return AsyncMock(side_effect=_side_effect)


def _patches(chain_mock, expiry_mock, quotes_mock=None):
    qm = quotes_mock or AsyncMock(return_value={})
    return [
        patch("services.ingestion_config.get_config",  new=AsyncMock(return_value=_FAKE_CONFIG)),
        patch("services.tier_engine._fetch_thresholds", new=AsyncMock(return_value=_FAKE_THRESH)),
        patch("services.symbol_registry.get_quotes_batch", new=qm),
        patch("services.symbol_registry.get_expirations",  new=expiry_mock),
        patch("services.symbol_registry.get_option_chain", new=chain_mock),
    ]


# ---------------------------------------------------------------------------
# Helper: run two consecutive builds on the same registry instance
# ---------------------------------------------------------------------------

def _two_builds(registry, chain_mock_1, expiry_mock_1,
                chain_mock_2, expiry_mock_2):
    """Run build() twice; return (count1, count2)."""
    async def _run():
        p1 = _patches(chain_mock_1, expiry_mock_1)
        with p1[0], p1[1], p1[2], p1[3], p1[4]:
            c1 = await registry.build(pre_fetched_quotes=_PRE_FETCHED)

        p2 = _patches(chain_mock_2, expiry_mock_2)
        with p2[0], p2[1], p2[2], p2[3], p2[4]:
            c2 = await registry.build(pre_fetched_quotes=_PRE_FETCHED)

        return c1, c2

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDeltaChainFetch:

    def test_unchanged_expiries_reuses_cache(self):
        """If expiries haven't changed, chain should NOT be re-fetched."""
        r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

        chain1 = _make_chain_mock({"AAPL": _CHAIN_AAPL})
        expiry1 = _make_expiry_mock({"AAPL": [_EXP_A]})
        chain2 = _make_chain_mock({"AAPL": _CHAIN_AAPL})
        expiry2 = _make_expiry_mock({"AAPL": [_EXP_A]})  # same expiries

        c1, c2 = _two_builds(r, chain1, expiry1, chain2, expiry2)

        assert c1 >= 1
        assert c2 >= 1
        # chain fetch should NOT have been called on second build for AAPL
        assert chain2.call_count == 0

    def test_new_expiry_triggers_refetch(self):
        """A new expiry date must trigger a full chain re-fetch for that ticker."""
        r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

        chain1 = _make_chain_mock({"AAPL": _CHAIN_AAPL})
        expiry1 = _make_expiry_mock({"AAPL": [_EXP_A]})
        chain2 = _make_chain_mock({"AAPL": _CHAIN_AAPL})
        expiry2 = _make_expiry_mock({"AAPL": [_EXP_A, _EXP_B]})  # new expiry added

        _two_builds(r, chain1, expiry1, chain2, expiry2)

        # chain2 must have been called at least once for AAPL
        assert chain2.call_count >= 1

    def test_oi_drift_above_threshold_triggers_refetch(self):
        """OI drift > 20% must trigger chain re-fetch even when expiries unchanged."""
        chain_high_oi = [
            {"symbol": "AAPL231215C00180000", "strike": 180.0,
             "option_type": "C", "open_interest": 5000},  # was 1000 → 400% drift
        ]
        r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

        chain1 = _make_chain_mock({"AAPL": _CHAIN_AAPL})
        expiry1 = _make_expiry_mock({"AAPL": [_EXP_A]})
        chain2 = _make_chain_mock({"AAPL": chain_high_oi})
        expiry2 = _make_expiry_mock({"AAPL": [_EXP_A]})  # expiries unchanged

        _two_builds(r, chain1, expiry1, chain2, expiry2)

        assert chain2.call_count >= 1

    def test_oi_drift_below_threshold_no_refetch(self):
        """OI drift <= 20% with same expiries must NOT trigger re-fetch."""
        chain_small_oi_change = [
            {"symbol": "AAPL231215C00180000", "strike": 180.0,
             "option_type": "C", "open_interest": 1050},  # +5% drift, under threshold
        ]
        r = SymbolRegistry(watchlist=["AAPL"], tier_map={"AAPL": 1})

        chain1 = _make_chain_mock({"AAPL": _CHAIN_AAPL})
        expiry1 = _make_expiry_mock({"AAPL": [_EXP_A]})
        chain2 = _make_chain_mock({"AAPL": chain_small_oi_change})
        expiry2 = _make_expiry_mock({"AAPL": [_EXP_A]})

        _two_builds(r, chain1, expiry1, chain2, expiry2)

        assert chain2.call_count == 0

    def test_multiple_tickers_only_changed_refetched(self):
        """AAPL (unchanged) stays cached; TSLA (new expiry) gets re-fetched."""
        r = SymbolRegistry(watchlist=["AAPL", "TSLA"],
                           tier_map={"AAPL": 1, "TSLA": 2})

        chain1 = _make_chain_mock({"AAPL": _CHAIN_AAPL, "TSLA": _CHAIN_TSLA})
        expiry1 = _make_expiry_mock({"AAPL": [_EXP_A], "TSLA": [_EXP_A]})
        chain2 = _make_chain_mock({"AAPL": _CHAIN_AAPL, "TSLA": _CHAIN_TSLA})
        expiry2 = _make_expiry_mock({
            "AAPL": [_EXP_A],            # unchanged
            "TSLA": [_EXP_A, _EXP_B],   # new expiry
        })

        _two_builds(r, chain1, expiry1, chain2, expiry2)

        # chain2 should be called for TSLA only
        called_tickers = {c.args[0] for c in chain2.call_args_list}
        assert "TSLA" in called_tickers
        assert "AAPL" not in called_tickers

    def test_first_build_always_fetches_chain(self):
        """On cold start (no cache), every ticker must fetch its chain."""
        r = SymbolRegistry(watchlist=["AAPL", "TSLA"],
                           tier_map={"AAPL": 1, "TSLA": 2})

        chain1 = _make_chain_mock({"AAPL": _CHAIN_AAPL, "TSLA": _CHAIN_TSLA})
        expiry1 = _make_expiry_mock({"AAPL": [_EXP_A], "TSLA": [_EXP_A]})

        async def _run():
            p = _patches(chain1, expiry1)
            with p[0], p[1], p[2], p[3], p[4]:
                return await r.build(pre_fetched_quotes=_PRE_FETCHED)

        asyncio.run(_run())
        fetched = {c.args[0] for c in chain1.call_args_list}
        assert "AAPL" in fetched
        assert "TSLA" in fetched
