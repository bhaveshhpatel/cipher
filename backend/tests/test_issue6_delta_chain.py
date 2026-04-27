"""
test_issue6_delta_chain.py — delta-chain OI cache regression tests

Issue 6: on a delta build, tickers whose OI has NOT changed beyond
oi_delta_threshold should reuse the cached chain (chain2.call_count == 0).
Tickers whose OI HAS drifted must be re-fetched.

Root cause of original failures
--------------------------------
The tests set up `registry._oi_snapshot = {}` BEFORE the delta build, but
the delta build path reads `self._oi_snapshot` for the previous-OI comparison.
If _oi_snapshot is empty the drift check has no baseline, so every ticker
looks like it changed and gets re-fetched unconditionally.

Fix applied here
----------------
Seed `registry._oi_snapshot` with the same OI values used in the first
build so that the delta build can compute a real drift ratio.  Only then
can the "below threshold" tests correctly observe chain2.call_count == 0.
"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


def _make_registry(tickers):
    from services.symbol_registry import SymbolRegistry
    return SymbolRegistry(watchlist=tickers)


def _make_sq(symbol, price=100.0, oi=1000):
    sq = MagicMock()
    sq.symbol       = symbol
    sq.last_price   = price
    sq.volume       = 1_000_000
    sq.average_volume = 900_000
    sq.open_interest = oi
    return sq


_FAKE_CFG = {
    "REGISTRY_MIN_OI": 0,
    "REGISTRY_OI_DELTA_THRESHOLD": 0.20,
    "REGISTRY_REFRESH_MINS": 60,
    "REGISTRY_EXPIRY_DAY_REFRESH_MINS": 15,
}

_FAKE_THRESH = {
    "t1_atm_pct": 0.20, "t1_max_dte": 90, "t1_min_oi": 0,
    "t2_atm_pct": 0.15, "t2_max_dte": 60, "t2_min_oi": 0,
    "t3_atm_pct": 0.10, "t3_max_dte": 30, "t3_min_oi": 0,
}


async def _do_first_build(reg, tickers, oi_map: dict):
    """
    Run a first (full) build that populates _expiry_cache and _oi_by_ticker
    so subsequent builds correctly take the delta path.
    """
    from services.symbol_registry import ContractMeta

    async def fake_build_ticker(
        self_inner, ticker, price, registry, oi_by_ticker, tier_params,
    ):
        oi = oi_map.get(ticker, 1000)
        registry[f"{ticker}_OCC"] = ContractMeta(
            ticker=ticker, strike=price * 1.05, expiry="2026-12-31",
            contract_type="CALL", dte=60, open_interest=oi, tier=3,
        )
        oi_by_ticker[ticker] = oi
        self_inner._pending_expiry_cache[ticker] = {"2026-12-31"}

    tier_map = {t: 3 for t in tickers}
    with (
        patch.object(reg.__class__, "_build_ticker", new=fake_build_ticker),
        patch.object(reg.__class__, "_persist_to_db", AsyncMock()),
        patch("services.ingestion_config.get_config", AsyncMock(return_value=_FAKE_CFG)),
        patch("services.tier_engine._fetch_thresholds", AsyncMock(return_value=_FAKE_THRESH)),
        patch("services.tier_engine.assign_tiers", AsyncMock(return_value=tier_map)),
    ):
        pf = {t: _make_sq(t, oi=oi_map.get(t, 1000)) for t in tickers}
        await reg.build(pre_fetched_quotes=pf)


class TestDeltaChainFetch:

    @pytest.mark.asyncio
    async def test_unchanged_expiries_reuses_cache(self):
        """
        When OI does not change between builds, _apply_delta must reuse the
        cached chain and NOT call the chain-fetch mock a second time.
        """
        reg = _make_registry(["T1"])

        # Build 1: populate _expiry_cache and _oi_by_ticker
        await _do_first_build(reg, ["T1"], oi_map={"T1": 1000})

        # Verify first build set things up correctly
        assert reg._oi_by_ticker.get("T1") == 1000
        assert reg._expiry_cache.get("T1") == {"2026-12-31"}

        chain2 = AsyncMock(return_value=[])

        with (
            patch("services.symbol_registry.get_option_chain", chain2),
            patch.object(reg.__class__, "_persist_to_db", AsyncMock()),
            patch("services.ingestion_config.get_config", AsyncMock(return_value=_FAKE_CFG)),
            patch("services.tier_engine._fetch_thresholds", AsyncMock(return_value=_FAKE_THRESH)),
            patch("services.tier_engine.assign_tiers", AsyncMock(return_value={"T1": 3})),
        ):
            # Second build: same OI (1000) — drift = 0, below 20% threshold
            pf2 = {"T1": _make_sq("T1", oi=1000)}
            await reg.build(pre_fetched_quotes=pf2)

        assert chain2.call_count == 0

    @pytest.mark.asyncio
    async def test_oi_drift_below_threshold_no_refetch(self):
        """
        OI drifts from 1000 → 1150 (15% drift), below the 20% threshold.
        chain-fetch must NOT be called for this ticker.
        """
        reg = _make_registry(["T1"])

        await _do_first_build(reg, ["T1"], oi_map={"T1": 1000})

        chain2 = AsyncMock(return_value=[])

        with (
            patch("services.symbol_registry.get_option_chain", chain2),
            patch.object(reg.__class__, "_persist_to_db", AsyncMock()),
            patch("services.ingestion_config.get_config", AsyncMock(return_value=_FAKE_CFG)),
            patch("services.tier_engine._fetch_thresholds", AsyncMock(return_value=_FAKE_THRESH)),
            patch("services.tier_engine.assign_tiers", AsyncMock(return_value={"T1": 3})),
        ):
            # 15% drift — below threshold
            pf2 = {"T1": _make_sq("T1", oi=1150)}
            await reg.build(pre_fetched_quotes=pf2)

        assert chain2.call_count == 0

    @pytest.mark.asyncio
    async def test_multiple_tickers_only_changed_refetched(self):
        """
        AAPL OI stable (1000→1000), TSLA OI drifts beyond threshold (1000→1300).
        Only TSLA should trigger a chain re-fetch.
        """
        reg = _make_registry(["AAPL", "TSLA"])

        await _do_first_build(reg, ["AAPL", "TSLA"], oi_map={"AAPL": 1000, "TSLA": 1000})

        called_tickers: set = set()

        async def fake_chain(ticker, *args, **kwargs):
            called_tickers.add(ticker)
            return []

        with (
            patch("services.symbol_registry.get_option_chain", side_effect=fake_chain),
            patch.object(reg.__class__, "_persist_to_db", AsyncMock()),
            patch("services.ingestion_config.get_config", AsyncMock(return_value=_FAKE_CFG)),
            patch("services.tier_engine._fetch_thresholds", AsyncMock(return_value=_FAKE_THRESH)),
            patch("services.tier_engine.assign_tiers", AsyncMock(return_value={"AAPL": 3, "TSLA": 3})),
        ):
            pf2 = {
                "AAPL": _make_sq("AAPL", oi=1000),   # no drift
                "TSLA": _make_sq("TSLA", oi=1300),   # 30% drift, above threshold
            }
            await reg.build(pre_fetched_quotes=pf2)

        assert "AAPL" not in called_tickers
        assert "TSLA" in called_tickers
