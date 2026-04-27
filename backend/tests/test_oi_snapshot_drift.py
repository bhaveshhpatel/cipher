"""
test_oi_snapshot_drift.py

Regression: SymbolRegistry._oi_snapshot must hold the OI from the PREVIOUS
build, not the current build.  _apply_delta() compares _oi_snapshot (prev)
vs curr_oi_map (incoming quote OI) to decide whether a ticker's OI has
drifted beyond the threshold and needs a chain re-fetch.

Bug that was fixed
------------------
Previously in build():
    self._oi_by_ticker = new_oi_by_ticker
    self._oi_snapshot  = dict(new_oi_by_ticker)   # ← both set to NEW values

This made _oi_snapshot == _oi_by_ticker after every build, so oi_drift in
_apply_delta was always 0 — meaning OI drift never triggered a chain re-fetch.

Fix: snapshot the OLD _oi_by_ticker BEFORE overwriting it:
    self._oi_snapshot  = dict(self._oi_by_ticker)  # prev OI preserved
    self._oi_by_ticker = new_oi_by_ticker

Patching notes
--------------
All fake_* replacements that stand in for instance methods MUST have
'self_inner' as their first positional parameter when patched at the class
level via patch() or patch.object().  Without it, Python injects the
instance as the first positional arg, shifting every subsequent parameter
by one (self → ticker, ticker → price, etc.) and corrupting the call.

_pending_expiry_cache note
--------------------------
After build 1, build() does:
    self._expiry_cache = dict(new_expiry_cache)   # new_expiry_cache built from
                                                   # self._pending_expiry_cache
So fake_build_ticker_b1 MUST write to self_inner._pending_expiry_cache
(not the old expiry_cache_out kwarg) so that _expiry_cache is non-empty
after build 1 and build 2 correctly takes the delta path.

fake_apply_delta arity note
---------------------------
_apply_delta now accepts curr_oi_map as its 7th positional arg (after self):
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

    After build 2:
      _oi_by_ticker["AAPL"]  should be 800  (current)
      _oi_snapshot["AAPL"]   should be 500  (previous, for drift detection)
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
    assert reg._oi_snapshot.get("AAPL", None) is None, (
        "_oi_snapshot should hold pre-build-1 OI (none) after first build"
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
    assert reg._oi_snapshot.get("AAPL") == 500, (
        "_oi_snapshot should preserve OI from build 1 (500), not build 2 (800)"
    )


@pytest.mark.asyncio
async def test_oi_snapshot_differs_from_oi_by_ticker_after_second_build():
    """
    Core invariant: after any build N>1, _oi_snapshot != _oi_by_ticker
    if OI actually changed between builds.
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
    assert reg._oi_snapshot.get("SPY") == 1000, (
        "_oi_snapshot must hold pre-build OI (1000) so drift detection works"
    )
    assert reg._oi_snapshot != reg._oi_by_ticker, (
        "_oi_snapshot and _oi_by_ticker must differ when OI changed between builds"
    )
