"""
Unit tests for services/signal_store.py

Covers:
  _build_row
  1.  Returns all required keys
  2.  direction normalised: REPEAT_BUY -> BUY
  3.  trade_type normalised: UNKNOWN -> SINGLE
  4.  influence_tier normalised: bad value -> RETAIL
  5.  alert_level derived from composite_score when absent
  6.  sentiment derived from contract_type / direction when absent
  7.  swarm fields forwarded to row
  8.  premium falls back from total_premium
  9.  empty expiry fields produce None (no crash)

  persist_composite_signal
  10. Calls _insert_signal_with_retry (not _insert_signal directly)
  11. Logs success info on True
  12. Logs warning on False

  _insert_signal
  13. Returns False when env vars missing
  14. Returns True on 201
  15. Returns False on 4xx

  S-02: _insert_signal_with_retry
  16. Retries up to _RETRY_MAX on failure
  17. Logs ERROR and returns False after all retries exhausted
  18. Returns True immediately on first success

  _bus_signal_listener
  19. composite_signal triggers persist_composite_signal
  20. Non-composite_signal message is ignored
  21. CancelledError unsubscribes from bus

  start_signal_writer
  22. Returns early (no bus subscribe) when env vars missing
  23. Warning message references SUPABASE_SERVICE_ROLE_KEY (S-01)
"""
import asyncio
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.signal_store as ss
from services.signal_store import (
    _build_row,
    _bus_signal_listener,
    _insert_signal,
    _insert_signal_with_retry,
    _normalise_direction,
    _normalise_influence_tier,
    _normalise_trade_type,
    persist_composite_signal,
    start_signal_writer,
)


# -- helpers -----------------------------------------------------------------
def _sig(
    ticker="AAPL",
    recommendation="BUY",
    composite_score=0.88,
    flow_score=0.82,
    backtest_score=0.75,
    reasoning="Strong sweep cluster",
    direction="BUY",
    sentiment="BULLISH",
    alert_level="CONVICTION",
    swarm_direction="BUY",
    swarm_confidence=0.9,
    swarm_bull_votes=7,
    swarm_bear_votes=1,
    swarm_hold_votes=2,
    swarm_agents=None,
    is_golden_sweep=False,
):
    return {
        "ticker":             ticker,
        "recommendation":     recommendation,
        "composite_score":    composite_score,
        "flow_score":         flow_score,
        "backtest_score":     backtest_score,
        "reasoning":          reasoning,
        "direction":          direction,
        "sentiment":          sentiment,
        "alert_level":        alert_level,
        "swarm_direction":    swarm_direction,
        "swarm_confidence":   swarm_confidence,
        "swarm_bull_votes":   swarm_bull_votes,
        "swarm_bear_votes":   swarm_bear_votes,
        "swarm_hold_votes":   swarm_hold_votes,
        "swarm_agents":       swarm_agents,
        "is_golden_sweep":    is_golden_sweep,
    }


def _ep(
    ticker="AAPL",
    direction="BUY",
    contract_type="CALL",
    total_premium=1_500_000.0,
    trade_count=6,
    is_accelerating=True,
    timestamp="2026-04-25T10:00:00",
    influence_tier="WHALE",
    trade_type="SWEEP",
):
    return {
        "ticker":          ticker,
        "direction":       direction,
        "contract_type":   contract_type,
        "total_premium":   total_premium,
        "trade_count":     trade_count,
        "is_accelerating": is_accelerating,
        "timestamp":       timestamp,
        "influence_tier":  influence_tier,
        "trade_type":      trade_type,
    }


# ============================================================
# _build_row
# ============================================================

# 1
def test_build_row_returns_required_keys():
    row = _build_row(_sig(), _ep())
    for key in [
        "ticker", "recommendation", "composite_score", "flow_score",
        "backtest_score", "reasoning", "alert_level", "direction",
        "sentiment", "premium", "trade_type", "influence_tier",
        "is_golden_sweep", "swarm_direction", "swarm_confidence",
        "swarm_bull_votes", "swarm_bear_votes", "swarm_hold_votes", "swarm_agents",
    ]:
        assert key in row, f"Missing key: {key}"


# 2
def test_direction_normalised_repeat_buy_to_buy():
    row = _build_row(_sig(direction="REPEAT_BUY"), _ep(direction="REPEAT_BUY"))
    assert row["direction"] == "BUY"


# 3
def test_trade_type_normalised_unknown_to_single():
    ep = _ep(trade_type="UNKNOWN")
    row = _build_row(_sig(), ep)
    assert row["trade_type"] == "SINGLE"


# 4
def test_influence_tier_normalised_bad_to_retail():
    ep = _ep(influence_tier="VVIP")
    row = _build_row(_sig(), ep)
    assert row["influence_tier"] == "RETAIL"


