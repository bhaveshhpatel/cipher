"""
Tests for H1, H3, and H4 fixes.

H1 — build() returns tuple[int, dict]; _post_build_upsert reuses raw_quotes
H3 — incremental build guard uses self._registry (not _seeded_from_db)
H4 — _sweep_upgrade_dispatched dict evicts entries older than TTL
"""
import asyncio
import time as _time
from unittest.mock import AsyncMock, MagicMock, patch


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# H1 Tests — build() returns tuple; _post_build_upsert skips duplicate fetch
# ===========================================================================

class TestH1BuildReturnsTuple:
    """build() must return (int, dict[str, dict]) not just int."""

    def _make_registry(self):
        from services.symbol_registry import SymbolRegistry
        return SymbolRegistry(watchlist=["AAPL"])

    def test_build_returns_tuple_of_int_and_dict(self):
        r = self._make_registry()
        mock_cfg    = {"REGISTRY_MIN_OI": 0, "REGISTRY_BUILD_CONCURRENCY": 2}
        mock_thresh = {}
        mock_prices = {"AAPL": 150.0}
        mock_raw    = {"AAPL": {"last": "150.0", "volume": "1000", "average_volume": "500"}}

        with patch("services.symbol_registry.get_config",
                   new=AsyncMock(return_value=mock_cfg)), \
             patch("services.symbol_registry._fetch_thresholds",
                   new=AsyncMock(return_value=mock_thresh)), \
             patch("services.symbol_registry.assign_tiers",
                   new=AsyncMock(return_value={"AAPL": 1})), \
             patch.object(r, "_fetch_stock_prices",
                          new=AsyncMock(return_value=(mock_prices, mock_raw))), \
             patch.object(r, "_build_ticker", new=AsyncMock()), \
             patch.object(r, "_persist_to_db", new=AsyncMock()):
            result = _run(r.build())

        assert isinstance(result, tuple), "build() must return a tuple"
        assert len(result) == 2, "tuple must have 2 elements"
        count, raw_quotes = result
        assert isinstance(count, int)
        assert isinstance(raw_quotes, dict)

    def test_build_raw_quotes_contains_fetched_data(self):
        r = self._make_registry()
        mock_cfg    = {"REGISTRY_MIN_OI": 0, "REGISTRY_BUILD_CONCURRENCY": 2}
        mock_thresh = {}
        mock_prices = {"AAPL": 150.0}
        mock_raw    = {"AAPL": {"last": "150.0", "volume": "2000"}}

        with patch("services.symbol_registry.get_config",
                   new=AsyncMock(return_value=mock_cfg)), \
             patch("services.symbol_registry._fetch_thresholds",
                   new=AsyncMock(return_value=mock_thresh)), \
             patch("services.symbol_registry.assign_tiers",
                   new=AsyncMock(return_value={})), \
             patch.object(r, "_fetch_stock_prices",
                          new=AsyncMock(return_value=(mock_prices, mock_raw))), \
             patch.object(r, "_build_ticker", new=AsyncMock()), \
             patch.object(r, "_persist_to_db", new=AsyncMock()):
            _, raw_quotes = _run(r.build())

        assert "AAPL" in raw_quotes
        assert raw_quotes["AAPL"]["volume"] == "2000"


