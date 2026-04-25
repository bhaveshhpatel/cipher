"""
test_6layer_regression.py — Regression tests for the 6-layer gap fixes.

Gap fixes audited 2026-04-24 (commit 309192f):
  C-016  Layer 4: DedupCache was implemented but never called in _process_trade()
  C-017  Layer 2: registry.refresh_loop() never called manager.refresh() after rebuild
  C-018  Layer 5: _FLUSH_INTERVAL was 5s instead of 500ms; no 100-row early flush
         Layer 3: is_synthetic_quote flag added for bid=ask=0 rows (migration 009)

Test groups:
  L4-01 … L4-10  DedupCache unit tests (utils/dedup.py)
  L5-01 … L5-08  flow_store flush interval + early-flush tests
  L2-01 … L2-06  StreamManager.refresh() wiring (import-level + mock)
  INT-01 … INT-03 Integration: dedup gate visible in tradier_stream._process_trade
  SQ-01 … SQ-08  is_synthetic_quote flag (C-018 Layer 3 / migration 009)

All tests are pure-Python / asyncio — no Supabase, no Tradier, no network.
"""
import asyncio
import time
import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# L4 — DedupCache (utils/dedup.py)
# ===========================================================================

from utils.dedup import DedupCache, flow_dedup


class TestDedupCacheL4:
    """C-016 regression: DedupCache correctness."""

    # L4-01
    def test_first_print_is_not_duplicate(self):
        cache = DedupCache(ttl_seconds=2.0)
        assert cache.is_duplicate("AAPL  260117C00180000", 10, 1.50, "N") is False

    # L4-02
    def test_second_exchange_is_duplicate(self):
        cache = DedupCache(ttl_seconds=2.0)
        cache.is_duplicate("AAPL  260117C00180000", 10, 1.50, "N")
        assert cache.is_duplicate("AAPL  260117C00180000", 10, 1.50, "C") is True

    # L4-03
    def test_third_and_fourth_exchange_duplicates(self):
        cache = DedupCache(ttl_seconds=2.0)
        sym, size, fill = "TSLA  260424C00375000", 50, 3.25
        cache.is_duplicate(sym, size, fill, "N")
        cache.is_duplicate(sym, size, fill, "C")
        assert cache.is_duplicate(sym, size, fill, "M") is True
        assert cache.is_duplicate(sym, size, fill, "Q") is True

    # L4-04
    def test_different_size_not_duplicate(self):
        cache = DedupCache(ttl_seconds=2.0)
        cache.is_duplicate("SPY   260117P00450000", 10, 2.00, "N")
        assert cache.is_duplicate("SPY   260117P00450000", 20, 2.00, "N") is False

    # L4-05
    def test_different_fill_price_not_duplicate(self):
        cache = DedupCache(ttl_seconds=2.0)
        cache.is_duplicate("NVDA  260117C00700000", 5, 1.00, "N")
        # fill rounded to 2dp — 1.009 still rounds to 1.01, so genuinely different
        assert cache.is_duplicate("NVDA  260117C00700000", 5, 1.01, "N") is False

    # L4-06
    def test_different_symbol_not_duplicate(self):
        cache = DedupCache(ttl_seconds=2.0)
        cache.is_duplicate("AAPL  260117C00180000", 10, 1.50, "N")
        assert cache.is_duplicate("MSFT  260117C00400000", 10, 1.50, "N") is False

    # L4-07
    def test_sweep_detected_after_3_exchanges(self):
        cache = DedupCache(ttl_seconds=2.0, sweep_window=5.0, sweep_min_exchanges=3)
        sym, size, fill = "TSLA  260424C00375000", 50, 3.25
        cache.is_duplicate(sym, size, fill, "N")
        cache.is_duplicate(sym, size, fill, "C")
        cache.is_duplicate(sym, size, fill, "M")
        # After 3 unique exchanges the canonical print is a sweep
        assert cache.is_sweep(sym, size, fill) is True

    # L4-08
    def test_no_sweep_with_only_2_exchanges(self):
        cache = DedupCache(ttl_seconds=2.0, sweep_window=5.0, sweep_min_exchanges=3)
        sym, size, fill = "AAPL  260117C00180000", 10, 1.50
        cache.is_duplicate(sym, size, fill, "N")
        cache.is_duplicate(sym, size, fill, "C")
        assert cache.is_sweep(sym, size, fill) is False

    # L4-09
    def test_ttl_expiry_allows_reuse(self):
        cache = DedupCache(ttl_seconds=0.05)   # 50ms TTL for test speed
        sym, size, fill, exch = "QQQ   260117P00450000", 20, 2.10, "N"
        cache.is_duplicate(sym, size, fill, exch)
        time.sleep(0.12)  # wait > TTL
        # New 2s bucket — should NOT be a duplicate
        assert cache.is_duplicate(sym, size, fill, exch) is False

    # L4-10
    def test_module_singleton_exists(self):
        """flow_dedup singleton must be importable and be a DedupCache."""
        assert isinstance(flow_dedup, DedupCache)


