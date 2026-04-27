"""
Tests for services/signal_store.py

Covers (merged from test_signal_store_coverage.py as of 2026-04-27):
 - In-memory store: save, get, clear, max-cap, TTL-expiry
 - save_signal / get_signals / get_recent_signals (SDK + httpx paths)
 - _coerce_to_dict: dict passthrough, __dict__, model_dump, fallback
 - _build_row: all alert_level tiers, explicit override, sentiment derivation, swarm fields
 - _insert_signal: no-credentials, 201 OK, non-201, exception
 - _insert_signal_with_retry: first-attempt, exhaustion, second-attempt recovery
 - save_signal SDK mock path, insert exception, no-credentials in-memory
 - get_signals SDK path, exception fallthrough, get_recent_signals alias
 - persist_composite_signal: not-configured, insert OK, insert fail
 - start_signal_writer: not-configured, configured+cancel
 - _bus_signal_listener: composite_signal processing + cancel
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.signal_store import (
    _clear_signal_memory,
    _build_row,
    _coerce_to_dict,
    save_signal,
    get_signals,
    get_recent_signals,
    persist_composite_signal,
    start_signal_writer,
    _insert_signal,
    _insert_signal_with_retry,
)


# ===========================================================================
# Fixtures / helpers
# ===========================================================================

@pytest.fixture(autouse=True)
def _clean_memory():
    """Reset in-memory signal store before every test."""
    _clear_signal_memory()
    yield
    _clear_signal_memory()


def _make_resp(status, json_data=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data or {}
    r.text = ""
    return r


def _async_client_ctx(mock_response):
    """Build an async context-manager mock for httpx.AsyncClient."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=MagicMock(post=AsyncMock(return_value=mock_response)))
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


# ===========================================================================
# In-memory store basics
# ===========================================================================

def test_save_signal_stores_in_memory_when_not_configured():
    with patch("services.signal_store._is_configured", return_value=False), \
         patch("services.signal_store._client", return_value=None):
        result = asyncio.run(save_signal({"id": "mem-1", "ticker": "AAPL"}))
    assert result is True
    signals = asyncio.run(get_signals())
    assert any(s.get("ticker") == "AAPL" for s in signals)


def test_get_signals_returns_list():
    signals = asyncio.run(get_signals())
    assert isinstance(signals, list)


def test_get_signals_filters_by_ticker():
    with patch("services.signal_store._is_configured", return_value=False), \
         patch("services.signal_store._client", return_value=None):
        asyncio.run(save_signal({"ticker": "AAPL", "id": "a"}))
        asyncio.run(save_signal({"ticker": "TSLA", "id": "b"}))
    rows = asyncio.run(get_signals(ticker="AAPL"))
    assert all(s.get("ticker") == "AAPL" for s in rows)


def test_save_multiple_signals_in_memory():
    with patch("services.signal_store._is_configured", return_value=False), \
         patch("services.signal_store._client", return_value=None):
        for i in range(5):
            asyncio.run(save_signal({"ticker": f"SYM{i}", "id": str(i)}))
    assert len(asyncio.run(get_signals())) == 5


def test_clear_signal_memory_empties_store():
    with patch("services.signal_store._is_configured", return_value=False), \
         patch("services.signal_store._client", return_value=None):
        asyncio.run(save_signal({"ticker": "AAPL", "id": "x"}))
    _clear_signal_memory()
    assert asyncio.run(get_signals()) == []


# ===========================================================================
# _coerce_to_dict
# ===========================================================================

def test_coerce_dict_passthrough():
    d = {"ticker": "AAPL"}
    assert _coerce_to_dict(d) is d


def test_coerce_object_with_dict():
    class Obj:
        def __init__(self):
            self.ticker = "TSLA"

    assert _coerce_to_dict(Obj())["ticker"] == "TSLA"


def test_coerce_pydantic_like():
    class PModel:
        def model_dump(self):
            return {"ticker": "NVDA"}

    assert _coerce_to_dict(PModel())["ticker"] == "NVDA"


def test_coerce_fallback_empty():
    class Bad:
        pass

    result = _coerce_to_dict(Bad())
    assert isinstance(result, dict)


# ===========================================================================
# _build_row — alert_level tiers
# ===========================================================================

def test_build_row_conviction_score():
    row = _build_row({"composite_score": 0.90, "flow_score": 0.9, "ticker": "AAPL",
                      "recommendation": "BUY", "backtest_score": 0.9})
    assert row["alert_level"] == "CONVICTION"


def test_build_row_strong_signal():
    row = _build_row({"composite_score": 0.75, "flow_score": 0.7, "ticker": "AAPL",
                      "recommendation": "BUY", "backtest_score": 0.7})
    assert row["alert_level"] == "STRONG_SIGNAL"


