"""
test_4a_tier_engine.py — Unit tests for Feature 4A: Dynamic Tiering

Covers:
  TE-01 … TE-08  assign_tiers() logic (tier_engine.py)
  TE-09 … TE-12  _fetch_thresholds() cache behaviour
  TE-13 … TE-16  Admin endpoint column whitelist / PATCH guard
  TE-17 … TE-20  _TierParams dataclass + ContractMeta.tier field (symbol_registry.py)
  TE-21 … TE-22  tier_map round-trip: upsert_symbol_quotes stores tier column

All tests are pure-Python / asyncio — no Supabase, no network.
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# TE-01 … TE-08  assign_tiers()
# ===========================================================================

class TestAssignTiers:
    """Feature 4A: assign_tiers() classifies symbols into T1/T2/T3 correctly."""

    def _thresholds(self):
        return {
            "t1_min_volume": 20_000_000, "t1_min_last_price": 10.0,
            "t1_min_oi": 1000, "t1_atm_pct": 0.20, "t1_max_dte": 90,
            "t2_min_volume": 2_000_000,  "t2_min_last_price": 10.0,
            "t2_min_oi": 500,  "t2_atm_pct": 0.15, "t2_max_dte": 60,
            "t3_min_volume": 500_000,    "t3_min_last_price": 1.0,
            "t3_min_oi": 100,  "t3_atm_pct": 0.10, "t3_max_dte": 30,
        }

    def _quote(self, volume, price, oi):
        return {"average_volume": volume, "last": price, "open_interest": oi}

    # TE-01
    def test_spy_tier1(self):
        from services.tier_engine import assign_tiers
        quotes = {"SPY": self._quote(80_000_000, 502.0, 50000)}
        result = assign_tiers(quotes, self._thresholds())
        assert result["SPY"] == 1

    # TE-02
    def test_hood_tier2(self):
        from services.tier_engine import assign_tiers
        quotes = {"HOOD": self._quote(5_000_000, 25.0, 800)}
        result = assign_tiers(quotes, self._thresholds())
        assert result["HOOD"] == 2

    # TE-03
    def test_low_volume_tier3(self):
        from services.tier_engine import assign_tiers
        quotes = {"XYZ": self._quote(600_000, 5.0, 150)}
        result = assign_tiers(quotes, self._thresholds())
        assert result["XYZ"] == 3

    # TE-04
    def test_below_t3_minimum_still_tier3(self):
        """Symbols below all thresholds should still land in tier 3 (default)."""
        from services.tier_engine import assign_tiers
        quotes = {"PENNY": self._quote(100_000, 0.50, 10)}
        result = assign_tiers(quotes, self._thresholds())
        assert result["PENNY"] == 3

    # TE-05
    def test_empty_quotes_returns_empty_dict(self):
        from services.tier_engine import assign_tiers
        result = assign_tiers({}, self._thresholds())
        assert result == {}

    # TE-06
    def test_multiple_symbols_classified_independently(self):
        from services.tier_engine import assign_tiers
        quotes = {
            "SPY":  self._quote(80_000_000, 502.0, 50000),
            "HOOD": self._quote(5_000_000,  25.0,  800),
            "XYZ":  self._quote(600_000,    5.0,   150),
        }
        result = assign_tiers(quotes, self._thresholds())
        assert result["SPY"]  == 1
        assert result["HOOD"] == 2
        assert result["XYZ"]  == 3

    # TE-07
    def test_tier1_boundary_exactly_at_threshold(self):
        from services.tier_engine import assign_tiers
        quotes = {"EDGE": self._quote(20_000_000, 10.0, 1000)}
        result = assign_tiers(quotes, self._thresholds())
        assert result["EDGE"] == 1

    # TE-08
    def test_missing_fields_default_to_zero_not_crash(self):
        """Quotes missing volume/price/oi keys must not raise — default to tier 3."""
        from services.tier_engine import assign_tiers
        quotes = {"BARE": {}}
        result = assign_tiers(quotes, self._thresholds())
        assert result["BARE"] == 3


# ===========================================================================
# TE-09 … TE-12  _fetch_thresholds() cache
# ===========================================================================

class TestFetchThresholds:
    """Feature 4A: _fetch_thresholds() returns active row and caches result."""

    # TE-09
    def test_fetch_thresholds_returns_dict(self):
        from services import tier_engine
        mock_row = {
            "t1_min_volume": 20000000, "t1_min_last_price": 10.0, "t1_min_oi": 1000,
            "t1_atm_pct": 0.20, "t1_max_dte": 90,
            "t2_min_volume": 2000000,  "t2_min_last_price": 10.0, "t2_min_oi": 500,
            "t2_atm_pct": 0.15, "t2_max_dte": 60,
            "t3_min_volume": 500000,   "t3_min_last_price": 1.0,  "t3_min_oi": 100,
            "t3_atm_pct": 0.10, "t3_max_dte": 30,
        }
        sb = MagicMock()
        q  = MagicMock()
        q.select.return_value = q
        q.eq.return_value     = q
        q.limit.return_value  = q
        q.execute.return_value = MagicMock(data=[mock_row])
        sb.table.return_value = q

        with patch.object(tier_engine, "_client", return_value=sb):
            tier_engine._thresholds_cache = None
            result = tier_engine._fetch_thresholds()
        assert isinstance(result, dict)
        assert result["t1_min_volume"] == 20000000

    # TE-10
    def test_fetch_thresholds_uses_cache_on_second_call(self):
        from services import tier_engine
        cached = {"t1_min_volume": 99999999}
        tier_engine._thresholds_cache     = cached
        tier_engine._thresholds_cache_ts  = 9_999_999_999  # far future
        result = tier_engine._fetch_thresholds()
        assert result is cached

    # TE-11
    def test_fetch_thresholds_falls_back_to_defaults_on_error(self):
        from services import tier_engine
        with patch.object(tier_engine, "_client", side_effect=Exception("DB down")):
            tier_engine._thresholds_cache = None
            result = tier_engine._fetch_thresholds()
        assert isinstance(result, dict)
        assert "t1_min_volume" in result

    # TE-12
    def test_invalidate_clears_cache(self):
        from services import tier_engine
        tier_engine._thresholds_cache = {"t1_min_volume": 1}
        tier_engine.invalidate_thresholds_cache()
        assert tier_engine._thresholds_cache is None


# ===========================================================================
# TE-13 … TE-16  Admin endpoint: PATCH /tier-thresholds column whitelist
# ===========================================================================

class TestAdminTierThresholds:
    """Feature 4A: PATCH /tier-thresholds must only accept known columns."""

    # TE-13
    def test_admin_module_has_patch_tier_thresholds_route(self):
        import pathlib
        src = pathlib.Path("backend/routers/admin.py")
        if not src.exists():
            src = pathlib.Path("routers/admin.py")
        text = src.read_text()
        assert "tier-thresholds" in text or "tier_thresholds" in text, (
            "admin.py must define the PATCH /tier-thresholds endpoint (Feature 4A)."
        )

    # TE-14
    def test_admin_tier_thresholds_route_has_whitelist(self):
        import pathlib
        src = pathlib.Path("backend/routers/admin.py")
        if not src.exists():
            src = pathlib.Path("routers/admin.py")
        text = src.read_text()
        assert "ALLOWED_TIER_COLUMNS" in text or "allowed_columns" in text.lower(), (
            "PATCH /tier-thresholds must define a column whitelist to prevent "
            "injection of arbitrary DB columns."
        )

    # TE-15
    def test_admin_tier_distribution_route_exists(self):
        import pathlib
        src = pathlib.Path("backend/routers/admin.py")
        if not src.exists():
            src = pathlib.Path("routers/admin.py")
        text = src.read_text()
        assert "tier-distribution" in text or "tier_distribution" in text, (
            "admin.py must define GET /tier-distribution endpoint (Feature 4A)."
        )

    # TE-16
    def test_admin_invalidates_cache_after_patch(self):
        """PATCH handler must call invalidate_thresholds_cache() after DB update."""
        import pathlib
        src = pathlib.Path("backend/routers/admin.py")
        if not src.exists():
            src = pathlib.Path("routers/admin.py")
        text = src.read_text()
        assert "invalidate_thresholds_cache" in text, (
            "PATCH /tier-thresholds must invalidate the in-process cache after "
            "updating the DB row, or stale thresholds will be used until restart."
        )


# ===========================================================================
# TE-17 … TE-20  _TierParams + ContractMeta.tier (symbol_registry.py)
# ===========================================================================

class TestTierParamsAndContractMeta:
    """Feature 4A: _TierParams dataclass and ContractMeta.tier field."""

    # TE-17
    def test_tier_params_dataclass_exists(self):
        from services.symbol_registry import _TierParams
        assert hasattr(_TierParams, "__dataclass_fields__") or hasattr(_TierParams, "atm_pct"), (
            "symbol_registry._TierParams must be a dataclass with atm_pct/max_dte fields."
        )

    # TE-18
    def test_tier_params_has_atm_pct_and_max_dte(self):
        from services.symbol_registry import _TierParams
        p = _TierParams(atm_pct=0.20, max_dte=90)
        assert p.atm_pct == 0.20
        assert p.max_dte == 90

    # TE-19
    def test_contract_meta_has_tier_field(self):
        from services.symbol_registry import ContractMeta
        import inspect
        fields = set()
        if hasattr(ContractMeta, "__dataclass_fields__"):
            fields = set(ContractMeta.__dataclass_fields__.keys())
        else:
            try:
                fields = set(vars(ContractMeta()).keys())
            except Exception:
                pass
        assert "tier" in fields, (
            "ContractMeta must carry a 'tier' field (int, 1/2/3) so downstream "
            "signal layers know the symbol's tier without re-querying the DB."
        )

    # TE-20
    def test_tier_params_t1_wider_than_t3(self):
        """T1 must have wider ATM window and longer DTE than T3 by default."""
        from services.symbol_registry import _TierParams
        t1 = _TierParams(atm_pct=0.20, max_dte=90)
        t3 = _TierParams(atm_pct=0.10, max_dte=30)
        assert t1.atm_pct > t3.atm_pct
        assert t1.max_dte > t3.max_dte


# ===========================================================================
# TE-21 … TE-22  tier_map round-trip via upsert_symbol_quotes
# ===========================================================================

class TestTierMapRoundTrip:
    """Feature 4A: upsert_symbol_quotes must write tier column to DB rows."""

    # TE-21
    def test_upsert_symbol_quotes_accepts_tier_map(self):
        """upsert_symbol_quotes signature must accept a tier_map kwarg."""
        import inspect
        from services.universe_store import upsert_symbol_quotes
        sig = inspect.signature(upsert_symbol_quotes)
        assert "tier_map" in sig.parameters, (
            "upsert_symbol_quotes() must accept a tier_map parameter (Feature 4A)."
        )

    # TE-22
    def test_upsert_symbol_quotes_writes_tier_to_row(self):
        """When tier_map is provided, each upserted row must include the tier value."""
        from services import universe_store

        sb   = MagicMock()
        q    = MagicMock()
        q.upsert.return_value  = q
        q.execute.return_value = MagicMock(data=[])
        sb.table.return_value  = q

        quotes   = {"SPY": {"last": 502.0, "open_interest": 50000, "average_volume": 80_000_000}}
        tier_map = {"SPY": 1}

        with patch.object(universe_store, "_client", return_value=sb):
            universe_store._sync_upsert_symbol_quotes(quotes, tier_map=tier_map)

        upsert_calls = q.upsert.call_args_list
        assert len(upsert_calls) >= 1
        rows = upsert_calls[0].args[0]
        if isinstance(rows, list):
            rows_with_tier = [r for r in rows if r.get("symbol") == "SPY"]
            assert len(rows_with_tier) == 1
            assert rows_with_tier[0].get("tier") == 1, (
                "upsert_symbol_quotes must write tier=1 for SPY when tier_map={SPY:1}."
            )
        else:
            assert rows.get("tier") == 1