# 5
def test_alert_level_derived_from_composite_score():
    sig = _sig(composite_score=0.90)
    del sig["alert_level"]
    row = _build_row(sig, {})
    assert row["alert_level"] == "CONVICTION"

    sig2 = _sig(composite_score=0.72)
    del sig2["alert_level"]
    row2 = _build_row(sig2, {})
    assert row2["alert_level"] == "STRONG_SIGNAL"


# 6
def test_sentiment_derived_from_contract_type():
    sig = _sig()
    del sig["sentiment"]
    row = _build_row(sig, _ep(contract_type="CALL", direction=""))
    assert row["sentiment"] == "BULLISH"

    row2 = _build_row(sig, _ep(contract_type="PUT", direction=""))
    assert row2["sentiment"] == "BEARISH"


# 7
def test_swarm_fields_forwarded():
    sig = _sig(swarm_direction="SELL", swarm_bull_votes=2, swarm_bear_votes=8, swarm_hold_votes=0)
    row = _build_row(sig, _ep())
    assert row["swarm_direction"] == "SELL"
    assert row["swarm_bull_votes"] == 2
    assert row["swarm_bear_votes"] == 8


# 8
def test_premium_falls_back_from_episode_total_premium():
    row = _build_row(_sig(), _ep(total_premium=999_000.0))
    assert row["premium"] == 999_000.0


# 9
def test_build_row_no_crash_on_empty_sig_and_ep():
    row = _build_row({}, {})
    assert isinstance(row, dict)
    assert row["direction"]      == "HOLD"
    assert row["trade_type"]     == "SINGLE"
    assert row["influence_tier"] == "RETAIL"


# ============================================================
# persist_composite_signal
# ============================================================

# 10
def test_persist_composite_signal_uses_retry():
    async def _test():
        with patch.object(ss, "_insert_signal_with_retry", new_callable=AsyncMock, return_value=True) as mock_r, \
             patch.object(ss, "_insert_signal", new_callable=AsyncMock) as mock_direct:
            await persist_composite_signal(_sig(), _ep())
            assert mock_r.call_count == 1
            assert mock_direct.call_count == 0  # must NOT call direct insert

    asyncio.get_event_loop().run_until_complete(_test())


# 11
def test_persist_composite_signal_logs_success(caplog):
    async def _test():
        with patch.object(ss, "_insert_signal_with_retry", new_callable=AsyncMock, return_value=True), \
             caplog.at_level(logging.INFO, logger="signal_store"):
            await persist_composite_signal(_sig(), _ep())

    asyncio.get_event_loop().run_until_complete(_test())
    assert any("INSERT OK" in r.message or "DB INSERT" in r.message for r in caplog.records)


# 12
def test_persist_composite_signal_logs_warning_on_failure(caplog):
    async def _test():
        with patch.object(ss, "_insert_signal_with_retry", new_callable=AsyncMock, return_value=False), \
             caplog.at_level(logging.WARNING, logger="signal_store"):
            await persist_composite_signal(_sig(), _ep())

    asyncio.get_event_loop().run_until_complete(_test())
    assert any("FAILED" in r.message or "NOT saved" in r.message for r in caplog.records)


# ============================================================
# _insert_signal
# ============================================================

# 13
def test_insert_signal_false_when_env_missing():
    async def _test():
        with patch.object(ss, "_SUPABASE_URL", None), \
             patch.object(ss, "_SUPABASE_KEY", None):
            return await _insert_signal({"ticker": "AAPL"})

    assert asyncio.get_event_loop().run_until_complete(_test()) is False


