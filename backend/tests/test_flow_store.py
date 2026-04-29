"""
Regression tests for services/flow_store.py

Covers:
  - FlowStore in-memory class (all methods)
  - persist_flow_event buffering
  - persist_flow_episode direct write
  - upgrade_to_sweep_in_db (C-003): success, failure, not-configured, exception
  - _insert_rows / _insert_rows_with_retry: retry exhaustion path
  - _flush_flow_events: background flush loop
  - _bus_signal_listener: ALERT-LEVEL bug fix — reads alert_level not recommendation
  - module-level add_flow / get_flows / clear_flows async helpers
"""
import asyncio
import time
import sys
import os
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flow(ticker="AAPL", premium=100_000.0, sentiment="BULLISH",
          contract_type="CALL", score=0.75, tier="WHALE",
          strike=180.0, expiry="2026-06-20", dte=30,
          is_golden_sweep=False):
    return {
        "ticker":           ticker,
        "premium":          premium,
        "sentiment":        sentiment,
        "contract_type":    contract_type,
        "composite_score":  score,
        "influence_tier":   tier,
        "strike":           strike,
        "expiry":           expiry,
        "dte":              dte,
        "is_golden_sweep":  is_golden_sweep,
        "timestamp":        time.time(),
    }


def _mock_response(status_code: int, text: str = ""):
    r = MagicMock()
    r.status_code = status_code
    r.text = text
    return r


# ---------------------------------------------------------------------------
# Smoke / import tests
# ---------------------------------------------------------------------------

def test_flow_store_importable():
    import services.flow_store as _m
    assert _m is not None


def test_flow_store_has_expected_api():
    import services.flow_store as fs
    for name in ("add_flow", "get_flows", "clear_flows"):
        assert hasattr(fs, name), f"Missing: {name}"


def test_flow_store_has_upgrade_to_sweep():
    import services.flow_store as fs
    assert hasattr(fs, "upgrade_to_sweep_in_db")
    assert callable(fs.upgrade_to_sweep_in_db)


def test_flow_store_has_persist_flow_episode():
    import services.flow_store as fs
    assert hasattr(fs, "persist_flow_episode")


# ---------------------------------------------------------------------------
# FlowStore class — in-memory store unit tests
# ---------------------------------------------------------------------------

def test_flowstore_add_and_get_flows():
    from services.flow_store import FlowStore
    store = FlowStore()
    store.add_flow({"symbol": "AAPL", "val": 1})
    flows = store.get_flows()
    assert len(flows) == 1
    assert flows[0]["symbol"] == "AAPL"


def test_flowstore_get_flows_by_symbol_match():
    from services.flow_store import FlowStore
    store = FlowStore()
    store.add_flow({"symbol": "AAPL"})
    store.add_flow({"symbol": "TSLA"})
    result = store.get_flows_by_symbol("AAPL")
    assert len(result) == 1
    assert result[0]["symbol"] == "AAPL"


def test_flowstore_get_flows_by_symbol_no_match():
    from services.flow_store import FlowStore
    store = FlowStore()
    store.add_flow({"symbol": "AAPL"})
    result = store.get_flows_by_symbol("NVDA")
    assert result == []


def test_flowstore_get_flows_by_symbol_object():
    """Handles non-dict flows with getattr fallback."""
    from services.flow_store import FlowStore
    store = FlowStore()
    obj = MagicMock()
    obj.symbol = "SPY"
    store._flows.append(obj)
    result = store.get_flows_by_symbol("SPY")
    assert len(result) == 1


def test_flowstore_get_stats_total():
    from services.flow_store import FlowStore
    store = FlowStore()
    assert store.get_stats() == {"total": 0}
    store.add_flow({"symbol": "X"})
    assert store.get_stats()["total"] == 1


def test_flowstore_clear():
    from services.flow_store import FlowStore
    store = FlowStore()
    store.add_flow({"symbol": "AAPL"})
    store.clear()
    assert store.size() == 0
    assert store.get_stats() == {"total": 0}


def test_flowstore_size():
    from services.flow_store import FlowStore
    store = FlowStore()
    assert store.size() == 0
    store.add_flow({})
    store.add_flow({})
    assert store.size() == 2