def test_build_row_alert():
    row = _build_row({"composite_score": 0.60, "flow_score": 0.6, "ticker": "AAPL",
                      "recommendation": "BUY", "backtest_score": 0.6})
    assert row["alert_level"] == "ALERT"


def test_build_row_watch():
    row = _build_row({"composite_score": 0.30, "flow_score": 0.3, "ticker": "AAPL",
                      "recommendation": "HOLD", "backtest_score": 0.3})
    assert row["alert_level"] == "WATCH"


def test_build_row_explicit_alert_level_wins():
    row = _build_row({"composite_score": 0.10, "alert_level": "CONVICTION",
                      "flow_score": 0.1, "ticker": "X", "recommendation": "HOLD",
                      "backtest_score": 0.1})
    assert row["alert_level"] == "CONVICTION"


def test_build_row_sentiment_from_ctype_call():
    row = _build_row({"composite_score": 0.5, "flow_score": 0.5, "ticker": "AAPL",
                      "recommendation": "BUY", "backtest_score": 0.5},
                     ep={"contract_type": "CALL", "direction": ""})
    assert row["sentiment"] == "BULLISH"


def test_build_row_sentiment_from_ctype_put():
    row = _build_row({"composite_score": 0.5, "flow_score": 0.5, "ticker": "AAPL",
                      "recommendation": "SELL", "backtest_score": 0.5},
                     ep={"contract_type": "PUT", "direction": ""})
    assert row["sentiment"] == "BEARISH"


def test_build_row_sentiment_explicit_wins():
    row = _build_row({"composite_score": 0.5, "flow_score": 0.5, "ticker": "AAPL",
                      "recommendation": "BUY", "backtest_score": 0.5,
                      "sentiment": "NEUTRAL"})
    assert row["sentiment"] == "NEUTRAL"


def test_build_row_swarm_fields():
    row = _build_row({"composite_score": 0.5, "flow_score": 0.5, "ticker": "AAPL",
                      "recommendation": "BUY", "backtest_score": 0.5,
                      "swarm_direction": "BUY", "swarm_confidence": 0.8,
                      "swarm_bull_votes": 4, "swarm_bear_votes": 1, "swarm_hold_votes": 1,
                      "swarm_agents": [{"role": "A", "verdict": "BUY"}]})
    assert row["swarm_direction"] == "BUY"
    assert row["swarm_bull_votes"] == 4


# ===========================================================================
# _insert_signal
# ===========================================================================

def test_insert_signal_no_credentials():
    with patch("services.signal_store._SUPABASE_URL", None):
        result = asyncio.run(_insert_signal({"ticker": "AAPL"}))
    assert result is False


def test_insert_signal_201_returns_true():
    ctx = _async_client_ctx(_make_resp(201))
    with patch("services.signal_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.signal_store._SUPABASE_KEY", "key"), \
         patch("services.signal_store.httpx.AsyncClient", return_value=ctx):
        assert asyncio.run(_insert_signal({"ticker": "AAPL"})) is True


def test_insert_signal_non201_returns_false():
    ctx = _async_client_ctx(_make_resp(500))
    with patch("services.signal_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.signal_store._SUPABASE_KEY", "key"), \
         patch("services.signal_store.httpx.AsyncClient", return_value=ctx):
        assert asyncio.run(_insert_signal({"ticker": "AAPL"})) is False


def test_insert_signal_exception_returns_false():
    with patch("services.signal_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.signal_store._SUPABASE_KEY", "key"), \
         patch("services.signal_store.httpx.AsyncClient", side_effect=Exception("net")):
        assert asyncio.run(_insert_signal({"ticker": "AAPL"})) is False


# ===========================================================================
# _insert_signal_with_retry
# ===========================================================================

def test_retry_succeeds_first_attempt():
    with patch("services.signal_store._insert_signal", new=AsyncMock(return_value=True)):
        assert asyncio.run(_insert_signal_with_retry({"ticker": "AAPL"})) is True


def test_retry_exhausts_returns_false():
    with patch("services.signal_store._insert_signal", new=AsyncMock(return_value=False)), \
         patch("services.signal_store.asyncio.sleep", new=AsyncMock()):
        assert asyncio.run(_insert_signal_with_retry({"ticker": "AAPL"})) is False


def test_retry_succeeds_on_second_attempt():
    call_count = [0]

    async def _mock_insert(row):
        call_count[0] += 1
        return call_count[0] >= 2

    with patch("services.signal_store._insert_signal", side_effect=_mock_insert), \
         patch("services.signal_store.asyncio.sleep", new=AsyncMock()):
        assert asyncio.run(_insert_signal_with_retry({"ticker": "AAPL"})) is True


# ===========================================================================
# save_signal — SDK paths
# ===========================================================================

def test_save_signal_sdk_mock_path():
    mock_client = MagicMock()
    mock_client.table.return_value.insert.return_value.execute.return_value = MagicMock()
    with patch("services.signal_store._client", return_value=mock_client):
        assert asyncio.run(save_signal({"ticker": "AAPL", "composite_score": 0.8})) is True


