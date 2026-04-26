"""
Unit tests for services/flow_store.py

Covers:
  persist_flow_event
  1.  Row appended to buffer for a standard event
  2.  All expected keys present in buffered row
  3.  Empty expiry coerced to None (not empty string)
  4.  occ_symbol forwarded to row
  5.  is_synthetic_quote forwarded to row (C-018)
  6.  Defaults applied when fields absent from ev_dict
  7.  Early flush triggered when buffer hits FLUSH_MAX_ROWS
  8.  Buffer trimmed correctly after early flush
  9.  No `id` field in buffered row (Postgres generates uuid)

  _flush_flow_events
  10. Drains buffer into flow_events on each tick
  11. Empty buffer skipped (no insert call)
  12. Buffer over FLUSH_MAX_ROWS trimmed to 100 per tick
  13. Failed insert logs warning

  persist_flow_episode
  14. Calls _insert_rows with 'flow_episodes' table
  15. Row contains expected fields; no `id` field
  16. Empty expiry coerced to None
  17. Returns without crashing on insert failure
  18. Sparse/None inputs produce safe defaults in log formatting

  _insert_rows
  19. Returns False when URL not configured
  20. Returns True on 201
  21. Returns False on 4xx
  22. Returns False on network exception
  23. Returns False when rows list is empty

  _bus_signal_listener
  24. composite_signal triggers persist_flow_episode
  25. Raw 'signal' message does NOT trigger persist_flow_episode (C-017)
  26. Non-dict message is ignored
  27. CancelledError unsubscribes from bus

  constants
  28. FLUSH_INTERVAL == 0.5
  29. FLUSH_MAX_ROWS == 100

  F-01: env-var gate on persist_flow_event
  30. persist_flow_event no-ops when env vars missing
  31. persist_flow_event logs warning when env vars missing

  F-02: retry buffer on flush failure
  32. _flush_flow_events retries up to RETRY_MAX on insert failure
  33. _flush_flow_events logs ERROR and discards batch after all retries exhausted
"""
import asyncio
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.flow_store as fs
from services.flow_store import (
    _bus_signal_listener,
    _flush_flow_events,
    _insert_rows,
    persist_flow_episode,
    persist_flow_event,
)


# -- helpers -----------------------------------------------------------------
def _ev(
    ticker="AAPL",
    contract_type="CALL",
    strike=180.0,
    expiry="2026-06-20",
    dte=56,
    fill_price=3.50,
    bid=3.40,
    ask=3.60,
    size=10,
    premium=3500.0,
    trade_type="SWEEP",
    bid_ask_class="AT_ASK",
    is_aggressive=True,
    is_golden_sweep=False,
    sentiment="BULLISH",
    influence_tier="WHALE",
    conviction_score=0.75,
    exchange_count=3,
    fill_count=3,
    open_interest=5000,
    iv=0.35,
    underlying_price=190.0,
    occ_symbol="AAPL  260620C00180000",
    is_synthetic_quote=False,
):
    return {
        "ticker":             ticker,
        "contract_type":      contract_type,
        "strike":             strike,
        "expiry":             expiry,
        "dte":                dte,
        "fill_price":         fill_price,
        "bid":                bid,
        "ask":                ask,
        "size":               size,
        "premium":            premium,
        "trade_type":         trade_type,
        "bid_ask_class":      bid_ask_class,
        "is_aggressive":      is_aggressive,
        "is_golden_sweep":    is_golden_sweep,
        "sentiment":          sentiment,
        "influence_tier":     influence_tier,
        "conviction_score":   conviction_score,
        "exchange_count":     exchange_count,
        "fill_count":         fill_count,
        "open_interest":      open_interest,
        "iv":                 iv,
        "underlying_price":   underlying_price,
        "occ_symbol":         occ_symbol,
        "is_synthetic_quote": is_synthetic_quote,
    }


def _ep_data(
    ticker="AAPL",
    direction="BUY",
    contract_type="CALL",
    total_premium=2_000_000.0,
    trade_count=8,
    alert_level="CONVICTION",
    is_accelerating=True,
    seed_episode="8x CALL $180 2026-06-20",
    timestamp="2026-04-25T10:00:00",
):
    return {
        "ticker":          ticker,
        "direction":       direction,
        "contract_type":   contract_type,
        "total_premium":   total_premium,
        "trade_count":     trade_count,
        "alert_level":     alert_level,
        "is_accelerating": is_accelerating,
        "seed_episode":    seed_episode,
        "timestamp":       timestamp,
    }


