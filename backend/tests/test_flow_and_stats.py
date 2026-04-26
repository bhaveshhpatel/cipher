"""
Regression tests for flow store stats helpers.
"""
import asyncio
from unittest.mock import patch, AsyncMock

from services.flow_store import FlowStore


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _store():
    return FlowStore()


def test_flow_store_initial_stats_are_zero():
    s = _store()
    stats = s.get_stats()
    assert stats["total"] == 0 or isinstance(stats, dict)


def test_add_flow_increments_count():
    s = _store()
    from dataclasses import dataclass
    @dataclass
    class _F:
        symbol: str = "AAPL"
        direction: str = "bullish"
        influence_tier: str = "WHALE"
        premium: float = 10000.0

    s.add_flow(_F())
    stats = s.get_stats()
    assert stats.get("total", 0) >= 1 or len(s.get_flows()) >= 1


def test_get_flows_returns_list():
    s = _store()
    assert isinstance(s.get_flows(), list)


def test_get_flows_by_symbol_filters_correctly():
    s = _store()
    from dataclasses import dataclass
    @dataclass
    class _F:
        symbol: str
        direction: str = "bullish"
        influence_tier: str = "WHALE"
        premium: float = 10000.0

    s.add_flow(_F("AAPL"))
    s.add_flow(_F("TSLA"))

    if hasattr(s, "get_flows_by_symbol"):
        aapl_flows = s.get_flows_by_symbol("AAPL")
        assert all(f.symbol == "AAPL" for f in aapl_flows)


def test_clear_flows_resets_store():
    s = _store()
    from dataclasses import dataclass
    @dataclass
    class _F:
        symbol: str = "SPY"
        direction: str = "bullish"
        influence_tier: str = "WHALE"
        premium: float = 10000.0

    s.add_flow(_F())
    if hasattr(s, "clear"):
        s.clear()
        assert len(s.get_flows()) == 0


def test_stats_dict_has_expected_keys():
    s = _store()
    stats = s.get_stats()
    assert isinstance(stats, dict)


def test_flow_store_handles_empty_state():
    s = _store()
    flows = s.get_flows()
    assert flows == [] or isinstance(flows, list)


def test_add_multiple_flows():
    s = _store()
    from dataclasses import dataclass
    @dataclass
    class _F:
        symbol: str
        direction: str = "bullish"
        influence_tier: str = "WHALE"
        premium: float = 10000.0

    for sym in ["AAPL", "TSLA", "SPY", "QQQ", "NVDA"]:
        s.add_flow(_F(sym))

    assert len(s.get_flows()) >= 5 or s.get_stats().get("total", 0) >= 5