class TestH1PostBuildUpsertRawQuotes:
    """_post_build_upsert must skip _fetch_batch_quotes when raw_quotes is provided."""

    def _make_mock_registry(self, oi_map=None):
        r = MagicMock()
        r.get_oi_map.return_value = oi_map or {"AAPL": 500}
        r.set_tier_map = MagicMock()
        return r

    def test_no_fetch_batch_quotes_when_raw_quotes_provided(self):
        import main as main_module
        mock_registry = self._make_mock_registry()
        raw_quotes    = {"AAPL": {"last": "150.0", "volume": "1000", "average_volume": "500"}}
        mock_upsert   = AsyncMock()

        with patch.object(main_module, "_fetch_batch_quotes",
                          new_callable=AsyncMock) as mock_fetch, \
             patch.object(main_module, "assign_tiers",
                          new_callable=AsyncMock, return_value={"AAPL": 1}), \
             patch.object(main_module.universe_store, "upsert_symbol_quotes",
                          new=mock_upsert):
            _run(main_module._post_build_upsert(
                mock_registry, ["AAPL"], raw_quotes=raw_quotes
            ))

        mock_fetch.assert_not_called()
        mock_upsert.assert_called_once()

    def test_fetches_quotes_when_raw_quotes_is_none(self):
        import main as main_module
        mock_registry = self._make_mock_registry()
        fake_quote = MagicMock()
        fake_quote.symbol = "AAPL"
        fake_quote.open_interest = 0
        mock_upsert = AsyncMock()

        with patch.object(main_module, "_fetch_batch_quotes",
                          new_callable=AsyncMock,
                          return_value=[fake_quote]) as mock_fetch, \
             patch.object(main_module, "assign_tiers",
                          new_callable=AsyncMock, return_value={"AAPL": 1}), \
             patch.object(main_module.universe_store, "upsert_symbol_quotes",
                          new=mock_upsert):
            _run(main_module._post_build_upsert(
                mock_registry, ["AAPL"], raw_quotes=None
            ))

        mock_fetch.assert_called_once()

    def test_empty_quotes_from_raw_skips_upsert(self):
        """When raw_quotes has no valid price entries, upsert must be skipped."""
        import main as main_module
        mock_registry = self._make_mock_registry()
        raw_quotes    = {"AAPL": {"last": None}}
        mock_upsert   = AsyncMock()

        with patch.object(main_module, "_fetch_batch_quotes",
                          new_callable=AsyncMock), \
             patch.object(main_module, "assign_tiers",
                          new_callable=AsyncMock, return_value={}), \
             patch.object(main_module.universe_store, "upsert_symbol_quotes",
                          new=mock_upsert):
            _run(main_module._post_build_upsert(
                mock_registry, ["AAPL"], raw_quotes=raw_quotes
            ))

        mock_upsert.assert_not_called()

    def test_background_build_passes_raw_quotes_to_post_build(self):
        """_background_build_and_upsert must pass raw_quotes from build() to _post_build_upsert."""
        import main as main_module
        mock_registry = self._make_mock_registry()
        raw_quotes_sentinel = {"AAPL": {"last": "150.0"}}
        mock_registry.build = AsyncMock(return_value=(100, raw_quotes_sentinel))
        mock_registry.size  = MagicMock(return_value=100)

        captured = {}

        async def fake_post_build(reg, syms, raw_quotes=None):
            captured["raw_quotes"] = raw_quotes

        with patch.object(main_module, "_post_build_upsert",
                          side_effect=fake_post_build):
            _run(main_module._background_build_and_upsert(mock_registry, ["AAPL"]))

        assert captured["raw_quotes"] is raw_quotes_sentinel


# ===========================================================================
# H3 Tests — incremental guard uses self._registry, not _seeded_from_db
# ===========================================================================