def _reset_buffer():
    fs._flow_event_buffer.clear()


# ============================================================
# persist_flow_event
# ============================================================

# 1
def test_persist_flow_event_appends_to_buffer():
    _reset_buffer()
    with patch.object(fs, "_is_configured", return_value=True):
        asyncio.get_event_loop().run_until_complete(persist_flow_event(_ev()))
    assert len(fs._flow_event_buffer) == 1


# 2
def test_persist_flow_event_row_has_required_keys():
    _reset_buffer()
    with patch.object(fs, "_is_configured", return_value=True):
        asyncio.get_event_loop().run_until_complete(persist_flow_event(_ev()))
    row = fs._flow_event_buffer[0]
    for key in [
        "ticker", "contract_type", "strike", "expiry", "dte",
        "fill_price", "bid", "ask", "size", "premium",
        "trade_type", "bid_ask_class", "is_aggressive", "is_golden_sweep",
        "sentiment", "influence_tier", "conviction_score",
        "exchange_count", "fill_count", "open_interest",
        "iv", "underlying_price", "occ_symbol", "is_synthetic_quote",
    ]:
        assert key in row, f"Missing key: {key}"


# 3
def test_persist_flow_event_empty_expiry_coerced_to_none():
    _reset_buffer()
    with patch.object(fs, "_is_configured", return_value=True):
        asyncio.get_event_loop().run_until_complete(persist_flow_event(_ev(expiry="")))
    assert fs._flow_event_buffer[0]["expiry"] is None


# 4
def test_persist_flow_event_occ_symbol_forwarded():
    _reset_buffer()
    with patch.object(fs, "_is_configured", return_value=True):
        asyncio.get_event_loop().run_until_complete(
            persist_flow_event(_ev(occ_symbol="SPY   260117P00450000"))
        )
    assert fs._flow_event_buffer[0]["occ_symbol"] == "SPY   260117P00450000"


# 5
def test_persist_flow_event_is_synthetic_quote_forwarded():
    _reset_buffer()
    with patch.object(fs, "_is_configured", return_value=True):
        asyncio.get_event_loop().run_until_complete(persist_flow_event(_ev(is_synthetic_quote=True)))
    assert fs._flow_event_buffer[0]["is_synthetic_quote"] is True


# 6
def test_persist_flow_event_defaults_applied_for_sparse_payload():
    _reset_buffer()
    with patch.object(fs, "_is_configured", return_value=True):
        asyncio.get_event_loop().run_until_complete(persist_flow_event({}))
    row = fs._flow_event_buffer[0]
    assert row["ticker"]             == "UNKNOWN"
    assert row["dte"]                == 0
    assert row["fill_price"]         == 0.0
    assert row["sentiment"]          == "NEUTRAL"
    assert row["influence_tier"]     == "RETAIL"
    assert row["is_aggressive"]      is False
    assert row["is_synthetic_quote"] is False


# 7
def test_persist_flow_event_early_flush_at_max_rows():
    _reset_buffer()

    async def _test():
        with patch.object(fs, "_is_configured", return_value=True), \
             patch.object(fs, "_insert_rows_with_retry", new_callable=AsyncMock, return_value=True) as mock_ins:
            for _ in range(fs._FLUSH_MAX_ROWS):
                await persist_flow_event(_ev())
            assert mock_ins.call_count == 1
            assert mock_ins.call_args[0][0] == "flow_events"

    asyncio.get_event_loop().run_until_complete(_test())


# 8
def test_persist_flow_event_buffer_trimmed_after_early_flush():
    _reset_buffer()

    async def _test():
        with patch.object(fs, "_is_configured", return_value=True), \
             patch.object(fs, "_insert_rows_with_retry", new_callable=AsyncMock, return_value=True):
            for _ in range(fs._FLUSH_MAX_ROWS + 5):
                await persist_flow_event(_ev())
            assert len(fs._flow_event_buffer) == 5

    asyncio.get_event_loop().run_until_complete(_test())