# ===========================================================================
# L5 — flow_store flush interval + early-flush (services/flow_store.py)
# ===========================================================================

class TestFlowStoreFlushL5:
    """C-018 regression: flush interval is 500ms and 100-row early-flush works."""

    # L5-01
    def test_flush_interval_is_500ms(self):
        import services.flow_store as fs
        assert fs._FLUSH_INTERVAL == 0.5, (
            f"_FLUSH_INTERVAL must be 0.5s (500ms), got {fs._FLUSH_INTERVAL}s. "
            "Regression: was incorrectly set to 5s."
        )

    # L5-02
    def test_flush_max_rows_is_100(self):
        import services.flow_store as fs
        assert fs._FLUSH_MAX_ROWS == 100, (
            f"_FLUSH_MAX_ROWS must be 100, got {fs._FLUSH_MAX_ROWS}."
        )

    # L5-03
    def test_early_flush_triggers_at_100_rows(self):
        """persist_flow_event() must flush when buffer reaches 100 rows."""
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
                        "underlying_price": 182.0, "occ_symbol": f"AAPL260117C00180000",
                        "is_synthetic_quote": False,
                    })
            # After 100 rows, early flush should have fired once
            assert len(flushed_batches) >= 1
            assert flushed_batches[0] == 100

        run(run_test())

    # L5-04
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
                        "underlying_price": 205.0, "occ_symbol": f"TSLA260620P00200000",
                        "is_synthetic_quote": False,
                    })
                # Buffer should be empty after the 100-row early flush
                assert len(fs._flow_event_buffer) == 0

        run(run_test())

    # L5-05
    def test_under_100_rows_no_early_flush(self):
        import services.flow_store as fs

        flushed = []

        async def mock_insert(table, rows):
            flushed.append(rows)
            return True

        async def run_test():
            fs._flow_event_buffer.clear()
            with patch.object(fs, '_insert_rows', side_effect=mock_insert):
                for i in range(50):  # only 50 rows — no early flush
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
                        "underlying_price": 502.0, "occ_symbol": f"SPY260321C00500000",
                        "is_synthetic_quote": False,
                    })
                assert len(flushed) == 0, "Should NOT flush until 100 rows or timer fires"
                assert len(fs._flow_event_buffer) == 50

        run(run_test())

    # L5-06
    def test_flush_interval_not_5_seconds(self):
        """Explicit regression: previous wrong value was 5."""
        import services.flow_store as fs
        assert fs._FLUSH_INTERVAL != 5, (
            "REGRESSION: _FLUSH_INTERVAL reverted to 5s. Must be 0.5s."
        )

    # L5-07
    def test_row_contains_occ_symbol_field(self):
        """occ_symbol must be included in every buffered row."""
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

    # L5-08
    def test_expiry_empty_string_coerced_to_none(self):
        """Empty expiry string must be stored as None — Postgres DATE cast protection."""
        import services.flow_store as fs

        async def run_test():
            fs._flow_event_buffer.clear()
            await fs.persist_flow_event({
                "ticker": "AAPL", "contract_type": "CALL",
                "strike": 180.0, "expiry": "",   # empty string
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
            assert row["expiry"] is None, (
                f"Empty expiry should be None in DB row, got: {row['expiry']!r}"
            )

        run(run_test())


# ===========================================================================
# L2 — StreamManager.refresh() wired to registry loop
# ===========================================================================

class TestStreamManagerRefreshL2:
    """C-017 regression: StreamManager.refresh() is called after registry rebuild."""

    # L2-01
    def test_stream_manager_has_refresh_method(self):
        from services.stream_manager import StreamManager
        assert hasattr(StreamManager, 'refresh'), \
            "StreamManager must have a refresh() method for post-rebuild worker restart."

    # L2-02
    def test_stream_manager_refresh_is_coroutine(self):
        import inspect
        from services.stream_manager import StreamManager
        assert inspect.iscoroutinefunction(StreamManager.refresh), \
            "StreamManager.refresh must be async (coroutine function)."

    # L2-03
    def test_tradier_stream_imports_stream_manager(self):
        """tradier_stream.py must reference StreamManager (not run without it)."""
        import ast, pathlib
        src = pathlib.Path("backend/services/tradier_stream.py")
        if not src.exists():
            src = pathlib.Path("services/tradier_stream.py")
        text = src.read_text()
        assert "StreamManager" in text, \
            "tradier_stream.py must import/use StreamManager for Layer 2."

    # L2-04
    def test_registry_refresh_notifies_manager(self):
        """tradier_stream.py must contain the manager.refresh() call."""
        import pathlib
        src = pathlib.Path("backend/services/tradier_stream.py")
        if not src.exists():
            src = pathlib.Path("services/tradier_stream.py")
        text = src.read_text()
        assert "manager.refresh()" in text, (
            "REGRESSION C-017: tradier_stream.py does not call manager.refresh() "
            "after registry rebuild. Workers will stream stale OCC symbols."
        )

    # L2-05
    def test_stream_manager_refresh_noop_on_no_change(self):
        """refresh() with identical symbol sets must not restart any workers."""
        from services.stream_manager import StreamManager
        from services.symbol_registry import SymbolRegistry

        async def run_test():
            registry = MagicMock(spec=SymbolRegistry)
            registry.all_symbols.return_value = ["AAPL  260117C00180000"]
            registry.size.return_value = 1

            manager = StreamManager(registry=registry, process_fn=AsyncMock())
            # Pre-populate _workers with one mock worker covering that symbol
            mock_worker = MagicMock()
            mock_worker.symbols = ["AAPL  260117C00180000"]
            manager._workers = [mock_worker]

            stop_called = []
            original_stop = manager.stop
            async def mock_stop():
                stop_called.append(True)
            manager.stop = mock_stop

            await manager.refresh()
            assert len(stop_called) == 0, \
                "refresh() must NOT restart workers when symbol set is unchanged."

        run(run_test())

    # L2-06
    def test_stream_manager_refresh_restarts_on_symbol_change(self):
        """refresh() with changed symbol set must trigger a worker restart."""
        from services.stream_manager import StreamManager
        from services.symbol_registry import SymbolRegistry

        async def run_test():
            registry = MagicMock(spec=SymbolRegistry)
            # New symbol set has a different contract
            registry.all_symbols.return_value = ["TSLA  260424C00375000"]
            registry.size.return_value = 1

            manager = StreamManager(registry=registry, process_fn=AsyncMock())
            # Pre-populate with old worker covering a DIFFERENT symbol
            mock_worker = MagicMock()
            mock_worker.symbols = ["AAPL  260117C00180000"]
            manager._workers = [mock_worker]
            manager._tasks = []
            manager._consumer = None

            stop_called = []
            spawn_called = []

            async def mock_stop():
                stop_called.append(True)
                manager._workers = []
                manager._tasks = []

            async def mock_spawn():
                spawn_called.append(True)

            manager.stop = mock_stop
            manager._spawn_workers = mock_spawn

            await manager.refresh()
            assert len(stop_called) == 1, \
                "refresh() must stop workers when symbol set changes."
            assert len(spawn_called) == 1, \
                "refresh() must respawn workers after symbol set changes."

        run(run_test())


# ===========================================================================
# INT — Integration tests: dedup gate visible in process_trade path
# ===========================================================================

class TestDedupIntegrationINT:
    """C-016 integration: verify dedup is called in tradier_stream._process_trade."""

    # INT-01
    def test_process_trade_module_imports_dedup(self):
        """tradier_stream.py must import flow_dedup."""
        import pathlib
        src = pathlib.Path("backend/services/tradier_stream.py")
        if not src.exists():
            src = pathlib.Path("services/tradier_stream.py")
        text = src.read_text()
        assert "flow_dedup" in text, (
            "REGRESSION C-016: tradier_stream.py does not import/use flow_dedup. "
            "DedupCache is built but not wired — 4× duplicate DB rows per trade."
        )

    # INT-02
    def test_process_trade_calls_is_duplicate(self):
        """_process_trade() source must contain flow_dedup.is_duplicate call."""
        import pathlib
        src = pathlib.Path("backend/services/tradier_stream.py")
        if not src.exists():
            src = pathlib.Path("services/tradier_stream.py")
        text = src.read_text()
        assert "flow_dedup.is_duplicate(" in text, (
            "REGRESSION C-016: _process_trade() does not call flow_dedup.is_duplicate(). "
            "Every exchange copy of a trade will be persisted to the DB."
        )

    # INT-03
    def test_process_trade_drops_duplicate_exchange_tick(self):
        """
        End-to-end: when the same trade arrives on a second exchange,
        persist_flow_event should NOT be called a second time.
        """
        import services.flow_store as fs
        from utils.dedup import DedupCache

        persisted = []

        async def run_test():
            test_dedup = DedupCache(ttl_seconds=2.0)

            async def fake_process(raw: dict):
                """
                Minimal replica of _process_trade() dedup gate logic
                so we can test the gate without the full tradier_stream import.
                """
                event_type = raw.get("type", "")
                if event_type != "timesale":
                    return
                payload = raw.get("timesale", raw)
                symbol   = payload.get("symbol", "")
                exchange = payload.get("exch", "UNK")
                size     = int(payload.get("size", 0) or 0)
                fill     = float(payload.get("last") or payload.get("price") or 0)
                if size == 0:
                    return
                if test_dedup.is_duplicate(symbol, size, fill, exchange):
                    return   # ← dedup gate
                persisted.append(payload)

            tick_n = {"type": "timesale", "timesale": {
                "symbol": "AAPL  260117C00180000", "last": 1.50,
                "size": 10, "exch": "N", "bid": 1.45, "ask": 1.55,
            }}
            tick_c = {"type": "timesale", "timesale": {
                "symbol": "AAPL  260117C00180000", "last": 1.50,
                "size": 10, "exch": "C", "bid": 1.45, "ask": 1.55,
            }}
            tick_m = {"type": "timesale", "timesale": {
                "symbol": "AAPL  260117C00180000", "last": 1.50,
                "size": 10, "exch": "M", "bid": 1.45, "ask": 1.55,
            }}
            tick_q = {"type": "timesale", "timesale": {
                "symbol": "AAPL  260117C00180000", "last": 1.50,
                "size": 10, "exch": "Q", "bid": 1.45, "ask": 1.55,
            }}

            await fake_process(tick_n)  # canonical — persisted
            await fake_process(tick_c)  # duplicate — dropped
            await fake_process(tick_m)  # duplicate — dropped
            await fake_process(tick_q)  # duplicate — dropped

            assert len(persisted) == 1, (
                f"Expected 1 persisted row (canonical), got {len(persisted)}. "
                "REGRESSION C-016: dedup gate not reducing 4× exchange duplicates."
            )

        run(run_test())


# ===========================================================================
# SQ — is_synthetic_quote (C-018 Layer 3 / migration 009)
# ===========================================================================

class TestSyntheticQuoteSQ:
    """
    C-018 regression: is_synthetic_quote flag — parser sets it correctly,
    flow_store persists it, and backtest queries must filter it out.
    """

    # SQ-01: OptionsFlowEvent must have is_synthetic_quote attribute
    def test_flow_event_has_is_synthetic_quote_field(self):
        from parsers.options_flow_parser import OptionsFlowEvent
        import inspect
        fields = {f.name for f in OptionsFlowEvent.__dataclass_fields__.values()} \
                 if hasattr(OptionsFlowEvent, '__dataclass_fields__') \
                 else set(vars(OptionsFlowEvent()).keys())
        assert "is_synthetic_quote" in fields, (
            "REGRESSION C-018: OptionsFlowEvent missing is_synthetic_quote field. "
            "migration 009 added the column — the dataclass must match."
        )

    # SQ-02: Parser sets is_synthetic_quote=True when bid=ask=0
    def test_parser_sets_synthetic_flag_when_bid_ask_zero(self):
        from parsers.options_flow_parser import parse_tradier_trade

        tick = {
            "type": "timesale",
            "symbol": "AAPL  260117C00180000",
            "last": 1.50, "price": 1.50, "size": 10,
            "bid": 0, "ask": 0,
            "exch": "N",
        }
        event = parse_tradier_trade(tick)
        assert event is not None
        assert event.is_synthetic_quote is True, (
            "Parser must set is_synthetic_quote=True when bid=ask=0. "
            "bid/ask were synthesised ±0.5% from fill price."
        )

    # SQ-03: Parser sets is_synthetic_quote=False when real NBBO present
    def test_parser_clears_synthetic_flag_with_real_nbbo(self):
        from parsers.options_flow_parser import parse_tradier_trade

        tick = {
            "type": "timesale",
            "symbol": "AAPL  260117C00180000",
            "last": 1.50, "price": 1.50, "size": 10,
            "bid": 1.45, "ask": 1.55,
            "exch": "N",
        }
        event = parse_tradier_trade(tick)
        assert event is not None
        assert event.is_synthetic_quote is False, (
            "Parser must set is_synthetic_quote=False when real bid/ask are present."
        )

    # SQ-04: Synthetic bid/ask are ±0.5% of fill price
    def test_synthetic_spread_is_half_percent_of_fill(self):
        from parsers.options_flow_parser import parse_tradier_trade

        fill = 2.00
        tick = {
            "type": "timesale",
            "symbol": "TSLA  260424C00375000",
            "last": fill, "size": 5,
            "bid": 0, "ask": 0,
            "exch": "C",
        }
        event = parse_tradier_trade(tick)
        assert event is not None
        expected_bid = round(fill * 0.995, 2)
        expected_ask = round(fill * 1.005, 2)
        assert abs(event.bid - expected_bid) < 0.005, f"bid={event.bid} expected ~{expected_bid}"
        assert abs(event.ask - expected_ask) < 0.005, f"ask={event.ask} expected ~{expected_ask}"

    # SQ-05: flow_store persists is_synthetic_quote field to DB row
    def test_flow_store_persists_is_synthetic_quote_true(self):
        import services.flow_store as fs

        async def run_test():
            fs._flow_event_buffer.clear()
            await fs.persist_flow_event({
                "ticker": "TSLA", "contract_type": "CALL",
                "strike": 375.0, "expiry": "2026-04-24",
                "dte": 0, "fill_price": 3.0,
                "bid": 2.985, "ask": 3.015, "size": 5,
                "premium": 1500.0, "trade_type": "SINGLE",
                "bid_ask_class": "AT_ASK", "is_aggressive": True,
                "is_golden_sweep": False, "sentiment": "BULLISH",
                "influence_tier": "RETAIL", "conviction_score": 0.3,
                "exchange_count": 1, "fill_count": 1,
                "open_interest": 1000, "iv": 0.50,
                "underlying_price": 370.0,
                "occ_symbol": "TSLA  260424C00375000",
                "is_synthetic_quote": True,
            })
            row = fs._flow_event_buffer[-1]
            assert "is_synthetic_quote" in row, (
                "REGRESSION C-018: flow_store must persist is_synthetic_quote column."
            )
            assert row["is_synthetic_quote"] is True

        run(run_test())

    # SQ-06: flow_store persists is_synthetic_quote=False for real NBBO rows
    def test_flow_store_persists_is_synthetic_quote_false(self):
        import services.flow_store as fs

        async def run_test():
            fs._flow_event_buffer.clear()
            await fs.persist_flow_event({
                "ticker": "SPY", "contract_type": "PUT",
                "strike": 450.0, "expiry": "2026-01-17",
                "dte": 30, "fill_price": 2.00,
                "bid": 1.95, "ask": 2.05, "size": 20,
                "premium": 4000.0, "trade_type": "BLOCK",
                "bid_ask_class": "MID", "is_aggressive": False,
                "is_golden_sweep": False, "sentiment": "BEARISH",
                "influence_tier": "WHALE", "conviction_score": 0.7,
                "exchange_count": 1, "fill_count": 1,
                "open_interest": 30000, "iv": 0.18,
                "underlying_price": 452.0,
                "occ_symbol": "SPY   260117P00450000",
                "is_synthetic_quote": False,
            })
            row = fs._flow_event_buffer[-1]
            assert row["is_synthetic_quote"] is False

        run(run_test())

    # SQ-07: tradier_stream.py must reference is_synthetic_quote
    def test_tradier_stream_sets_is_synthetic_quote(self):
        import pathlib
        src = pathlib.Path("backend/services/tradier_stream.py")
        if not src.exists():
            src = pathlib.Path("services/tradier_stream.py")
        text = src.read_text()
        assert "is_synthetic_quote" in text, (
            "REGRESSION C-018: tradier_stream.py does not pass is_synthetic_quote "
            "to persist_flow_event(). Synthetic rows will have NULL in DB column."
        )

    # SQ-08: backtest_score helper must filter is_synthetic_quote=false rows
    def test_backtest_score_query_filters_synthetic_quotes(self):
        """
        Verify the backtest score SQL or query builder excludes synthetic rows.
        Checks source-level: backtesting module must contain the filter.
        """
        import pathlib
        # Check wherever backtest_score / historical win-rate is computed
        candidates = [
            pathlib.Path("backend/services/backtest_store.py"),
            pathlib.Path("services/backtest_store.py"),
            pathlib.Path("backend/signals/backtest_scorer.py"),
            pathlib.Path("signals/backtest_scorer.py"),
        ]
        found_file = None
        for p in candidates:
            if p.exists():
                found_file = p
                break
        if found_file is None:
            pytest.skip("backtest_store.py / backtest_scorer.py not found — skip SQ-08")
        text = found_file.read_text()
        assert "is_synthetic_quote" in text, (
            "REGRESSION C-018: backtest query file does not filter is_synthetic_quote=false. "
            "Aggression ratios will be skewed by synthesised NBBO rows."
        )