class TestH3IncrementalGuard:
    """After first build(), a second build() must use incremental mode."""

    def _make_registry_with_data(self):
        from services.symbol_registry import SymbolRegistry, ContractMeta
        r = SymbolRegistry(watchlist=["AAPL", "TSLA"])
        r._registry = {
            "AAPL261219C00150000": ContractMeta(
                ticker="AAPL", strike=150.0, expiry="2026-12-19",
                contract_type="CALL", dte=30, open_interest=100
            ),
            "TSLA261219C00200000": ContractMeta(
                ticker="TSLA", strike=200.0, expiry="2026-12-19",
                contract_type="CALL", dte=30, open_interest=200
            ),
        }
        return r

    def test_populated_registry_triggers_incremental(self):
        """With self._registry populated and all DTEs > 0, tickers_to_refresh must be empty."""
        r = self._make_registry_with_data()
        mock_cfg    = {"REGISTRY_MIN_OI": 0, "REGISTRY_BUILD_CONCURRENCY": 2}
        mock_prices = {"AAPL": 150.0, "TSLA": 200.0}
        mock_raw    = {
            "AAPL": {"last": "150.0", "volume": "1000", "average_volume": "500"},
            "TSLA": {"last": "200.0", "volume": "2000", "average_volume": "1000"},
        }
        build_ticker_calls = []

        async def fake_build_ticker(ticker, *a, **kw):
            build_ticker_calls.append(ticker)

        with patch("services.symbol_registry.get_config",
                   new=AsyncMock(return_value=mock_cfg)), \
             patch("services.symbol_registry._fetch_thresholds",
                   new=AsyncMock(return_value={})), \
             patch("services.symbol_registry.assign_tiers",
                   new=AsyncMock(return_value={})), \
             patch.object(r, "_fetch_stock_prices",
                          new=AsyncMock(return_value=(mock_prices, mock_raw))), \
             patch.object(r, "_build_ticker", side_effect=fake_build_ticker), \
             patch.object(r, "_persist_to_db", new=AsyncMock()):
            _run(r.build())

        assert build_ticker_calls == [], (
            f"Expected no tickers refreshed in incremental mode, got: {build_ticker_calls}"
        )

    def test_empty_registry_triggers_full_build(self):
        """Empty registry must trigger full build for all watchlist tickers."""
        from services.symbol_registry import SymbolRegistry
        r = SymbolRegistry(watchlist=["AAPL", "TSLA"])
        mock_cfg    = {"REGISTRY_MIN_OI": 0, "REGISTRY_BUILD_CONCURRENCY": 2}
        mock_prices = {"AAPL": 150.0, "TSLA": 200.0}
        mock_raw    = {"AAPL": {"last": "150.0"}, "TSLA": {"last": "200.0"}}
        build_ticker_calls = []

        async def fake_build_ticker(ticker, *a, **kw):
            build_ticker_calls.append(ticker)

        with patch("services.symbol_registry.get_config",
                   new=AsyncMock(return_value=mock_cfg)), \
             patch("services.symbol_registry._fetch_thresholds",
                   new=AsyncMock(return_value={})), \
             patch("services.symbol_registry.assign_tiers",
                   new=AsyncMock(return_value={})), \
             patch.object(r, "_fetch_stock_prices",
                          new=AsyncMock(return_value=(mock_prices, mock_raw))), \
             patch.object(r, "_build_ticker", side_effect=fake_build_ticker), \
             patch.object(r, "_persist_to_db", new=AsyncMock()):
            _run(r.build())

        assert set(build_ticker_calls) == {"AAPL", "TSLA"}

    def test_expired_ticker_refreshed_in_incremental_mode(self):
        """A ticker with dte==0 must be refreshed even in incremental mode."""
        from services.symbol_registry import SymbolRegistry, ContractMeta
        r = SymbolRegistry(watchlist=["AAPL", "TSLA"])
        r._registry = {
            "AAPL260428C00150000": ContractMeta(
                ticker="AAPL", strike=150.0, expiry="2026-04-28",
                contract_type="CALL", dte=0, open_interest=100
            ),
            "TSLA261219C00200000": ContractMeta(
                ticker="TSLA", strike=200.0, expiry="2026-12-19",
                contract_type="CALL", dte=30, open_interest=200
            ),
        }
        mock_cfg    = {"REGISTRY_MIN_OI": 0, "REGISTRY_BUILD_CONCURRENCY": 2}
        mock_prices = {"AAPL": 150.0, "TSLA": 200.0}
        mock_raw    = {"AAPL": {"last": "150.0"}, "TSLA": {"last": "200.0"}}
        build_ticker_calls = []

        async def fake_build_ticker(ticker, *a, **kw):
            build_ticker_calls.append(ticker)

        with patch("services.symbol_registry.get_config",
                   new=AsyncMock(return_value=mock_cfg)), \
             patch("services.symbol_registry._fetch_thresholds",
                   new=AsyncMock(return_value={})), \
             patch("services.symbol_registry.assign_tiers",
                   new=AsyncMock(return_value={})), \
             patch.object(r, "_fetch_stock_prices",
                          new=AsyncMock(return_value=(mock_prices, mock_raw))), \
             patch.object(r, "_build_ticker", side_effect=fake_build_ticker), \
             patch.object(r, "_persist_to_db", new=AsyncMock()):
            _run(r.build())

        assert "AAPL" in build_ticker_calls, "AAPL (dte=0) should have been refreshed"
        assert "TSLA" not in build_ticker_calls, "TSLA (dte=30) should have been carried forward"

    def test_no_seeded_from_db_attribute(self):
        """SymbolRegistry must not have a _seeded_from_db attribute (H3 cleanup)."""
        from services.symbol_registry import SymbolRegistry
        r = SymbolRegistry(watchlist=[])
        assert not hasattr(r, "_seeded_from_db"), (
            "_seeded_from_db attribute must be removed in H3 fix"
        )

    def test_load_from_db_does_not_set_seeded_flag(self):
        """load_from_db must not set _seeded_from_db on the registry."""
        from services.symbol_registry import SymbolRegistry, ContractMeta
        r = SymbolRegistry(watchlist=[])
        fake_chain = {
            "AAPL261219C00150000": ContractMeta(
                ticker="AAPL", strike=150.0, expiry="2026-12-19",
                contract_type="CALL", dte=30, open_interest=100
            )
        }
        with patch("services.symbol_registry.load_chain",
                   new=AsyncMock(return_value=fake_chain)):
            _run(r.load_from_db("snap-001"))
        assert not hasattr(r, "_seeded_from_db")


# ===========================================================================
# H4 Tests — _sweep_upgrade_dispatched TTL eviction
# ===========================================================================