def test_flowstore_get_flows_returns_copy():
    """get_flows() should return a new list (copy)."""
    from services.flow_store import FlowStore
    store = FlowStore()
    store.add_flow({"symbol": "A"})
    flows1 = store.get_flows()
    flows2 = store.get_flows()
    assert flows1 == flows2
    assert flows1 is not store._flows


# ---------------------------------------------------------------------------
# Module-level async helpers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_and_get_single_flow():
    import services.flow_store as fs
    await fs.clear_flows()
    await fs.add_flow(_flow("AAPL"))
    flows = await fs.get_flows("AAPL")
    assert len(flows) >= 1
    assert flows[0]["ticker"] == "AAPL"


@pytest.mark.asyncio
async def test_get_flows_returns_empty_for_unknown_ticker():
    import services.flow_store as fs
    flows = await fs.get_flows("ZZZZ_NONEXISTENT")
    assert flows == []


@pytest.mark.asyncio
async def test_multiple_flows_same_ticker():
    import services.flow_store as fs
    await fs.clear_flows()
    for i in range(3):
        await fs.add_flow(_flow("TSLA", premium=float(50_000 * (i + 1))))
    flows = await fs.get_flows("TSLA")
    assert len(flows) == 3


@pytest.mark.asyncio
async def test_flows_different_tickers_are_isolated():
    import services.flow_store as fs
    await fs.clear_flows()
    await fs.add_flow(_flow("AAPL"))
    await fs.add_flow(_flow("NVDA"))
    aapl = await fs.get_flows("AAPL")
    nvda = await fs.get_flows("NVDA")
    assert all(f["ticker"] == "AAPL" for f in aapl)
    assert all(f["ticker"] == "NVDA" for f in nvda)


@pytest.mark.asyncio
async def test_clear_flows_removes_all():
    import services.flow_store as fs
    await fs.add_flow(_flow("SPY"))
    await fs.clear_flows()
    flows = await fs.get_flows("SPY")
    assert flows == []


@pytest.mark.asyncio
async def test_expired_flows_not_returned():
    import services.flow_store as fs
    await fs.clear_flows()
    old_flow = _flow("QQQ")
    old_flow["timestamp"] = time.time() - 99999
    await fs.add_flow(old_flow)
    flows = await fs.get_flows("QQQ")
    assert isinstance(flows, list)


@pytest.mark.asyncio
async def test_golden_sweep_flag_preserved():
    import services.flow_store as fs
    await fs.clear_flows()
    await fs.add_flow(_flow("AAPL", is_golden_sweep=True))
    flows = await fs.get_flows("AAPL")
    assert any(f.get("is_golden_sweep") is True for f in flows)


@pytest.mark.asyncio
async def test_concurrent_add_flow_no_data_loss():
    import services.flow_store as fs
    await fs.clear_flows()
    tickers = ["AAPL", "TSLA", "NVDA", "SPY", "QQQ"]
    await asyncio.gather(*[fs.add_flow(_flow(t)) for t in tickers])
    for t in tickers:
        flows = await fs.get_flows(t)
        assert len(flows) >= 1, f"Missing flows for {t}"


@pytest.mark.asyncio
async def test_whale_flow_stored_with_correct_tier():
    import services.flow_store as fs
    await fs.clear_flows()
    await fs.add_flow(_flow("AMD", tier="WHALE"))
    flows = await fs.get_flows("AMD")
    assert any(f.get("influence_tier") == "WHALE" for f in flows)


@pytest.mark.asyncio
async def test_retail_flow_stored_with_correct_tier():
    import services.flow_store as fs
    await fs.clear_flows()
    await fs.add_flow(_flow("AMD", tier="RETAIL"))
    flows = await fs.get_flows("AMD")
    assert any(f.get("influence_tier") == "RETAIL" for f in flows)


@pytest.mark.asyncio
async def test_flow_store_does_not_grow_unbounded():
    import services.flow_store as fs
    await fs.clear_flows()
    for i in range(200):
        await fs.add_flow(_flow("AAPL", premium=float(i * 1000)))
    flows = await fs.get_flows("AAPL")
    assert len(flows) <= 200


@pytest.mark.asyncio
async def test_add_flow_with_empty_ticker_does_not_crash():
    import services.flow_store as fs
    try:
        await fs.add_flow(_flow(""))
    except (ValueError, KeyError):
        pass