# 14
def test_insert_signal_true_on_201():
    fake_resp = MagicMock()
    fake_resp.status_code = 201

    async def _test():
        with patch.object(ss, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(ss, "_SUPABASE_KEY", "key"), \
             patch("httpx.AsyncClient") as mock_cls:
            mc = AsyncMock()
            mc.__aenter__ = AsyncMock(return_value=mc)
            mc.__aexit__  = AsyncMock(return_value=False)
            mc.post       = AsyncMock(return_value=fake_resp)
            mock_cls.return_value = mc
            return await _insert_signal({"ticker": "AAPL"})

    assert asyncio.get_event_loop().run_until_complete(_test()) is True


# 15
def test_insert_signal_false_on_4xx():
    fake_resp = MagicMock()
    fake_resp.status_code = 422
    fake_resp.text        = "Unprocessable"

    async def _test():
        with patch.object(ss, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(ss, "_SUPABASE_KEY", "key"), \
             patch("httpx.AsyncClient") as mock_cls:
            mc = AsyncMock()
            mc.__aenter__ = AsyncMock(return_value=mc)
            mc.__aexit__  = AsyncMock(return_value=False)
            mc.post       = AsyncMock(return_value=fake_resp)
            mock_cls.return_value = mc
            return await _insert_signal({"ticker": "AAPL"})

    assert asyncio.get_event_loop().run_until_complete(_test()) is False


# ============================================================
# S-02: _insert_signal_with_retry
# ============================================================

# 16
def test_insert_signal_with_retry_retries_up_to_retry_max():
    call_count = {"n": 0}

    async def _always_fail(row):
        call_count["n"] += 1
        return False

    async def _test():
        with patch.object(ss, "_insert_signal", side_effect=_always_fail), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            return await _insert_signal_with_retry({"ticker": "AAPL"})

    result = asyncio.get_event_loop().run_until_complete(_test())
    assert result is False
    assert call_count["n"] == ss._RETRY_MAX


# 17
def test_insert_signal_with_retry_logs_error_after_exhaustion(caplog):
    async def _test():
        with patch.object(ss, "_insert_signal", new_callable=AsyncMock, return_value=False), \
             patch("asyncio.sleep", new_callable=AsyncMock), \
             caplog.at_level(logging.ERROR, logger="signal_store"):
            await _insert_signal_with_retry({"ticker": "AAPL"})

    asyncio.get_event_loop().run_until_complete(_test())
    assert any(
        "discarded" in r.message.lower() or "failed after" in r.message.lower()
        for r in caplog.records
    )


# 18
def test_insert_signal_with_retry_returns_true_on_first_success():
    call_count = {"n": 0}

    async def _succeeds_first(row):
        call_count["n"] += 1
        return True

    async def _test():
        with patch.object(ss, "_insert_signal", side_effect=_succeeds_first), \
             patch("asyncio.sleep", new_callable=AsyncMock):
            return await _insert_signal_with_retry({"ticker": "AAPL"})

    result = asyncio.get_event_loop().run_until_complete(_test())
    assert result is True
    assert call_count["n"] == 1


# ============================================================
# _bus_signal_listener
# ============================================================

# 19
def test_bus_listener_composite_signal_triggers_persist():
    msg = {
        "type": "composite_signal",
        "data": {
            "signal":  _sig(),
            "episode": _ep(),
        },
    }

    mock_q = asyncio.Queue()
    asyncio.get_event_loop().run_until_complete(mock_q.put(msg))
    mock_bus = MagicMock()
    mock_bus.subscribe.return_value  = mock_q
    mock_bus.unsubscribe             = MagicMock()

    async def _test():
        with patch.object(ss, "persist_composite_signal", new_callable=AsyncMock) as mock_p, \
             patch("services.signal_store.bus", mock_bus):
            task = asyncio.create_task(_bus_signal_listener())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert mock_p.call_count >= 1

    asyncio.get_event_loop().run_until_complete(_test())


# 20
def test_bus_listener_ignores_non_composite_signal():
    mock_q = asyncio.Queue()
    asyncio.get_event_loop().run_until_complete(mock_q.put({"type": "signal", "data": {}}))
    mock_bus = MagicMock()
    mock_bus.subscribe.return_value = mock_q
    mock_bus.unsubscribe            = MagicMock()

    async def _test():
        with patch.object(ss, "persist_composite_signal", new_callable=AsyncMock) as mock_p, \
             patch("services.signal_store.bus", mock_bus):
            task = asyncio.create_task(_bus_signal_listener())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert mock_p.call_count == 0

    asyncio.get_event_loop().run_until_complete(_test())


# 21
def test_bus_listener_cancelled_error_unsubscribes():
    async def _test():
        mock_q = asyncio.Queue()
        mock_bus = MagicMock()
        mock_bus.subscribe.return_value = mock_q
        mock_bus.unsubscribe            = MagicMock()

        with patch("services.signal_store.bus", mock_bus):
            task = asyncio.create_task(_bus_signal_listener())
            await asyncio.sleep(0.02)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            mock_bus.unsubscribe.assert_called_once_with("signal_writer", mock_q)

    asyncio.get_event_loop().run_until_complete(_test())


# ============================================================
# start_signal_writer
# ============================================================

# 22
def test_start_signal_writer_returns_early_when_env_missing():
    async def _test():
        with patch.object(ss, "_SUPABASE_URL", None), \
             patch.object(ss, "_SUPABASE_KEY", None), \
             patch.object(ss, "_bus_signal_listener", new_callable=AsyncMock) as mock_bus:
            await start_signal_writer()
            assert mock_bus.call_count == 0

    asyncio.get_event_loop().run_until_complete(_test())


# 23 (S-01)
def test_start_signal_writer_warning_references_correct_env_var(caplog):
    async def _test():
        with patch.object(ss, "_SUPABASE_URL", None), \
             patch.object(ss, "_SUPABASE_KEY", None), \
             caplog.at_level(logging.WARNING, logger="signal_store"):
            await start_signal_writer()

    asyncio.get_event_loop().run_until_complete(_test())
    assert any(
        "SUPABASE_SERVICE_ROLE_KEY" in r.message
        for r in caplog.records
    ), "Warning must reference the correct Railway env var SUPABASE_SERVICE_ROLE_KEY"
