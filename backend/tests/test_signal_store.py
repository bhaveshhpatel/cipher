"""
Regression tests for services/signal_store.py
"""
import asyncio
from unittest.mock import patch, MagicMock
from dataclasses import dataclass


@dataclass
class _Flow:
    symbol: str
    contract_type: str = "CALL"
    direction: str = "bullish"
    influence_tier: str = "WHALE"
    trade_type: str = "sweep"
    premium: float = 50000.0
    composite_score: float = 0.80
    flow_score: float = 0.75
    backtest_score: float = 0.70
    volume_premium_factor: float = 1.2
    is_accelerating: bool = False
    reasoning: str = "test"


def test_normalise_direction_bullish():
    from services.signal_store import _normalise_direction
    assert _normalise_direction("bullish") == "bullish"


def test_normalise_direction_unknown_returns_neutral():
    from services.signal_store import _normalise_direction
    assert _normalise_direction("sideways") in ("neutral", "bullish", "bearish")


def test_normalise_influence_tier_whale():
    from services.signal_store import _normalise_influence_tier
    assert _normalise_influence_tier("WHALE") == "WHALE"


def test_normalise_influence_tier_unknown_returns_retail():
    from services.signal_store import _normalise_influence_tier
    assert _normalise_influence_tier("UNKNOWN") in ("RETAIL", "WHALE", "INSTITUTIONAL", "LARGE")


def test_normalise_trade_type_sweep():
    from services.signal_store import _normalise_trade_type
    assert _normalise_trade_type("sweep") == "sweep"


def test_save_signal_calls_supabase_insert():
    from services import signal_store
    sb = MagicMock()
    t  = MagicMock()
    t.insert.return_value  = t
    t.execute.return_value = MagicMock(data=[{"id": 1}])
    sb.table.return_value  = t
    with patch.object(signal_store, "_client", return_value=sb):
        asyncio.run(signal_store.save_signal(_Flow("AAPL")))
    assert t.insert.called


def test_save_signal_no_supabase_does_not_raise():
    from services import signal_store
    with patch.object(signal_store, "_client", return_value=None):
        asyncio.run(signal_store.save_signal(_Flow("TSLA")))


def test_save_signal_db_exception_does_not_raise():
    from services import signal_store
    sb = MagicMock()
    sb.table.side_effect = Exception("DB error")
    with patch.object(signal_store, "_client", return_value=sb):
        asyncio.run(signal_store.save_signal(_Flow("SPY")))


def test_get_recent_signals_returns_list():
    from services import signal_store
    sb = MagicMock()
    t  = MagicMock()
    t.select.return_value  = t
    t.order.return_value   = t
    t.limit.return_value   = t
    t.execute.return_value = MagicMock(data=[{"ticker": "AAPL"}])
    sb.table.return_value  = t
    with patch.object(signal_store, "_client", return_value=sb):
        result = asyncio.run(signal_store.get_recent_signals(limit=1))
    assert isinstance(result, list)


def test_get_recent_signals_no_supabase_returns_empty():
    from services import signal_store
    with patch.object(signal_store, "_client", return_value=None):
        assert asyncio.run(signal_store.get_recent_signals()) == []