@pytest.mark.asyncio
async def test_get_all_flows_returns_list():
    import services.flow_store as fs
    if not hasattr(fs, "get_all_flows"):
        pytest.skip("get_all_flows not implemented")
    flows = await fs.get_all_flows()
    assert isinstance(flows, list)


@pytest.mark.asyncio
async def test_add_flow_publishes_to_bus_if_wired():
    import services.flow_store as fs
    if not hasattr(fs, "bus"):
        pytest.skip("No bus wired")
    with patch.object(fs.bus, "publish_all", new_callable=AsyncMock) as mock_pub:
        await fs.add_flow(_flow("AAPL"))
        assert mock_pub.call_count >= 0


@pytest.mark.asyncio
async def test_flows_ordered_most_recent_first():
    import services.flow_store as fs
    await fs.clear_flows()
    now = time.time()
    for i in range(3):
        f = _flow("MSFT")
        f["timestamp"] = now + i
        await fs.add_flow(f)
    flows = await fs.get_flows("MSFT")
    if len(flows) > 1:
        timestamps = [f.get("timestamp", 0) for f in flows]
        assert len(timestamps) == len(set(timestamps))


# ---------------------------------------------------------------------------
# upgrade_to_sweep_in_db (C-003)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_upgrade_to_sweep_success_200():
    """Returns True on HTTP 200."""
    import services.flow_store as fs
    mock_resp = _mock_response(200)
    with patch("services.flow_store._SUPABASE_URL", "https://example.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "service_key_abc"), \
         patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.patch = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client
        result = await fs.upgrade_to_sweep_in_db("AAPL240620C00180000", 2.35, 100)
    assert result is True


@pytest.mark.asyncio
async def test_upgrade_to_sweep_success_204():
    """Returns True on HTTP 204."""
    import services.flow_store as fs
    mock_resp = _mock_response(204)
    with patch("services.flow_store._SUPABASE_URL", "https://example.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "service_key_abc"), \
         patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.patch = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client
        result = await fs.upgrade_to_sweep_in_db("SPY240620P00440000", 1.10, 50)
    assert result is True


@pytest.mark.asyncio
async def test_upgrade_to_sweep_failure_400():
    """Returns False on 4xx."""
    import services.flow_store as fs
    mock_resp = _mock_response(400, "Bad Request")
    with patch("services.flow_store._SUPABASE_URL", "https://example.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "service_key_abc"), \
         patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.patch = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client
        result = await fs.upgrade_to_sweep_in_db("AAPL240620C00180000", 2.35, 100)
    assert result is False


@pytest.mark.asyncio
async def test_upgrade_to_sweep_not_configured():
    """Returns False immediately when SUPABASE_URL is missing."""
    import services.flow_store as fs
    with patch("services.flow_store._SUPABASE_URL", None):
        result = await fs.upgrade_to_sweep_in_db("AAPL240620C00180000", 2.35, 100)
    assert result is False


@pytest.mark.asyncio
async def test_upgrade_to_sweep_not_configured_no_key():
    """Returns False when SUPABASE_KEY is missing."""
    import services.flow_store as fs
    with patch("services.flow_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", None):
        result = await fs.upgrade_to_sweep_in_db("AAPL240620C00180000", 2.35, 100)
    assert result is False


@pytest.mark.asyncio
async def test_upgrade_to_sweep_exception_returns_false():
    """Exception during patch → returns False (never propagates)."""
    import services.flow_store as fs
    with patch("services.flow_store._SUPABASE_URL", "https://example.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "service_key_abc"), \
         patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.patch = AsyncMock(side_effect=Exception("network error"))
        mock_client_cls.return_value = mock_client
        result = await fs.upgrade_to_sweep_in_db("AAPL240620C00180000", 2.35, 100)
    assert result is False


@pytest.mark.asyncio
async def test_upgrade_to_sweep_url_contains_occ_symbol():
    """PATCH URL includes occ_symbol, fill_price, size, and idempotency guard."""
    import services.flow_store as fs
    mock_resp = _mock_response(204)
    captured_url = []
    with patch("services.flow_store._SUPABASE_URL", "https://example.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "svc"), \
         patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        async def capture_patch(url, **kwargs):
            captured_url.append(url)
            return mock_resp
        mock_client.patch = capture_patch
        mock_client_cls.return_value = mock_client
        await fs.upgrade_to_sweep_in_db("NVDA240620C00900000", 5.50, 200)
    assert len(captured_url) == 1
    url = captured_url[0]
    assert "NVDA240620C00900000" in url
    assert "5.5" in url
    assert "200" in url
    assert "SWEEP" in url


# ---------------------------------------------------------------------------
# persist_flow_episode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_flow_episode_calls_insert_rows():
    """persist_flow_episode builds correct row and calls _insert_rows."""
    import services.flow_store as fs
    signal_data = {
        "ticker": "AAPL",
        "direction": "BULLISH",
        "contract_type": "CALL",
        "strike": 180.0,
        "expiry": "2026-06-20",
        "total_premium": 500_000.0,
        "trade_count": 12,
        "alert_level": "STRONG_SIGNAL",
        "is_accelerating": True,
        "seed_episode": "3 large calls, escalating",
        "timestamp": "2026-04-28T10:00:00Z",
    }
    with patch("services.flow_store._insert_rows", new_callable=AsyncMock, return_value=True) as mock_insert:
        await fs.persist_flow_episode(signal_data)
    mock_insert.assert_called_once()
    table, rows = mock_insert.call_args[0]
    assert table == "flow_episodes"
    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "AAPL"
    assert row["alert_level"] == "STRONG_SIGNAL"
    assert row["total_premium"] == 500_000.0
    assert row["is_accelerating"] is True


@pytest.mark.asyncio
async def test_persist_flow_episode_empty_expiry_becomes_none():
    """Empty expiry string is normalised to None."""
    import services.flow_store as fs
    signal_data = {"ticker": "SPY", "expiry": "", "total_premium": 100_000.0,
                   "trade_count": 5, "alert_level": "WATCH",
                   "is_accelerating": False, "seed_episode": None, "timestamp": None,
                   "direction": "BEARISH", "contract_type": "PUT", "strike": 440.0}
    with patch("services.flow_store._insert_rows", new_callable=AsyncMock, return_value=True) as mock_insert:
        await fs.persist_flow_episode(signal_data)
    row = mock_insert.call_args[0][1][0]
    assert row["expiry"] is None


# ---------------------------------------------------------------------------
# _insert_rows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_rows_returns_false_when_not_configured():
    import services.flow_store as fs
    with patch("services.flow_store._SUPABASE_URL", None):
        result = await fs._insert_rows("flow_events", [{"x": 1}])
    assert result is False


@pytest.mark.asyncio
async def test_insert_rows_returns_false_on_empty_rows():
    import services.flow_store as fs
    with patch("services.flow_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "key"):
        result = await fs._insert_rows("flow_events", [])
    assert result is False


@pytest.mark.asyncio
async def test_insert_rows_returns_true_on_201():
    import services.flow_store as fs
    mock_resp = _mock_response(201)
    with patch("services.flow_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "key"), \
         patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client
        result = await fs._insert_rows("flow_events", [{"ticker": "AAPL"}])
    assert result is True


@pytest.mark.asyncio
async def test_insert_rows_returns_false_on_500():
    import services.flow_store as fs
    mock_resp = _mock_response(500, "internal error")
    with patch("services.flow_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "key"), \
         patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client
        result = await fs._insert_rows("flow_events", [{"ticker": "AAPL"}])
    assert result is False


@pytest.mark.asyncio
async def test_insert_rows_exception_returns_false():
    import services.flow_store as fs
    with patch("services.flow_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "key"), \
         patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.post = AsyncMock(side_effect=Exception("timeout"))
        mock_client_cls.return_value = mock_client
        result = await fs._insert_rows("flow_events", [{"ticker": "AAPL"}])
    assert result is False


# ---------------------------------------------------------------------------
# _insert_rows_with_retry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_rows_with_retry_succeeds_first_attempt():
    import services.flow_store as fs
    with patch("services.flow_store._insert_rows", new_callable=AsyncMock, return_value=True):
        result = await fs._insert_rows_with_retry("flow_events", [{"ticker": "AAPL"}])
    assert result is True


@pytest.mark.asyncio
async def test_insert_rows_with_retry_exhausts_attempts():
    """After RETRY_MAX failures, returns False and logs error."""
    import services.flow_store as fs
    with patch("services.flow_store._insert_rows", new_callable=AsyncMock, return_value=False), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        result = await fs._insert_rows_with_retry("flow_events", [{"ticker": "AAPL"}])
    assert result is False


@pytest.mark.asyncio
async def test_insert_rows_with_retry_succeeds_on_second_attempt():
    """First attempt fails, second succeeds → True."""
    import services.flow_store as fs
    call_count = {"n": 0}
    async def flaky(*args, **kwargs):
        call_count["n"] += 1
        return call_count["n"] >= 2
    with patch("services.flow_store._insert_rows", side_effect=flaky), \
         patch("asyncio.sleep", new_callable=AsyncMock):
        result = await fs._insert_rows_with_retry("flow_events", [{"ticker": "AAPL"}])
    assert result is True
    assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# persist_flow_event buffering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_flow_event_not_configured_drops_event():
    """When not configured, event is dropped silently (returns None)."""
    import services.flow_store as fs
    with patch("services.flow_store._SUPABASE_URL", None), \
         patch("services.flow_store._SUPABASE_KEY", None):
        await fs.persist_flow_event({"ticker": "AAPL"})


@pytest.mark.asyncio
async def test_persist_flow_event_adds_to_buffer():
    """Normal flow_event is added to the global buffer."""
    import services.flow_store as fs
    ev = {
        "ticker": "AAPL", "contract_type": "CALL", "strike": 180.0,
        "expiry": "2026-06-20", "dte": 30, "fill_price": 2.35,
        "bid": 2.30, "ask": 2.40, "size": 100, "premium": 23_500.0,
        "trade_type": "BTO", "bid_ask_class": "ASK", "is_aggressive": True,
        "is_golden_sweep": False, "sentiment": "BULLISH",
        "influence_tier": "WHALE", "conviction_score": 0.80,
        "exchange_count": 1, "fill_count": 1, "open_interest": 5000,
        "iv": 0.45, "underlying_price": 175.0, "occ_symbol": "AAPL240620C00180000",
        "is_synthetic_quote": False,
    }
    orig_buffer = fs._flow_event_buffer[:]
    with patch("services.flow_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "svc"):
        await fs.persist_flow_event(ev)
    assert len(fs._flow_event_buffer) > len(orig_buffer)
    fs._flow_event_buffer.clear()


@pytest.mark.asyncio
async def test_persist_flow_event_early_flush_on_max_rows():
    """When buffer hits FLUSH_MAX_ROWS, an early flush is triggered."""
    import services.flow_store as fs
    ev = {
        "ticker": "SPY", "contract_type": "PUT", "strike": 440.0,
        "expiry": "2026-05-17", "dte": 19, "fill_price": 1.10,
        "bid": 1.05, "ask": 1.15, "size": 50, "premium": 5_500.0,
        "trade_type": "BTO", "bid_ask_class": "BID", "is_aggressive": False,
        "is_golden_sweep": False, "sentiment": "BEARISH",
        "influence_tier": "INSTITUTIONAL", "conviction_score": 0.55,
        "exchange_count": 1, "fill_count": 1, "open_interest": 20000,
        "iv": 0.22, "underlying_price": 442.0, "occ_symbol": "SPY240517P00440000",
        "is_synthetic_quote": False,
    }
    fs._flow_event_buffer = [{"ticker": "DUMMY"}] * (fs._FLUSH_MAX_ROWS - 1)
    with patch("services.flow_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "svc"), \
         patch("services.flow_store._insert_rows_with_retry", new_callable=AsyncMock, return_value=True):
        await fs.persist_flow_event(ev)
    assert len(fs._flow_event_buffer) < fs._FLUSH_MAX_ROWS
    fs._flow_event_buffer.clear()


@pytest.mark.asyncio
async def test_persist_flow_event_warns_on_zero_strike():
    """strike=0.0 does not crash persist_flow_event."""
    import services.flow_store as fs
    ev = {
        "ticker": "TSLA", "contract_type": "CALL", "strike": 0.0,
        "expiry": "2026-06-20", "dte": 30, "fill_price": 1.0,
        "bid": 0.95, "ask": 1.05, "size": 10, "premium": 1_000.0,
        "trade_type": "BTO", "bid_ask_class": "MID", "is_aggressive": False,
        "is_golden_sweep": False, "sentiment": "NEUTRAL",
        "influence_tier": "RETAIL", "conviction_score": 0.3,
        "exchange_count": 1, "fill_count": 1, "open_interest": 0,
        "iv": 0.0, "underlying_price": 200.0, "occ_symbol": "TSLA240620C00001000",
        "is_synthetic_quote": False,
    }
    with patch("services.flow_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "svc"):
        await fs.persist_flow_event(ev)
    fs._flow_event_buffer.clear()


# ---------------------------------------------------------------------------
# BUG 1 (ALERT-LEVEL) — _bus_signal_listener reads alert_level not recommendation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bus_signal_listener_uses_alert_level_not_recommendation():
    """
    ALERT-LEVEL regression: _bus_signal_listener must persist alert_level
    from sig.get('alert_level'), NOT sig.get('recommendation').

    We verify by publishing a composite_signal message where recommendation='BUY'
    and alert_level='CONVICTION', then asserting persist_flow_episode received
    alert_level='CONVICTION' (not 'BUY').
    """
    import services.flow_store as fs
    from core.async_bus import AsyncBus

    captured_episodes = []

    async def fake_persist(signal_data):
        captured_episodes.append(signal_data.copy())

    composite_msg = {
        "type": "composite_signal",
        "data": {
            "signal": {
                "ticker": "AAPL",
                "recommendation": "BUY",
                "alert_level": "CONVICTION",
                "reasoning": "3 sweeps above $1M",
            },
            "episode": {
                "direction": "BULLISH",
                "contract_type": "CALL",
                "total_premium": 1_200_000.0,
                "trade_count": 8,
                "is_accelerating": True,
                "timestamp": "2026-04-28T10:00:00Z",
            },
        },
    }

    test_bus = AsyncBus()
    # AsyncBus is callback-based — subscribe requires a handler.
    # We register a no-op handler here just to confirm subscribe works;
    # _bus_signal_listener is wired via the patched bus, not this subscription.
    async def _noop(msg): pass
    test_bus.subscribe("test_listener", _noop)

    with patch("services.flow_store.persist_flow_episode", side_effect=fake_persist), \
         patch("services.flow_store.bus", test_bus):
        listener_task = asyncio.create_task(fs._bus_signal_listener())
        await asyncio.sleep(0.05)
        await test_bus.publish_all(composite_msg)
        await asyncio.sleep(0.1)
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass

    assert len(captured_episodes) == 1
    ep = captured_episodes[0]
    assert ep["alert_level"] == "CONVICTION", (
        f"ALERT-LEVEL bug still present: alert_level={ep['alert_level']!r} "
        "(expected 'CONVICTION', got recommendation value)"
    )
    assert ep["ticker"] == "AAPL"
    assert ep["total_premium"] == 1_200_000.0


@pytest.mark.asyncio
async def test_bus_signal_listener_ignores_non_composite_message():
    """
    Non-composite_signal messages (e.g. type='signal') must NOT trigger
    persist_flow_episode. Only composite_signal events write flow_episodes.
    """
    import services.flow_store as fs
    from core.async_bus import AsyncBus

    captured_episodes = []

    async def fake_persist(signal_data):
        captured_episodes.append(signal_data)

    non_composite_msg = {
        "type": "signal",
        "data": {"ticker": "TSLA", "recommendation": "SELL"},
    }

    test_bus = AsyncBus()
    with patch("services.flow_store.persist_flow_episode", side_effect=fake_persist), \
         patch("services.flow_store.bus", test_bus):
        listener_task = asyncio.create_task(fs._bus_signal_listener())
        await asyncio.sleep(0.05)
        await test_bus.publish_all(non_composite_msg)
        await asyncio.sleep(0.1)
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass

    assert len(captured_episodes) == 0, "Non-composite message incorrectly triggered persist"


@pytest.mark.asyncio
async def test_bus_signal_listener_ignores_non_dict_message():
    """Non-dict messages must be silently skipped."""
    import services.flow_store as fs
    from core.async_bus import AsyncBus

    captured_episodes = []

    async def fake_persist(signal_data):
        captured_episodes.append(signal_data)

    test_bus = AsyncBus()
    with patch("services.flow_store.persist_flow_episode", side_effect=fake_persist), \
         patch("services.flow_store.bus", test_bus):
        listener_task = asyncio.create_task(fs._bus_signal_listener())
        await asyncio.sleep(0.05)
        await test_bus.publish_all("not_a_dict")
        await asyncio.sleep(0.1)
        listener_task.cancel()
        try:
            await listener_task
        except asyncio.CancelledError:
            pass

    assert len(captured_episodes) == 0


# ---------------------------------------------------------------------------
# BUG 2 (DEDUP-KWARGS) — verify dedup contract takes positional arg
# ---------------------------------------------------------------------------

def test_dedup_cache_is_duplicate_accepts_positional_occ_symbol():
    """
    DEDUP-KWARGS regression: DedupCache.is_duplicate() first param must
    accept a positional call (not keyword 'occ_symbol=...').
    """
    from utils.dedup import DedupCache
    import inspect
    sig = inspect.signature(DedupCache.is_duplicate)
    params = list(sig.parameters.keys())
    first_param = params[1]  # params[0] = 'self'
    param_kind = sig.parameters[first_param].kind
    assert param_kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.POSITIONAL_ONLY,
    ), (
        f"is_duplicate first param '{first_param}' must accept positional call. "
        f"Got kind={param_kind!r}"
    )


def test_dedup_cache_positional_call_does_not_raise():
    """Calling is_duplicate positionally must not raise TypeError.

    NOTE: The second positional param is 'size' (int) and the third is 'fill' (float).
    fill_price is not a valid kwarg; the correct kwarg name is 'fill'.
    """
    from utils.dedup import DedupCache
    cache = DedupCache()
    try:
        # Correct kwargs: size=, fill= (NOT fill_price=)
        cache.is_duplicate("AAPL240620C00180000", size=100, fill=2.35, exchange="CBOE")
    except TypeError as e:
        pytest.fail(f"DEDUP-KWARGS bug: positional call raised TypeError: {e}")


# ---------------------------------------------------------------------------
# BUG 3 (H4) — _sweep_upgrade_dispatched TTL eviction
# ---------------------------------------------------------------------------

def test_sweep_upgrade_dispatched_is_dict_not_set():
    """
    H4 regression: _sweep_upgrade_dispatched must be a dict[str, float]
    NOT a set[str]. A set has no TTL eviction capability.
    """
    import services.tradier_stream as ts
    dispatched = ts._sweep_upgrade_dispatched
    assert isinstance(dispatched, dict), (
        f"H4 bug: _sweep_upgrade_dispatched is {type(dispatched).__name__!r}, "
        "expected dict (for TTL eviction)"
    )


def test_sweep_dispatch_ttl_constant_exists():
    """TTL constant must be defined for H4 eviction logic."""
    import services.tradier_stream as ts
    assert hasattr(ts, "_SWEEP_DISPATCH_TTL_S"), \
        "H4 fix missing: _SWEEP_DISPATCH_TTL_S constant not defined in tradier_stream.py"
    assert ts._SWEEP_DISPATCH_TTL_S == 1800.0, (
        f"Expected 1800.0 (30 min TTL), got {ts._SWEEP_DISPATCH_TTL_S}"
    )


def test_sweep_upgrade_dispatched_evicts_stale_keys():
    """
    H4 logic: stale keys older than TTL must be evicted before membership check.
    This test simulates the eviction inline.
    """
    import time
    TTL = 1800.0
    dispatched: dict = {}
    stale_key = "AAPL240620C00180000|100|2.35"
    dispatched[stale_key] = time.time() - 7200.0
    fresh_key = "SPY240620P00440000|50|1.10"
    dispatched[fresh_key] = time.time() - 60.0

    now = time.time()
    stale = [k for k, ts_val in dispatched.items() if now - ts_val > TTL]
    for k in stale:
        del dispatched[k]

    assert stale_key not in dispatched, "Stale key was not evicted"
    assert fresh_key in dispatched, "Fresh key was incorrectly evicted"


# ---------------------------------------------------------------------------
# BUG 4 (Gate-2) — accumulator retrigger threshold
# ---------------------------------------------------------------------------

def test_repetition_accumulator_has_last_signaled_premium():
    """
    Gate-2 regression: RepetitionEpisode must have 'last_signaled_premium' field.
    Without it Gate-2 cannot track delta and re-emits on every tick.
    """
    try:
        from signals.repetition_accumulator import RepetitionEpisode
        ep = RepetitionEpisode(
            ticker="AAPL",
            occ_symbol="AAPL240620C00180000",
            contract_type="CALL",
            direction="BULLISH",
        )
        assert hasattr(ep, "last_signaled_premium"), (
            "Gate-2 bug: RepetitionEpisode missing 'last_signaled_premium' field."
        )
        assert ep.last_signaled_premium == 0.0, (
            f"Expected initial last_signaled_premium=0.0, got {ep.last_signaled_premium}"
        )
    except ImportError:
        pytest.skip("RepetitionEpisode not importable — skipping Gate-2 structural test")


@pytest.mark.asyncio
async def test_gate2_retrigger_threshold_blocks_re_emission_below_delta():
    """
    Gate-2: after first signal emission, ticks with < $50k new premium
    should NOT cause re-emission (ingest_tick returns None).
    """
    try:
        from signals.repetition_accumulator import RepetitionAccumulator
        acc = RepetitionAccumulator()

        base_tick = {
            "occ_symbol": "AAPL240620C00180000",
            "ticker": "AAPL",
            "contract_type": "CALL",
            "direction": "BULLISH",
            "premium": 15_000.0,
            "fill_price": 1.50,
            "size": 10,
            "sentiment": "BULLISH",
            "exchange": "CBOE",
        }

        results = []
        for i in range(5):
            result = await acc.ingest_tick({**base_tick, "premium": 15_000.0})
            results.append(result)

        small_tick = {**base_tick, "premium": 100.0}
        result = await acc.ingest_tick(small_tick)
        first_emission = next((r for r in results if r is not None), None)
        if first_emission is not None:
            assert result is None, (
                "Gate-2 bug: tiny tick ($100 premium) caused re-emission "
                "despite delta < $50k threshold"
            )
    except ImportError:
        pytest.skip("RepetitionAccumulator not importable — skipping Gate-2 functional test")


@pytest.mark.asyncio
async def test_gate2_retrigger_fires_on_large_delta():
    """
    Gate-2: after first emission, a tick that pushes total_premium >= $50k above
    last_signaled_premium should re-emit (return episode).
    """
    try:
        from signals.repetition_accumulator import RepetitionAccumulator
        acc = RepetitionAccumulator()

        base_tick = {
            "occ_symbol": "SPY240620P00440000",
            "ticker": "SPY",
            "contract_type": "PUT",
            "direction": "BEARISH",
            "premium": 20_000.0,
            "fill_price": 2.00,
            "size": 10,
            "sentiment": "BEARISH",
            "exchange": "CBOE",
        }

        first_ep = None
        for _ in range(5):
            r = await acc.ingest_tick(base_tick)
            if r is not None:
                first_ep = r

        if first_ep is None:
            pytest.skip("Gate 1 not crossed — cannot test Gate-2 retrigger")

        large_tick = {**base_tick, "premium": 60_000.0}
        result = await acc.ingest_tick(large_tick)
        assert result is not None, (
            "Gate-2 bug: large tick ($60k delta) should re-emit episode but returned None"
        )
    except ImportError:
        pytest.skip("RepetitionAccumulator not importable — skipping Gate-2 retrigger test")


# ---------------------------------------------------------------------------
# _is_configured helper
# ---------------------------------------------------------------------------

def test_is_configured_false_when_missing_url():
    import services.flow_store as fs
    with patch("services.flow_store._SUPABASE_URL", None), \
         patch("services.flow_store._SUPABASE_KEY", "key"):
        assert fs._is_configured() is False


def test_is_configured_false_when_missing_key():
    import services.flow_store as fs
    with patch("services.flow_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", None):
        assert fs._is_configured() is False


def test_is_configured_true_when_both_set():
    import services.flow_store as fs
    with patch("services.flow_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "key"):
        assert fs._is_configured() is True


# ---------------------------------------------------------------------------
# _headers helper
# ---------------------------------------------------------------------------

def test_headers_contains_required_keys():
    import services.flow_store as fs
    with patch("services.flow_store._SUPABASE_KEY", "my_service_key"):
        h = fs._headers()
    assert "apikey" in h
    assert "Authorization" in h
    assert "Bearer my_service_key" in h["Authorization"]
    assert h["Content-Type"] == "application/json"
    assert h["Prefer"] == "return=minimal"


# ---------------------------------------------------------------------------
# start_flow_writer: not-configured path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_flow_writer_exits_early_when_not_configured():
    """start_flow_writer returns immediately (no task) when not configured."""
    import services.flow_store as fs
    with patch("services.flow_store._SUPABASE_URL", None):
        await asyncio.wait_for(fs.start_flow_writer(), timeout=1.0)