def test_save_signal_sdk_insert_exception():
    mock_client = MagicMock()
    mock_client.table.side_effect = RuntimeError("db error")
    with patch("services.signal_store._client", return_value=mock_client):
        assert asyncio.run(save_signal({"ticker": "AAPL", "composite_score": 0.8})) is False


def test_save_signal_no_credentials_stores_in_memory():
    with patch("services.signal_store._is_configured", return_value=False), \
         patch("services.signal_store._client", return_value=None):
        assert asyncio.run(save_signal({"id": "sig-1", "ticker": "TSLA"})) is True
    signals = asyncio.run(get_signals())
    assert any(s.get("ticker") == "TSLA" for s in signals)


# ===========================================================================
# get_signals — SDK paths
# ===========================================================================

def test_get_signals_sdk_path():
    mock_result = MagicMock()
    mock_result.data = [{"ticker": "AAPL", "id": 1}]
    mock_client = MagicMock()
    (mock_client.table.return_value
     .select.return_value
     .order.return_value
     .limit.return_value
     .execute.return_value) = mock_result
    with patch("services.signal_store._client", return_value=mock_client):
        rows = asyncio.run(get_signals(ticker="AAPL"))
    assert rows[0]["ticker"] == "AAPL"


def test_get_signals_sdk_exception_falls_through_to_memory():
    mock_client = MagicMock()
    mock_client.table.side_effect = RuntimeError("err")
    with patch("services.signal_store._client", return_value=mock_client):
        assert isinstance(asyncio.run(get_signals()), list)


def test_get_recent_signals_alias():
    assert isinstance(asyncio.run(get_recent_signals()), list)


# ===========================================================================
# persist_composite_signal
# ===========================================================================

def test_persist_composite_signal_not_configured_returns_early():
    with patch("services.signal_store._is_configured", return_value=False):
        asyncio.run(
            persist_composite_signal({"ticker": "AAPL", "composite_score": 0.9,
                                       "flow_score": 0.9, "recommendation": "BUY",
                                       "backtest_score": 0.8})
        )


def test_persist_composite_signal_insert_ok():
    with patch("services.signal_store._is_configured", return_value=True), \
         patch("services.signal_store._insert_signal_with_retry", new=AsyncMock(return_value=True)):
        asyncio.run(
            persist_composite_signal(
                {"ticker": "AAPL", "composite_score": 0.9, "flow_score": 0.9,
                 "recommendation": "BUY", "backtest_score": 0.8,
                 "swarm_direction": "BUY", "swarm_bull_votes": 5,
                 "swarm_bear_votes": 1, "swarm_hold_votes": 0,
                 "is_golden_sweep": True},
                ep={"total_premium": 500_000, "trade_count": 3,
                    "is_accelerating": True, "timestamp": "2026-04-26T12:00:00"}
            )
        )


def test_persist_composite_signal_insert_fail():
    with patch("services.signal_store._is_configured", return_value=True), \
         patch("services.signal_store._insert_signal_with_retry", new=AsyncMock(return_value=False)):
        asyncio.run(
            persist_composite_signal(
                {"ticker": "AAPL", "composite_score": 0.5, "flow_score": 0.5,
                 "recommendation": "HOLD", "backtest_score": 0.5}
            )
        )


# ===========================================================================
# start_signal_writer
# ===========================================================================

def test_start_signal_writer_not_configured_returns_early():
    with patch("services.signal_store._is_configured", return_value=False):
        asyncio.run(start_signal_writer())


def test_start_signal_writer_configured_cancels():
    async def _run():
        with patch("services.signal_store._is_configured", return_value=True), \
             patch("services.signal_store._bus_signal_listener",
                   new=AsyncMock(side_effect=asyncio.CancelledError)):
            try:
                await start_signal_writer()
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())


# ===========================================================================
# _bus_signal_listener
# ===========================================================================

def test_bus_signal_listener_processes_composite_signal_and_cancels():
    from services import signal_store

    call_log = []

    async def _fake_persist(sig, ep):
        call_log.append(sig)

    async def _run():
        q = asyncio.Queue()
        await q.put({"type": "composite_signal", "data": {
            "signal":  {"ticker": "AAPL", "composite_score": 0.9},
            "episode": {"total_premium": 200_000},
        }})

        with patch.object(signal_store, "persist_composite_signal", side_effect=_fake_persist), \
             patch("services.signal_store.bus") as mock_bus:
            mock_bus.subscribe.return_value = q
            mock_bus.unsubscribe = MagicMock()

            async def _cancel_after_first():
                await asyncio.sleep(0.05)
                task.cancel()

            task = asyncio.create_task(signal_store._bus_signal_listener())
            asyncio.create_task(_cancel_after_first())
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())
    assert len(call_log) == 1
    assert call_log[0]["ticker"] == "AAPL"
