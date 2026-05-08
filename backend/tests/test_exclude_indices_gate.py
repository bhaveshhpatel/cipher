"""
test_exclude_indices_gate.py — ING-011: Stream-side exclude_indices gate tests.

Tests the _resolve_exclude_indices() resolver and the Gate-6 check in
_process_trade() that drops index ETF ticks before parse_tradier_trade()
is ever called.

Test matrix
-----------
  EI-1  _resolve_exclude_indices returns True  when store returns 1.0
  EI-2  _resolve_exclude_indices returns False when store returns 0.0
  EI-3  _resolve_exclude_indices returns True (safe fallback) on Exception
  EI-4  _resolve_exclude_indices safe-fallback on None return from store
  EI-5  _process_trade drops INDEX tick when gate ON → index_filtered++, no parse
  EI-6  _process_trade passes INDEX tick when gate OFF → parse called
  EI-7  _process_trade passes NON-INDEX tick even when gate ON → parse called
  EI-8  _INDEX_SYMBOLS frozenset contains canonical index ETF tickers
  EI-9  _stats[\"index_filtered\"] starts at 0 and increments per suppressed tick
  EI-10 gate check fires BEFORE parse_tradier_trade (no parse cost for filtered ticks)

Patch targets (all module-level in services.tradier_stream)
-----------------------------------------------------------
  services.tradier_stream.gate_config_store  — controls _resolve_exclude_indices
  services.tradier_stream.parse_tradier_trade — sentinel to assert not called on filter
  services.tradier_stream._stats             — read for counter assertions

No live Supabase, Tradier, or asyncio stream dependencies.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store_mock(get_value: float | None = 1.0, raises: bool = False) -> MagicMock:
    """Return a mock gate_config_store with .get() configured."""
    m = MagicMock()
    if raises:
        m.get.side_effect = Exception("store unavailable")
    else:
        m.get.return_value = get_value
    m.epoch = 1
    return m


def _spy_tick(underlying: str = "SPY") -> dict:
    """Return a minimal raw timesale dict with the given underlying ticker."""
    return {
        "type": "timesale",
        "timesale": {
            "symbol":     f"{underlying}251219C00500000",
            "underlying": underlying,
            "last":       "2.50",
            "bid":        "2.45",
            "ask":        "2.55",
            "size":       "10",
            "exch":       "C",
        },
    }


# ---------------------------------------------------------------------------
# EI-1 / EI-2 / EI-3 / EI-4 — _resolve_exclude_indices unit tests
# ---------------------------------------------------------------------------

class TestResolveExcludeIndices:
    """Unit-test _resolve_exclude_indices() in isolation."""

    def test_ei1_returns_true_when_store_returns_1(self):
        """EI-1: gate ON (store value = 1.0) → True."""
        from services import tradier_stream as ts
        mock_store = _make_store_mock(get_value=1.0)
        with patch.object(ts, "gate_config_store", mock_store):
            result = ts._resolve_exclude_indices()
        assert result is True

    def test_ei2_returns_false_when_store_returns_0(self):
        """EI-2: gate OFF (store value = 0.0) → False."""
        from services import tradier_stream as ts
        mock_store = _make_store_mock(get_value=0.0)
        with patch.object(ts, "gate_config_store", mock_store):
            result = ts._resolve_exclude_indices()
        assert result is False

    def test_ei3_returns_true_on_exception(self):
        """EI-3: store.get() raises → safe fallback True (filter stays ON)."""
        from services import tradier_stream as ts
        mock_store = _make_store_mock(raises=True)
        with patch.object(ts, "gate_config_store", mock_store):
            result = ts._resolve_exclude_indices()
        assert result is True

    def test_ei4_returns_true_on_none_from_store(self):
        """EI-4: store.get() returns None → bool(None >= 0.5) raises → fallback True."""
        from services import tradier_stream as ts

        # Simulate store returning None — the `val >= 0.5` comparison raises TypeError
        # which the except block catches and returns True.
        mock_store = MagicMock()
        mock_store.get.return_value = None
        mock_store.epoch = 1
        with patch.object(ts, "gate_config_store", mock_store):
            result = ts._resolve_exclude_indices()
        assert result is True

    def test_ei1_store_called_with_correct_args(self):
        """_resolve_exclude_indices always queries gate='exclude_indices', tier=1."""
        from services import tradier_stream as ts
        mock_store = _make_store_mock(get_value=1.0)
        with patch.object(ts, "gate_config_store", mock_store):
            ts._resolve_exclude_indices()
        mock_store.get.assert_called_once_with("exclude_indices", 1)


# ---------------------------------------------------------------------------
# EI-8 — _INDEX_SYMBOLS contents
# ---------------------------------------------------------------------------

class TestIndexSymbolsSet:
    """EI-8: Verify _INDEX_SYMBOLS contains the expected canonical tickers."""

    def test_ei8_contains_spy_qqq(self):
        from services.tradier_stream import _INDEX_SYMBOLS
        assert "SPY" in _INDEX_SYMBOLS
        assert "QQQ" in _INDEX_SYMBOLS

    def test_ei8_contains_all_canonical_tickers(self):
        from services.tradier_stream import _INDEX_SYMBOLS
        expected = {"SPY", "QQQ", "IWM", "DIA", "VXX", "GLD", "TLT", "HYG", "EEM", "SLV"}
        assert expected == _INDEX_SYMBOLS

    def test_ei8_is_frozenset(self):
        from services.tradier_stream import _INDEX_SYMBOLS
        assert isinstance(_INDEX_SYMBOLS, frozenset)

    def test_ei8_non_index_ticker_not_in_set(self):
        from services.tradier_stream import _INDEX_SYMBOLS
        for ticker in ("AAPL", "TSLA", "NVDA", "MSFT", "AMZN"):
            assert ticker not in _INDEX_SYMBOLS, f"{ticker} should not be in _INDEX_SYMBOLS"


# ---------------------------------------------------------------------------
# EI-5 / EI-6 / EI-7 / EI-9 / EI-10 — _process_trade integration tests
# ---------------------------------------------------------------------------

class TestProcessTradeIndexFilter:
    """Integration tests for Gate-6 (exclude_indices) inside _process_trade."""

    # Shared patch targets
    _PARSE_TARGET       = "services.tradier_stream.parse_tradier_trade"
    _STORE_TARGET       = "services.tradier_stream.gate_config_store"
    _PERSIST_EV_TARGET  = "services.tradier_stream.persist_flow_event"
    _ACCUM_TARGET       = "services.tradier_stream.accumulator"
    _BUS_TARGET         = "services.tradier_stream.bus"

    def _make_base_patches(self, gate_on: bool = True):
        """Return a dict of patch kwargs shared across process_trade tests."""
        mock_store = _make_store_mock(get_value=1.0 if gate_on else 0.0)
        return mock_store

    @pytest.mark.asyncio
    async def test_ei5_index_tick_dropped_when_gate_on(self):
        """
        EI-5: SPY tick + gate ON → tick dropped before parse.
        index_filtered incremented by 1.
        parse_tradier_trade must NOT be called.
        """
        from services import tradier_stream as ts

        # Reset counter before test
        ts._stats["index_filtered"] = 0
        ts._stats["ticks"] = 0

        mock_store = _make_store_mock(get_value=1.0)

        with patch(self._PARSE_TARGET) as mock_parse, \
             patch.object(ts, "gate_config_store", mock_store):
            await ts._process_trade(_spy_tick("SPY"))

        mock_parse.assert_not_called()
        assert ts._stats["index_filtered"] == 1

    @pytest.mark.asyncio
    async def test_ei6_index_tick_passes_when_gate_off(self):
        """
        EI-6: SPY tick + gate OFF → parse is called (may fail, that's fine).
        index_filtered must NOT increment.
        """
        from services import tradier_stream as ts

        ts._stats["index_filtered"] = 0
        ts._stats["ticks"] = 0

        mock_store = _make_store_mock(get_value=0.0)

        # parse returns None → _stats["parse_failed"] increments, but that's OK here.
        with patch(self._PARSE_TARGET, return_value=None) as mock_parse, \
             patch.object(ts, "gate_config_store", mock_store):
            await ts._process_trade(_spy_tick("SPY"))

        mock_parse.assert_called_once()
        assert ts._stats["index_filtered"] == 0

    @pytest.mark.asyncio
    async def test_ei7_non_index_ticker_passes_when_gate_on(self):
        """
        EI-7: AAPL tick + gate ON → AAPL not in _INDEX_SYMBOLS → parse called.
        index_filtered must NOT increment.
        """
        from services import tradier_stream as ts

        ts._stats["index_filtered"] = 0
        ts._stats["ticks"] = 0

        mock_store = _make_store_mock(get_value=1.0)

        with patch(self._PARSE_TARGET, return_value=None) as mock_parse, \
             patch.object(ts, "gate_config_store", mock_store):
            await ts._process_trade(_spy_tick("AAPL"))

        mock_parse.assert_called_once()
        assert ts._stats["index_filtered"] == 0

    @pytest.mark.asyncio
    async def test_ei9_index_filtered_increments_per_suppressed_tick(self):
        """
        EI-9: Three consecutive index ticks with gate ON → index_filtered == 3.
        """
        from services import tradier_stream as ts

        ts._stats["index_filtered"] = 0
        ts._stats["ticks"] = 0

        mock_store = _make_store_mock(get_value=1.0)

        with patch(self._PARSE_TARGET) as mock_parse, \
             patch.object(ts, "gate_config_store", mock_store):
            await ts._process_trade(_spy_tick("QQQ"))
            await ts._process_trade(_spy_tick("IWM"))
            await ts._process_trade(_spy_tick("DIA"))

        mock_parse.assert_not_called()
        assert ts._stats["index_filtered"] == 3

    @pytest.mark.asyncio
    async def test_ei10_parse_not_called_for_filtered_index_tick(self):
        """
        EI-10: Gate check fires BEFORE parse_tradier_trade.
        parse must never be reached for filtered ticks (no parse cost).
        """
        from services import tradier_stream as ts

        ts._stats["index_filtered"] = 0
        ts._stats["parse_failed"] = 0
        ts._stats["ticks"] = 0

        mock_store = _make_store_mock(get_value=1.0)

        with patch(self._PARSE_TARGET) as mock_parse, \
             patch.object(ts, "gate_config_store", mock_store):
            await ts._process_trade(_spy_tick("GLD"))

        # parse was never called — gate check is pre-parse
        assert mock_parse.call_count == 0
        # parse_failed must not have incremented (parse was not attempted)
        assert ts._stats["parse_failed"] == 0

    @pytest.mark.asyncio
    async def test_ei5_all_index_symbols_are_filtered(self):
        """
        EI-5 extended: every ticker in _INDEX_SYMBOLS is dropped when gate ON.
        """
        from services import tradier_stream as ts
        from services.tradier_stream import _INDEX_SYMBOLS

        ts._stats["index_filtered"] = 0
        ts._stats["ticks"] = 0

        mock_store = _make_store_mock(get_value=1.0)

        with patch(self._PARSE_TARGET) as mock_parse, \
             patch.object(ts, "gate_config_store", mock_store):
            for ticker in sorted(_INDEX_SYMBOLS):
                await ts._process_trade(_spy_tick(ticker))

        assert mock_parse.call_count == 0
        assert ts._stats["index_filtered"] == len(_INDEX_SYMBOLS)

    @pytest.mark.asyncio
    async def test_ei2_gate_off_all_index_symbols_reach_parse(self):
        """
        EI-2 extended: gate OFF → every _INDEX_SYMBOLS ticker reaches parse.
        """
        from services import tradier_stream as ts
        from services.tradier_stream import _INDEX_SYMBOLS

        ts._stats["index_filtered"] = 0
        ts._stats["ticks"] = 0

        mock_store = _make_store_mock(get_value=0.0)

        with patch(self._PARSE_TARGET, return_value=None) as mock_parse, \
             patch.object(ts, "gate_config_store", mock_store):
            for ticker in sorted(_INDEX_SYMBOLS):
                await ts._process_trade(_spy_tick(ticker))

        assert mock_parse.call_count == len(_INDEX_SYMBOLS)
        assert ts._stats["index_filtered"] == 0