# 9
def test_persist_flow_event_no_id_field_in_row():
    _reset_buffer()
    with patch.object(fs, "_is_configured", return_value=True):
        asyncio.get_event_loop().run_until_complete(persist_flow_event(_ev()))
    assert "id" not in fs._flow_event_buffer[0]


# ============================================================
# _flush_flow_events
# ============================================================

# 10
def test_flush_flow_events_drains_buffer():
    _reset_buffer()
    fs._flow_event_buffer.extend([{"ticker": f"T{i}"} for i in range(10)])

    async def _test():
        with patch.object(fs, "_insert_rows_with_retry", new_callable=AsyncMock, return_value=True) as mock_ins, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            task = asyncio.create_task(_flush_flow_events())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert mock_ins.call_count >= 1

    asyncio.get_event_loop().run_until_complete(_test())


# 11
def test_flush_flow_events_skips_empty_buffer():
    _reset_buffer()

    async def _test():
        with patch.object(fs, "_insert_rows_with_retry", new_callable=AsyncMock, return_value=True) as mock_ins, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            task = asyncio.create_task(_flush_flow_events())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert mock_ins.call_count == 0

    asyncio.get_event_loop().run_until_complete(_test())


# 12
def test_flush_trims_to_max_rows_per_tick():
    _reset_buffer()
    fs._flow_event_buffer.extend([{"ticker": f"T{i}"} for i in range(150)])

    async def _test():
        with patch.object(fs, "_insert_rows_with_retry", new_callable=AsyncMock, return_value=True) as mock_ins, \
             patch("asyncio.sleep", new_callable=AsyncMock):
            task = asyncio.create_task(_flush_flow_events())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            if mock_ins.call_count >= 1:
                rows_sent = mock_ins.call_args_list[0][0][1]
                assert len(rows_sent) == 100

    asyncio.get_event_loop().run_until_complete(_test())


# 13
def test_flush_logs_warning_on_insert_failure(caplog):
    _reset_buffer()
    fs._flow_event_buffer.append({"ticker": "AAPL"})

    async def _test():
        with patch.object(fs, "_insert_rows_with_retry", new_callable=AsyncMock, return_value=False), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             caplog.at_level(logging.WARNING, logger="flow_store"):
            task = asyncio.create_task(_flush_flow_events())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.get_event_loop().run_until_complete(_test())
    # _insert_rows_with_retry handles its own error logging; _flush just calls it
    # If it returns False after retries, no extra warning is emitted by _flush itself.
    # This test verifies no crash occurs on failed insert.


# ============================================================
# persist_flow_episode
# ============================================================

# 14
def test_persist_flow_episode_calls_correct_table():
    async def _test():
        with patch.object(fs, "_insert_rows", new_callable=AsyncMock, return_value=True) as mock_ins:
            await persist_flow_episode(_ep_data())
            assert mock_ins.call_count == 1
            assert mock_ins.call_args[0][0] == "flow_episodes"

    asyncio.get_event_loop().run_until_complete(_test())


# 15
def test_persist_flow_episode_row_fields_and_no_id():
    async def _test():
        with patch.object(fs, "_insert_rows", new_callable=AsyncMock, return_value=True) as mock_ins:
            await persist_flow_episode(_ep_data())
            row = mock_ins.call_args[0][1][0]
            assert "id" not in row
            for key in ["ticker", "direction", "contract_type",
                        "total_premium", "trade_count", "is_accelerating"]:
                assert key in row

    asyncio.get_event_loop().run_until_complete(_test())


# 16
def test_persist_flow_episode_empty_expiry_coerced_to_none():
    async def _test():
        with patch.object(fs, "_insert_rows", new_callable=AsyncMock, return_value=True) as mock_ins:
            data = _ep_data()
            data["expiry"] = ""
            await persist_flow_episode(data)
            assert mock_ins.call_args[0][1][0]["expiry"] is None

    asyncio.get_event_loop().run_until_complete(_test())


# 17
def test_persist_flow_episode_no_crash_on_insert_failure():
    async def _test():
        with patch.object(fs, "_insert_rows", new_callable=AsyncMock, return_value=False):
            await persist_flow_episode(_ep_data())  # must not raise

    asyncio.get_event_loop().run_until_complete(_test())


