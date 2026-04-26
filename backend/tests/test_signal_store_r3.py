"""
Release-3 additional regression tests for signal_store.
"""
import asyncio
from unittest.mock import patch


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_signal_store_r3_importable():
    import services.signal_store  # noqa: F401


def test_save_signal_with_none_supabase_does_not_raise():
    from services import signal_store
    from dataclasses import dataclass

    @dataclass
    class _F:
        symbol: str = "AAPL"
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

    with patch.object(signal_store, "_client", return_value=None):
        _run(signal_store.save_signal(_F()))


def test_get_recent_signals_returns_list_with_no_client():
    from services import signal_store
    with patch.object(signal_store, "_client", return_value=None):
        assert isinstance(_run(signal_store.get_recent_signals()), list)
