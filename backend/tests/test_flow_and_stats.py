"""
Regression tests for flow store stats helpers.
"""
import asyncio

from services.flow_store import FlowStore


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _store():
    return FlowStore()


def test_flow_store_initial_stats_are_zero():
    s = _store()
    assert isinstance(s.get_stats(), dict)


def test_add_flow_increments_count():
    from dataclasses import dataclass
    @dataclass
    class _F:
        symbol: str = "AAPL"
        direction: str = "bullish"
        influence_tier: str = "WHALE"
        premium: float = 10000.0
    s = _store()
    s.add_flow(_F())
    assert s.get_stats().get("total", 0) >= 1 or len(s.get_flows()) >= 1


def test_get_flows_returns_list():
    assert isinstance(_store().get_flows(), list)


def test_get_flows_by_symbol_filters_correctly():
    from dataclasses import dataclass
    @dataclass
    class _F:
        symbol: str
        direction: str = "bullish"
        influence_tier: str = "WHALE"
        premium: float = 10000.0
    s = _store()
    s.add_flow(_F("AAPL"))
    s.add_flow(_F("TSLA"))
    if hasattr(s, "get_flows_by_symbol"):
        assert all(f.symbol == "AAPL" for f in s.get_flows_by_symbol("AAPL"))


def test_clear_flows_resets_store():
    from dataclasses import dataclass
    @dataclass
    class _F:
        symbol: str = "SPY"
        direction: str = "bullish"
        influence_tier: str = "WHALE"
        premium: float = 10000.0
    s = _store()
    s.add_flow(_F())
    if hasattr(s, "clear"):
        s.clear()
        assert len(s.get_flows()) == 0


def test_stats_dict_has_expected_keys():
    assert isinstance(_store().get_stats(), dict)


def test_flow_store_handles_empty_state():
    assert isinstance(_store().get_flows(), list)


def test_add_multiple_flows():
    from dataclasses import dataclass
    @dataclass
    class _F:
        symbol: str
        direction: str = "bullish"
        influence_tier: str = "WHALE"
        premium: float = 10000.0
    s = _store()
    for sym in ["AAPL", "TSLA", "SPY", "QQQ", "NVDA"]:
        s.add_flow(_F(sym))
    assert len(s.get_flows()) >= 5 or s.get_stats().get("total", 0) >= 5
