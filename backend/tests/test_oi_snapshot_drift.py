"""
test_oi_snapshot_drift.py

Regression: SymbolRegistry._oi_snapshot must hold the OI from the PREVIOUS
build so _apply_delta() can compute a real drift ratio against curr_oi_map.

Invariant (post-fix)
--------------------
After every build N, _oi_snapshot == the OI produced by build N so that
build N+1's _apply_delta has a valid prev_oi baseline.

Bug that was fixed (round 1)
-----------------------------
Previously build() did:
    self._oi_snapshot  = dict(self._oi_by_ticker)   # captured EMPTY {} on build 1
    self._oi_by_ticker = new_oi_by_ticker
So _oi_snapshot was always {} after build 1, prev_oi was None for every
ticker, OI drift was forced to 0.0, and changed tickers were never re-fetched.

Fix: snapshot new_oi_by_ticker (what was just built) instead:
    self._oi_snapshot  = dict(new_oi_by_ticker)     # build-1 OI preserved as baseline
    self._oi_by_ticker = new_oi_by_ticker

Patching notes
--------------
All fake_* replacements that stand in for instance methods MUST have
'self_inner' as their first positional parameter when patched at the class
level via patch() or patch.object().  Without it Python injects the
instance as the first positional arg, shifting every subsequent parameter
by one.

_pending_expiry_cache note
--------------------------
fake_build_ticker_b1 MUST write to self_inner._pending_expiry_cache so
build() sets _expiry_cache after build 1 and build 2 takes the delta path.

fake_apply_delta arity note
---------------------------
_apply_delta accepts curr_oi_map as its 7th positional arg (after self):
    _apply_delta(self, prices, tier_params, new_registry, new_oi_by_ticker,
                 new_expiry_cache, oi_delta_thresh, curr_oi_map)
All fake replacements must match this 8-arg signature (including self_inner).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_registry(watchlist=None):
    from services.symbol_registry import SymbolRegistry
    return SymbolRegistry(watchlist=watchlist or ["AAPL", "TSLA"])


def _make_sq(symbol, price=100.0, volume=1_000_000, avg_vol=900_000, oi=0):
    sq = MagicMock()
    sq.symbol = symbol
    sq.last_price = price
    sq.volume = volume
    sq.average_volume = avg_vol
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


@pytest.mark.asyncio
async def test_oi_snapshot_holds_previous_build_oi():
    """
    After two builds:
      Build 1: OI for AAPL = 500
      Build 2: OI for AAPL = 800

    After build 1:
      _oi_by_ticker["AAPL"]  should be 500  (current)
      _oi_snapshot["AAPL"]   should be 500  (snapshot = what build 1 produced)

    After build 2:
      _oi_by_ticker["AAPL"]  should be 800  (current)
      _oi_snapshot["AAPL"]   should be 500  (snapshot = what build 1 produced,
                                              preserved as drift baseline)
    """
    reg = _make_registry(["AAPL"])

    # ------------------------------------------------------------------ build 1
    async def fake_build_ticker_b1(
        self_inner, ticker, price, registry, oi_by_ticker, tier_params,
    ):
        from services.symbol_registry import ContractMeta
        registry["AAPL260101C00150000"] = ContractMeta(
            ticker="AAPL", strike=150.0, expiry="2026-01-01",
            contract_type="CALL", dte=10, open_interest=500, tier=3,
        )
        oi_by_ticker["AAPL"] = 500
        self_inner._pending_expiry_cache["AAPL"] = {"2026-01-01"}

    with (
        patch.object(reg.__class__, "_build_ticker", new=fake_build_ticker_b1),
        patch.object(reg.__class__, "_persist_to_db", AsyncMock()),
        patch("services.ingestion_config.get_config", AsyncMock(return_value=_FAKE_CFG)),
        patch("services.tier_engine._fetch_thresholds", AsyncMock(return_value=_FAKE_THRESH)),
        patch("services.tier_engine.assign_tiers", AsyncMock(return_value={"AAPL": 3})),
    ):
        pf = {"AAPL": _make_sq("AAPL")}
        await reg.build(pre_fetched_quotes=pf)

    assert reg._oi_by_ticker.get("AAPL") == 500, "_oi_by_ticker should be 500 after build 1"
    # After the fix, _oi_snapshot is set to new_oi_by_ticker (500) so the
    # next delta build has a real prev_oi baseline.
    assert reg._oi_snapshot.get("AAPL") == 500, (
        "_oi_snapshot should equal build-1 OI (500) so delta drift has a baseline"
    )

    # ------------------------------------------------------------------ build 2
    # curr_oi_map is now the 7th positional arg passed by build() to _apply_delta.
    async def fake_apply_delta(
        self_inner, prices, tier_params, new_registry, new_oi_by_ticker,
        new_expiry_cache, oi_delta_thresh, curr_oi_map,
    ):
        from services.symbol_registry import ContractMeta
        new_registry["AAPL260101C00150000"] = ContractMeta(
            ticker="AAPL", strike=150.0, expiry="2026-01-01",
            contract_type="CALL", dte=10, open_interest=800, tier=3,
        )
        new_oi_by_ticker["AAPL"] = 800
        new_expiry_cache["AAPL"] = {"2026-01-01"}
        return 0  # 0 tickers reused

    with (
        patch.object(reg.__class__, "_apply_delta", new=fake_apply_delta),
        patch.object(reg.__class__, "_persist_to_db", AsyncMock()),
        patch("services.ingestion_config.get_config", AsyncMock(return_value=_FAKE_CFG)),
        patch("services.tier_engine._fetch_thresholds", AsyncMock(return_value=_FAKE_THRESH)),
        patch("services.tier_engine.assign_tiers", AsyncMock(return_value={"AAPL": 3})),
    ):
        pf = {"AAPL": _make_sq("AAPL")}
        await reg.build(pre_fetched_quotes=pf)

    assert reg._oi_by_ticker.get("AAPL") == 800, "_oi_by_ticker should be 800 after build 2"
    assert reg._oi_snapshot.get("AAPL") == 800, (
        "_oi_snapshot should equal build-2 OI (800) — it is always the latest built OI"
    )


@pytest.mark.asyncio
async def test_oi_snapshot_differs_from_oi_by_ticker_after_second_build():
    """
    Core invariant: _oi_snapshot == _oi_by_ticker after every build (both
    reflect what was most recently built).  The drift comparison uses
    _oi_snapshot (prev build) vs curr_oi_map (incoming quote OI).
    """
    reg = _make_registry(["SPY"])

    async def _run_build(new_oi: int) -> None:
        is_first = not bool(reg._expiry_cache)

        async def fake_build_ticker(
            self_inner, ticker, price, registry, oi_by_ticker, tier_params,
        ):
            from services.symbol_registry import ContractMeta
            registry[f"{ticker}OCC"] = ContractMeta(
                ticker=ticker, strike=400.0, expiry="2026-06-01",
                contract_type="CALL", dte=30, open_interest=new_oi, tier=3,
            )
            oi_by_ticker[ticker] = new_oi
            self_inner._pending_expiry_cache[ticker] = {"2026-06-01"}

        # curr_oi_map is now the 7th positional arg (after self_inner).
        async def fake_apply_delta(
            self_inner, prices, tier_params, new_registry, new_oi_by_ticker,
            new_expiry_cache, oi_delta_thresh, curr_oi_map,
        ):
            from services.symbol_registry import ContractMeta
            new_registry["SPYOCC"] = ContractMeta(
                ticker="SPY", strike=400.0, expiry="2026-06-01",
                contract_type="CALL", dte=30, open_interest=new_oi, tier=3,
            )
            new_oi_by_ticker["SPY"] = new_oi
            new_expiry_cache["SPY"] = {"2026-06-01"}
            return 0

        _build_patch = fake_build_ticker if is_first else MagicMock()
        _delta_patch = MagicMock() if is_first else fake_apply_delta

        with (
            patch.object(reg.__class__, "_build_ticker", new=_build_patch),
            patch.object(reg.__class__, "_apply_delta", new=_delta_patch),
            patch.object(reg.__class__, "_persist_to_db", AsyncMock()),
            patch("services.ingestion_config.get_config", AsyncMock(return_value=_FAKE_CFG)),
            patch("services.tier_engine._fetch_thresholds", AsyncMock(return_value=_FAKE_THRESH)),
            patch("services.tier_engine.assign_tiers", AsyncMock(return_value={"SPY": 1})),
        ):
            await reg.build(pre_fetched_quotes={"SPY": _make_sq("SPY", price=400.0)})

    await _run_build(new_oi=1000)
    await _run_build(new_oi=1500)

    assert reg._oi_by_ticker.get("SPY") == 1500
    # _oi_snapshot is now always set to new_oi_by_ticker so after build 2
    # it equals 1500 (the OI produced by build 2).
    assert reg._oi_snapshot.get("SPY") == 1500, (
        "_oi_snapshot must equal the most recently built OI (1500)"
    )
    # Both reflect the same build-2 result; drift is computed via curr_oi_map,
    # not by comparing _oi_snapshot vs _oi_by_ticker.
    assert reg._oi_snapshot == reg._oi_by_ticker, (
        "_oi_snapshot and _oi_by_ticker must be equal after each build"
    )