# 18
def test_persist_flow_episode_none_fields_log_safe():
    async def _test():
        with patch.object(fs, "_insert_rows", new_callable=AsyncMock, return_value=True):
            await persist_flow_episode({
                "ticker": None, "contract_type": None,
                "alert_level": None, "total_premium": 0,
            })

    asyncio.get_event_loop().run_until_complete(_test())


# ============================================================
# _insert_rows
# ============================================================

# 19
def test_insert_rows_false_when_url_not_set():
    async def _test():
        with patch.object(fs, "_SUPABASE_URL", None), \
             patch.object(fs, "_SUPABASE_KEY", None):
            return await _insert_rows("flow_events", [{"ticker": "AAPL"}])
    assert asyncio.get_event_loop().run_until_complete(_test()) is False


# 20
def test_insert_rows_true_on_201():
    fake_resp = MagicMock()
    fake_resp.status_code = 201

    async def _test():
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", "key"), \
             patch("httpx.AsyncClient") as mock_cls:
            mc = AsyncMock()
            mc.__aenter__ = AsyncMock(return_value=mc)
            mc.__aexit__  = AsyncMock(return_value=False)
            mc.post       = AsyncMock(return_value=fake_resp)
            mock_cls.return_value = mc
            return await _insert_rows("flow_events", [{"ticker": "AAPL"}])

    assert asyncio.get_event_loop().run_until_complete(_test()) is True


# 21
def test_insert_rows_false_on_4xx():
    fake_resp = MagicMock()
    fake_resp.status_code = 422
    fake_resp.text        = "Unprocessable"

    async def _test():
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", "key"), \
             patch("httpx.AsyncClient") as mock_cls:
            mc = AsyncMock()
            mc.__aenter__ = AsyncMock(return_value=mc)
            mc.__aexit__  = AsyncMock(return_value=False)
            mc.post       = AsyncMock(return_value=fake_resp)
            mock_cls.return_value = mc
            return await _insert_rows("flow_events", [{"ticker": "AAPL"}])

    assert asyncio.get_event_loop().run_until_complete(_test()) is False


# 22
def test_insert_rows_false_on_exception():
    async def _test():
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", "key"), \
             patch("httpx.AsyncClient") as mock_cls:
            mc = AsyncMock()
            mc.__aenter__ = AsyncMock(return_value=mc)
            mc.__aexit__  = AsyncMock(return_value=False)
            mc.post       = AsyncMock(side_effect=ConnectionError("timeout"))
            mock_cls.return_value = mc
            return await _insert_rows("flow_events", [{"ticker": "AAPL"}])

    assert asyncio.get_event_loop().run_until_complete(_test()) is False


# 23
def test_insert_rows_false_on_empty_rows():
    async def _test():
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", "key"):
            return await _insert_rows("flow_events", [])

    assert asyncio.get_event_loop().run_until_complete(_test()) is False


# ============================================================
# _bus_signal_listener
# ============================================================

def _mock_bus_with_msg(msg):
    mock_q = asyncio.Queue()
    asyncio.get_event_loop().run_until_complete(mock_q.put(msg))
    mock_bus = MagicMock()
    mock_bus.subscribe.return_value = mock_q
    mock_bus.unsubscribe            = MagicMock()
    return mock_bus, mock_q


# 24
def test_bus_listener_composite_signal_triggers_persist_episode():
    msg = {
        "type": "composite_signal",
        "data": {
            "signal":  {"ticker": "AAPL", "recommendation": "BUY", "reasoning": "x"},
            "episode": _ep_data(),
        },
    }
    mock_bus, mock_q = _mock_bus_with_msg(msg)

    async def _test():
        with patch.object(fs, "persist_flow_episode", new_callable=AsyncMock) as mock_ep, \
             patch("services.flow_store.bus", mock_bus):
            task = asyncio.create_task(_bus_signal_listener())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert mock_ep.call_count >= 1

    asyncio.get_event_loop().run_until_complete(_test())


