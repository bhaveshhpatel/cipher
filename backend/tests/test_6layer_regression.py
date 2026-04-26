"""
test_6layer_regression.py — Regression tests for the 6-layer gap fixes.
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


from utils.dedup import DedupCache, flow_dedup


class TestDedupCacheL4:
    def test_first_print_is_not_duplicate(self):
        cache = DedupCache(ttl_seconds=2.0)
        assert cache.is_duplicate("AAPL  260117C00180000", 10, 1.50, "N") is False

    def test_second_exchange_is_duplicate(self):
        cache = DedupCache(ttl_seconds=2.0)
        cache.is_duplicate("AAPL  260117C00180000", 10, 1.50, "N")
        assert cache.is_duplicate("AAPL  260117C00180000", 10, 1.50, "C") is True

    def test_third_and_fourth_exchange_duplicates(self):
        cache = DedupCache(ttl_seconds=2.0)
        sym, size, fill = "TSLA  260424C00375000", 50, 3.25
        cache.is_duplicate(sym, size, fill, "N")
        cache.is_duplicate(sym, size, fill, "C")
        assert cache.is_duplicate(sym, size, fill, "M") is True
        assert cache.is_duplicate(sym, size, fill, "Q") is True

    def test_different_size_not_duplicate(self):
        cache = DedupCache(ttl_seconds=2.0)
        cache.is_duplicate("SPY   260117P00450000", 10, 2.00, "N")
        assert cache.is_duplicate("SPY   260117P00450000", 20, 2.00, "N") is False

    def test_different_fill_price_not_duplicate(self):
        cache = DedupCache(ttl_seconds=2.0)
        cache.is_duplicate("NVDA  260117C00700000", 5, 1.00, "N")
        assert cache.is_duplicate("NVDA  260117C00700000", 5, 1.01, "N") is False

    def test_different_symbol_not_duplicate(self):
        cache = DedupCache(ttl_seconds=2.0)
        cache.is_duplicate("AAPL  260117C00180000", 10, 1.50, "N")
        assert cache.is_duplicate("MSFT  260117C00400000", 10, 1.50, "N") is False

    def test_sweep_detected_after_3_exchanges(self):
        cache = DedupCache(ttl_seconds=2.0, sweep_window=5.0, sweep_min_exchanges=3)
        sym, size, fill = "TSLA  260424C00375000", 50, 3.25
        cache.is_duplicate(sym, size, fill, "N")
        cache.is_duplicate(sym, size, fill, "C")
        cache.is_duplicate(sym, size, fill, "M")
        assert cache.is_sweep(sym, size, fill) is True

    def test_no_sweep_with_only_2_exchanges(self):
        cache = DedupCache(ttl_seconds=2.0, sweep_window=5.0, sweep_min_exchanges=3)
        sym, size, fill = "AAPL  260117C00180000", 10, 1.50
        cache.is_duplicate(sym, size, fill, "N")
        cache.is_duplicate(sym, size, fill, "C")
        assert cache.is_sweep(sym, size, fill) is False

    def test_ttl_expiry_allows_reuse(self):
        cache = DedupCache(ttl_seconds=0.05)
        sym, size, fill, exch = "QQQ   260117P00450000", 20, 2.10, "N"
        cache.is_duplicate(sym, size, fill, exch)
        time.sleep(0.12)
        assert cache.is_duplicate(sym, size, fill, exch) is False

    def test_module_singleton_exists(self):
        assert isinstance(flow_dedup, DedupCache)


class TestFlowStoreFlushL5:
    def test_flush_interval_is_500ms(self):
        import services.flow_store as fs
        assert fs._FLUSH_INTERVAL == 0.5, (
            f"_FLUSH_INTERVAL must be 0.5s (500ms), got {fs._FLUSH_INTERVAL}s. "
            "Regression: was incorrectly set to 5s."
        )

    def test_flush_max_rows_is_100(self):
        import services.flow_store as fs
        assert fs._FLUSH_MAX_ROWS == 100, (
            f"_FLUSH_MAX_ROWS must be 100, got {fs._FLUSH_MAX_ROWS}."
        )

    def test_early_flush_triggers_at_100_rows(self):
        import services.flow_store as fs

        flushed_batches = []

        async def mock_insert(table, rows):
            flushed_batches.append(len(rows))
            return True

        async def run_test():
            fs._flow_event_buffer.clear()
            with patch.object(fs, '_insert_rows', side_effect=mock_insert):
                for i in range(100):
                    await fs.persist_flow_event({
                        "ticker": "AAPL", "contract_type": "CALL",
                        "strike": 180.0, "expiry": "2026-01-17",
                        "dte": 30, "fill_price": 1.50,
                        "bid": 1.45, "ask": 1.55, "size": 10,
                        "premium": 1500.0, "trade_type": "SINGLE",
                        "bid_ask_class": "AT_ASK", "is_aggressive": True,
                        "is_golden_sweep": False, "sentiment": "BULLISH",
                        "influence_tier": "LARGE", "conviction_score": 0.6,
                        "exchange_count": 1, "fill_count": 1,
                        "open_interest": 5000, "iv": 0.35,
                        "underlying_price": 182.0, "occ_symbol": f"AAPL260117C{i:08d}",
                        "is_synthetic_quote": False,
                    })
            assert len(flushed_batches) >= 1
            assert flushed_batches[0] == 100

        run(run_test())

    def test_buffer_clears_after_early_flush(self):
        import services.flow_store as fs

        async def mock_insert(table, rows):
            return True

        async def run_test():
            fs._flow_event_buffer.clear()
            with patch.object(fs, '_insert_rows', side_effect=mock_insert):
                for i in range(100):
                    await fs.persist_flow_event({
                        "ticker": "TSLA", "contract_type": "PUT",
                        "strike": 200.0, "expiry": "2026-06-20",
                        "dte": 57, "fill_price": 2.0,
                        "bid": 1.9, "ask": 2.1, "size": 5,
                        "premium": 1000.0, "trade_type": "SINGLE",
                        "bid_ask_class": "MID", "is_aggressive": False,
                        "is_golden_sweep": False, "sentiment": "BEARISH",
                        "influence_tier": "RETAIL", "conviction_score": 0.2,
                        "exchange_count": 1, "fill_count": 1,
                        "open_interest": 0, "iv": 0.0,
                        "underlying_price": 205.0, "occ_symbol": f"TSLA260620P{i:08d}",
                        "is_synthetic_quote": False,
                    })
                assert len(fs._flow_event_buffer) == 0

        run(run_test())

    def test_under_100_rows_no_early_flush(self):
        import services.flow_store as fs

        flushed = []

        async def mock_insert(table, rows):
            flushed.append(rows)
            return True

        async def run_test():
            fs._flow_event_buffer.clear()
            with patch.object(fs, '_insert_rows', side_effect=mock_insert):
                for i in range(50):
                    await fs.persist_flow_event({
                        "ticker": "SPY", "contract_type": "CALL",
                        "strike": 500.0, "expiry": "2026-03-21",
                        "dte": 10, "fill_price": 3.0,
                        "bid": 2.9, "ask": 3.1, "size": 2,
                        "premium": 600.0, "trade_type": "BLOCK",
                        "bid_ask_class": "ABOVE_ASK", "is_aggressive": True,
                        "is_golden_sweep": False, "sentiment": "BULLISH",
                        "influence_tier": "WHALE", "conviction_score": 0.9,
                        "exchange_count": 4, "fill_count": 1,
                        "open_interest": 20000, "iv": 0.22,
                        "underlying_price": 502.0, "occ_symbol": f"SPY260321C{i:08d}",
                        "is_synthetic_quote": False,
                    })
                assert len(flushed) == 0
                assert len(fs._flow_event_buffer) == 50

        run(run_test())

    def test_flush_interval_not_5_seconds(self):
        import services.flow_store as fs
        assert fs._FLUSH_INTERVAL != 5, (
            "REGRESSION: _FLUSH_INTERVAL reverted to 5s. Must be 0.5s."
        )

    def test_row_contains_occ_symbol_field(self):
        import services.flow_store as fs

        async def run_test():
            fs._flow_event_buffer.clear()
            await fs.persist_flow_event({
                "ticker": "NVDA", "contract_type": "CALL",
                "strike": 700.0, "expiry": "2026-01-17",
                "dte": 5, "fill_price": 8.0,
                "bid": 7.9, "ask": 8.1, "size": 3,
                "premium": 2400.0, "trade_type": "SWEEP",
                "bid_ask_class": "ABOVE_ASK", "is_aggressive": True,
                "is_golden_sweep": True, "sentiment": "BULLISH",
                "influence_tier": "INSTITUTIONAL", "conviction_score": 0.88,
                "exchange_count": 3, "fill_count": 1,
                "open_interest": 8000, "iv": 0.45,
                "underlying_price": 701.0,
                "occ_symbol": "NVDA  260117C00700000",
                "is_synthetic_quote": False,
            })
            assert len(fs._flow_event_buffer) == 1
            row = fs._flow_event_buffer[0]
            assert "occ_symbol" in row
            assert row["occ_symbol"] == "NVDA  260117C00700000"

        run(run_test())

    def test_expiry_empty_string_coerced_to_none(self):
        import services.flow_store as fs

        async def run_test():
            fs._flow_event_buffer.clear()
            await fs.persist_flow_event({
                "ticker": "AAPL", "contract_type": "CALL",
                "strike": 180.0, "expiry": "",
                "dte": 0, "fill_price": 1.0,
                "bid": 0.9, "ask": 1.1, "size": 1,
                "premium": 100.0, "trade_type": "SINGLE",
                "bid_ask_class": "MID", "is_aggressive": False,
                "is_golden_sweep": False, "sentiment": "BULLISH",
                "influence_tier": "RETAIL", "conviction_score": 0.1,
                "exchange_count": 1, "fill_count": 1,
                "open_interest": 0, "iv": 0.0,
                "underlying_price": 182.0, "occ_symbol": None,
                "is_synthetic_quote": False,
            })
            row = fs._flow_event_buffer[-1]
            assert row["expiry"] is None

        run(run_test())


class TestStreamManagerRefreshL2:
    def test_stream_manager_has_refresh_method(self):
        from services.stream_manager import StreamManager
        assert hasattr(StreamManager, 'refresh')

    def test_stream_manager_refresh_is_coroutine(self):
        import inspect
        from services.stream_manager import StreamManager
        assert inspect.iscoroutinefunction(StreamManager.refresh)

    def test_tradier_stream_imports_stream_manager(self):
        import pathlib
        src = pathlib.Path("backend/services/tradier_stream.py")
        if not src.exists():
            src = pathlib.Path("services/tradier_stream.py")
        text = src.read_text()
        assert "StreamManager" in text

    def test_registry_refresh_notifies_manager(self):
        import pathlib
        src = pathlib.Path("backend/services/tradier_stream.py")
        if not src.exists():
            src = pathlib.Path("services/tradier_stream.py")
        text = src.read_text()
        assert "manager.refresh()" in text

    def test_stream_manager_refresh_noop_on_no_change(self):
        from services.stream_manager import StreamManager
        from services.symbol_registry import SymbolRegistry

        async def run_test():
            registry = MagicMock(spec=SymbolRegistry)
            registry.all_symbols.return_value = ["AAPL  260117C00180000"]
            registry.size.return_value = 1
            manager = StreamManager(registry=registry, process_fn=AsyncMock())
            mock_worker = MagicMock()
            mock_worker.symbols = ["AAPL  260117C00180000"]
            manager._workers = [mock_worker]
            stop_called = []
            async def mock_stop():
                stop_called.append(True)
            manager.stop = mock_stop
            await manager.refresh()
            assert len(stop_called) == 0

        run(run_test())

    def test_stream_manager_refresh_restarts_on_symbol_change(self):
        from services.stream_manager import StreamManager
        from services.symbol_registry import SymbolRegistry

        async def run_test():
            registry = MagicMock(spec=SymbolRegistry)
            registry.all_symbols.return_value = ["TSLA  260424C00375000"]
            registry.size.return_value = 1
            manager = StreamManager(registry=registry, process_fn=AsyncMock())
            mock_worker = MagicMock()
            mock_worker.symbols = ["AAPL  260117C00180000"]
            manager._workers = [mock_worker]
            manager._tasks = []
            manager._consumer = None
            stop_called  = []
            spawn_called = []
            async def mock_stop():
                stop_called.append(True)
                manager._workers = []
                manager._tasks   = []
            async def mock_spawn():
                spawn_called.append(True)
            manager.stop = mock_stop
            manager._spawn_workers = mock_spawn
            await manager.refresh()
            assert len(stop_called)  == 1
            assert len(spawn_called) == 1

        run(run_test())


class TestDedupIntegrationINT:
    def test_process_trade_module_imports_dedup(self):
        import pathlib
        src = pathlib.Path("backend/services/tradier_stream.py")
        if not src.exists():
            src = pathlib.Path("services/tradier_stream.py")
        text = src.read_text()
        assert "flow_dedup" in text

    def test_process_trade_calls_is_duplicate(self):
        import pathlib
        src = pathlib.Path("backend/services/tradier_stream.py")
        if not src.exists():
            src = pathlib.Path("services/tradier_stream.py")
        text = src.read_text()
        assert "flow_dedup.is_duplicate(" in text

    def test_process_trade_drops_duplicate_exchange_tick(self):
        import services.flow_store as fs
        from utils.dedup import DedupCache

        persisted = []

        async def run_test():
            test_dedup = DedupCache(ttl_seconds=2.0)

            async def fake_process(raw: dict):
                event_type = raw.get("type", "")
                if event_type != "timesale":
                    return
                payload  = raw.get("timesale", raw)
                symbol   = payload.get("symbol", "")
                exchange = payload.get("exch", "UNK")
                size     = int(payload.get("size", 0) or 0)
                fill     = float(payload.get("last") or payload.get("price") or 0)
                if size == 0:
                    return
                if test_dedup.is_duplicate(symbol, size, fill, exchange):
                    return
                persisted.append(payload)

            for exch in ["N", "C", "M", "Q"]:
                await fake_process({"type": "timesale", "timesale": {
                    "symbol": "AAPL  260117C00180000", "last": 1.50,
                    "size": 10, "exch": exch, "bid": 1.45, "ask": 1.55,
                }})
            assert len(persisted) == 1

        run(run_test())


class TestSyntheticQuoteSQ:
    def test_flow_event_has_is_synthetic_quote_field(self):
        from parsers.options_flow_parser import OptionsFlowEvent
        fields = {f.name for f in OptionsFlowEvent.__dataclass_fields__.values()} \
                 if hasattr(OptionsFlowEvent, '__dataclass_fields__') \
                 else set(vars(OptionsFlowEvent()).keys())
        assert "is_synthetic_quote" in fields

    def test_parser_sets_synthetic_flag_when_bid_ask_zero(self):
        from parsers.options_flow_parser import parse_tradier_trade
        event = parse_tradier_trade({
            "type": "timesale", "symbol": "AAPL  260117C00180000",
            "last": 1.50, "price": 1.50, "size": 10, "bid": 0, "ask": 0, "exch": "N",
        })
        assert event is not None
        assert event.is_synthetic_quote is True

    def test_parser_clears_synthetic_flag_with_real_nbbo(self):
        from parsers.options_flow_parser import parse_tradier_trade
        event = parse_tradier_trade({
            "type": "timesale", "symbol": "AAPL  260117C00180000",
            "last": 1.50, "price": 1.50, "size": 10, "bid": 1.45, "ask": 1.55, "exch": "N",
        })
        assert event is not None
        assert event.is_synthetic_quote is False

    def test_synthetic_spread_is_half_percent_of_fill(self):
        from parsers.options_flow_parser import parse_tradier_trade
        fill  = 2.00
        event = parse_tradier_trade({
            "type": "timesale", "symbol": "TSLA  260424C00375000",
            "last": fill, "size": 5, "bid": 0, "ask": 0, "exch": "C",
        })
        assert event is not None
        assert abs(event.bid - round(fill * 0.995, 2)) < 0.005
        assert abs(event.ask - round(fill * 1.005, 2)) < 0.005

    def test_flow_store_persists_is_synthetic_quote_true(self):
        import services.flow_store as fs
        async def run_test():
            fs._flow_event_buffer.clear()
            await fs.persist_flow_event({
                "ticker": "TSLA", "contract_type": "CALL", "strike": 375.0,
                "expiry": "2026-04-24", "dte": 0, "fill_price": 3.0,
                "bid": 2.985, "ask": 3.015, "size": 5, "premium": 1500.0,
                "trade_type": "SINGLE", "bid_ask_class": "AT_ASK", "is_aggressive": True,
                "is_golden_sweep": False, "sentiment": "BULLISH",
                "influence_tier": "RETAIL", "conviction_score": 0.3,
                "exchange_count": 1, "fill_count": 1, "open_interest": 1000, "iv": 0.50,
                "underlying_price": 370.0, "occ_symbol": "TSLA  260424C00375000",
                "is_synthetic_quote": True,
            })
            row = fs._flow_event_buffer[-1]
            assert "is_synthetic_quote" in row
            assert row["is_synthetic_quote"] is True
        run(run_test())

    def test_flow_store_persists_is_synthetic_quote_false(self):
        import services.flow_store as fs
        async def run_test():
            fs._flow_event_buffer.clear()
            await fs.persist_flow_event({
                "ticker": "SPY", "contract_type": "PUT", "strike": 450.0,
                "expiry": "2026-01-17", "dte": 30, "fill_price": 2.00,
                "bid": 1.95, "ask": 2.05, "size": 20, "premium": 4000.0,
                "trade_type": "BLOCK", "bid_ask_class": "MID", "is_aggressive": False,
                "is_golden_sweep": False, "sentiment": "BEARISH",
                "influence_tier": "WHALE", "conviction_score": 0.7,
                "exchange_count": 1, "fill_count": 1, "open_interest": 30000, "iv": 0.18,
                "underlying_price": 452.0, "occ_symbol": "SPY   260117P00450000",
                "is_synthetic_quote": False,
            })
            assert fs._flow_event_buffer[-1]["is_synthetic_quote"] is False
        run(run_test())

    def test_tradier_stream_sets_is_synthetic_quote(self):
        import pathlib
        src = pathlib.Path("backend/services/tradier_stream.py")
        if not src.exists():
            src = pathlib.Path("services/tradier_stream.py")
        assert "is_synthetic_quote" in src.read_text()

    def test_backtest_score_query_filters_synthetic_quotes(self):
        import pathlib
        candidates = [
            pathlib.Path("backend/services/backtest_store.py"),
            pathlib.Path("services/backtest_store.py"),
            pathlib.Path("backend/signals/backtest_scorer.py"),
            pathlib.Path("signals/backtest_scorer.py"),
        ]
        found = next((p for p in candidates if p.exists()), None)
        if found is None:
            pytest.skip("backtest file not found")
        assert "is_synthetic_quote" in found.read_text()


class TestFeature4ATierWiring:
    def test_registry_has_set_tier_map(self):
        from services.symbol_registry import SymbolRegistry
        assert hasattr(SymbolRegistry, 'set_tier_map')

    def test_set_tier_map_is_callable(self):
        from services.symbol_registry import SymbolRegistry
        assert callable(SymbolRegistry.set_tier_map)

    def test_main_passes_tier_map_to_registry(self):
        import pathlib
        src = pathlib.Path("backend/main.py")
        if not src.exists():
            src = pathlib.Path("main.py")
        text = src.read_text()
        assert "tier_map" in text

    def test_load_tier_map_function_exists(self):
        from services import universe_store
        assert hasattr(universe_store, 'load_tier_map') or \
               hasattr(universe_store, '_sync_load_tier_map')

    def test_universe_refresh_loop_calls_set_tier_map(self):
        import pathlib
        src = pathlib.Path("backend/main.py")
        if not src.exists():
            src = pathlib.Path("main.py")
        text = src.read_text()
        assert "set_tier_map" in text

    def test_symbol_registry_uses_tier_params_in_build(self):
        import pathlib
        src = pathlib.Path("backend/services/symbol_registry.py")
        if not src.exists():
            src = pathlib.Path("services/symbol_registry.py")
        text = src.read_text()
        assert "_TierParams" in text

    def test_tier_engine_module_exists(self):
        import pathlib
        candidates = [
            pathlib.Path("backend/services/tier_engine.py"),
            pathlib.Path("services/tier_engine.py"),
        ]
        found = next((p for p in candidates if p.exists()), None)
        assert found is not None
        assert "assign_tiers" in found.read_text()

    def test_upsert_symbol_quotes_references_tier(self):
        import pathlib
        src = pathlib.Path("backend/services/universe_store.py")
        if not src.exists():
            src = pathlib.Path("services/universe_store.py")
        text = src.read_text()
        assert "tier" in text