class TestH4SweepDispatchTTL:

    def _reset_module_state(self):
        import services.tradier_stream as ts
        ts._sweep_upgrade_dispatched.clear()

    def test_ttl_constant_is_1800(self):
        import services.tradier_stream as ts
        assert ts._SWEEP_DISPATCH_TTL_S == 1800.0

    def test_dispatch_dict_is_dict_not_set(self):
        import services.tradier_stream as ts
        assert isinstance(ts._sweep_upgrade_dispatched, dict)

    def test_stale_key_evicted_before_check(self):
        """A key older than TTL must be evicted, allowing a new dispatch."""
        import services.tradier_stream as ts
        self._reset_module_state()

        stale_ts = _time.time() - ts._SWEEP_DISPATCH_TTL_S - 1
        ts._sweep_upgrade_dispatched["AAPL|100|5.00"] = stale_ts

        mock_ev = MagicMock()
        mock_ev.size = 100
        mock_ev.fill_price = 5.0
        mock_ev.trade_type = "BTO"
        mock_ev.exchange_count = 0

        task_created = []

        async def run_test():
            with patch("services.tradier_stream.parse_tradier_trade",
                       return_value=mock_ev), \
                 patch("services.tradier_stream.flow_dedup") as mock_dedup, \
                 patch("services.tradier_stream.asyncio.create_task",
                       side_effect=lambda c: task_created.append(c) or MagicMock()):
                mock_dedup.is_duplicate.return_value = True
                mock_dedup._sweep_min = 3
                mock_dedup.get_exchange_count.return_value = 3

                raw = {"type": "timesale", "timesale": {"symbol": "AAPL|100|5.00"}}
                await ts._process_trade(raw)

        _run(run_test())
        assert len(task_created) >= 1, "New dispatch should fire after stale key was evicted"

    def test_fresh_key_not_evicted(self):
        """A key within TTL must NOT be evicted — no second dispatch."""
        import services.tradier_stream as ts
        self._reset_module_state()

        fresh_ts = _time.time() - 60
        ts._sweep_upgrade_dispatched["AAPL|100|5.00"] = fresh_ts

        mock_ev = MagicMock()
        mock_ev.size = 100
        mock_ev.fill_price = 5.0
        mock_ev.trade_type = "BTO"

        task_created = []

        async def run_test():
            with patch("services.tradier_stream.parse_tradier_trade",
                       return_value=mock_ev), \
                 patch("services.tradier_stream.flow_dedup") as mock_dedup, \
                 patch("services.tradier_stream.asyncio.create_task",
                       side_effect=lambda c: task_created.append(c) or MagicMock()):
                mock_dedup.is_duplicate.return_value = True
                mock_dedup._sweep_min = 3
                mock_dedup.get_exchange_count.return_value = 3

                raw = {"type": "timesale", "timesale": {"symbol": "AAPL|100|5.00"}}
                await ts._process_trade(raw)

        _run(run_test())
        assert len(task_created) == 0, "No dispatch should fire when key is within TTL"

    def test_new_key_stored_with_timestamp(self):
        """First dispatch must store the key with a float timestamp."""
        import services.tradier_stream as ts
        self._reset_module_state()

        mock_ev = MagicMock()
        mock_ev.size = 200
        mock_ev.fill_price = 7.50
        mock_ev.trade_type = "BTO"

        before = _time.time()

        async def run_test():
            with patch("services.tradier_stream.parse_tradier_trade",
                       return_value=mock_ev), \
                 patch("services.tradier_stream.flow_dedup") as mock_dedup, \
                 patch("services.tradier_stream.asyncio.create_task",
                       return_value=MagicMock()):
                mock_dedup.is_duplicate.return_value = True
                mock_dedup._sweep_min = 3
                mock_dedup.get_exchange_count.return_value = 3

                raw = {"type": "timesale", "timesale": {"symbol": "NVDA|200|7.50"}}
                await ts._process_trade(raw)

        _run(run_test())
        after = _time.time()

        key = "NVDA|200|7.50"
        assert key in ts._sweep_upgrade_dispatched
        stored_ts = ts._sweep_upgrade_dispatched[key]
        assert isinstance(stored_ts, float)
        assert before <= stored_ts <= after

    def test_no_set_import_in_typing(self):
        """tradier_stream must not import Set from typing (H4 cleanup)."""
        import ast
        import inspect
        import services.tradier_stream as ts
        source = inspect.getsource(ts)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "typing":
                names = [alias.name for alias in node.names]
                assert "Set" not in names, (
                    "Set must be removed from typing import in H4 fix"
                )