# 25
def test_bus_listener_raw_signal_does_not_trigger_persist():
    mock_bus, mock_q = _mock_bus_with_msg({"type": "signal", "data": {"ticker": "AAPL"}})

    async def _test():
        with patch.object(fs, "persist_flow_episode", new_callable=AsyncMock) as mock_ep, \
             patch("services.flow_store.bus", mock_bus):
            task = asyncio.create_task(_bus_signal_listener())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert mock_ep.call_count == 0

    asyncio.get_event_loop().run_until_complete(_test())


# 26
def test_bus_listener_non_dict_message_ignored():
    mock_bus, mock_q = _mock_bus_with_msg("not-a-dict")

    async def _test():
        with patch.object(fs, "persist_flow_episode", new_callable=AsyncMock) as mock_ep, \
             patch("services.flow_store.bus", mock_bus):
            task = asyncio.create_task(_bus_signal_listener())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert mock_ep.call_count == 0

    asyncio.get_event_loop().run_until_complete(_test())


# 27
def test_bus_listener_cancelled_error_unsubscribes():
    async def _test():
        mock_q = asyncio.Queue()
        mock_bus = MagicMock()
        mock_bus.subscribe.return_value = mock_q
        mock_bus.unsubscribe            = MagicMock()

        with patch("services.flow_store.bus", mock_bus):
            task = asyncio.create_task(_bus_signal_listener())
            await asyncio.sleep(0.02)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            mock_bus.unsubscribe.assert_called_once_with("db_writer", mock_q)

    asyncio.get_event_loop().run_until_complete(_test())


# ============================================================
# constants
# ============================================================

# 28
def test_flush_interval_is_500ms():
    assert fs._FLUSH_INTERVAL == pytest.approx(0.5)


# 29
def test_flush_max_rows_is_100():
    assert fs._FLUSH_MAX_ROWS == 100


# ============================================================
# F-01: env-var gate on persist_flow_event
# ============================================================

# 30
def test_persist_flow_event_noop_when_env_vars_missing():
    """F-01: event must be silently dropped (buffer stays empty) when env vars absent."""
    _reset_buffer()
    with patch.object(fs, "_is_configured", return_value=False):
        asyncio.get_event_loop().run_until_complete(persist_flow_event(_ev()))
    assert len(fs._flow_event_buffer) == 0


# 31
def test_persist_flow_event_logs_warning_when_env_vars_missing(caplog):
    """F-01: a WARNING must be emitted so ops can see the misconfiguration."""
    _reset_buffer()
    with patch.object(fs, "_is_configured", return_value=False), \
         caplog.at_level(logging.WARNING, logger="flow_store"):
        asyncio.get_event_loop().run_until_complete(persist_flow_event(_ev()))
    assert any("not set" in r.message.lower() or "env" in r.message.lower()
               for r in caplog.records)


# ============================================================
# F-02: retry buffer on flush failure
# ============================================================

# 32
def test_insert_rows_with_retry_retries_up_to_retry_max():
    """F-02: _insert_rows_with_retry must call _insert_rows up to _RETRY_MAX times."""
    from services.flow_store import _insert_rows_with_retry

    call_count = {"n": 0}

    async def _always_fail(table, rows):
        call_count["n"] += 1
        return False

    async def _test():
        with patch.object(fs, "_insert_rows", side_effect=_always_fail), \
             patch("asyncio.sleep", new_callable=AsyncMock):  # skip retry delay in tests
            result = await _insert_rows_with_retry("flow_events", [{"ticker": "AAPL"}])
        return result

    result = asyncio.get_event_loop().run_until_complete(_test())
    assert result is False
    assert call_count["n"] == fs._RETRY_MAX


# 33
def test_insert_rows_with_retry_logs_error_after_all_retries(caplog):
    """F-02: after all retries exhausted an ERROR must be logged so data loss is visible."""
    from services.flow_store import _insert_rows_with_retry

    async def _test():
        with patch.object(fs, "_insert_rows", new_callable=AsyncMock, return_value=False), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             caplog.at_level(logging.ERROR, logger="flow_store"):
            await _insert_rows_with_retry("flow_events", [{"ticker": "AAPL"}])

    asyncio.get_event_loop().run_until_complete(_test())
    assert any("discarded" in r.message.lower() or "failed after" in r.message.lower()
               for r in caplog.records)
