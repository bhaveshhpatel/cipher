"""
Round 3 tests for services/signal_store.py

Covers S-05: mid-run env-var guard in persist_composite_signal
and _is_configured().

Tests 1-7 here are ADDITIVE to the 23 tests already in test_signal_store.py
(Round 2). They are in a separate file to avoid modifying the committed
test_signal_store.py SHA.

S-05 tests:
  1. _is_configured returns False when _SUPABASE_URL is None
  2. _is_configured returns False when _SUPABASE_KEY is None
  3. _is_configured returns True when both are set
  4. persist_composite_signal returns immediately (no retry call) when not configured
  5. persist_composite_signal logs a WARNING mentioning the ticker when not configured
  6. persist_composite_signal does NOT call _build_row when not configured
  7. start_signal_writer uses _is_configured (returns early + logs warning)
"""
import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import services.signal_store as ss
from services.signal_store import (
    _is_configured,
    persist_composite_signal,
    start_signal_writer,
)


def _sig(ticker="TSLA"):
    return {
        "ticker":          ticker,
        "recommendation":  "BUY",
        "composite_score": 0.88,
        "flow_score":      0.82,
        "backtest_score":  0.75,
        "direction":       "BUY",
        "sentiment":       "BULLISH",
        "alert_level":     "CONVICTION",
        "swarm_direction": "BUY",
        "swarm_confidence":0.9,
        "swarm_bull_votes":7,
        "swarm_bear_votes":1,
        "swarm_hold_votes":2,
    }


# ============================================================
# _is_configured
# ============================================================

# 1
def test_is_configured_false_when_url_none():
    with patch.object(ss, "_SUPABASE_URL", None), \
         patch.object(ss, "_SUPABASE_KEY", "some-key"):
        assert _is_configured() is False


# 2
def test_is_configured_false_when_key_none():
    with patch.object(ss, "_SUPABASE_URL", "https://x.supabase.co"), \
         patch.object(ss, "_SUPABASE_KEY", None):
        assert _is_configured() is False


# 3
def test_is_configured_true_when_both_set():
    with patch.object(ss, "_SUPABASE_URL", "https://x.supabase.co"), \
         patch.object(ss, "_SUPABASE_KEY", "service-role-key"):
        assert _is_configured() is True


# ============================================================
# persist_composite_signal — S-05 guard
# ============================================================

# 4
def test_persist_no_retry_call_when_not_configured():
    """_insert_signal_with_retry must NOT be called when env vars are missing."""
    async def _test():
        with patch.object(ss, "_SUPABASE_URL", None), \
             patch.object(ss, "_SUPABASE_KEY", None), \
             patch.object(ss, "_insert_signal_with_retry", new_callable=AsyncMock) as mock_r:
            await persist_composite_signal(_sig())
            assert mock_r.call_count == 0

    asyncio.get_event_loop().run_until_complete(_test())


# 5
def test_persist_logs_warning_with_ticker_when_not_configured(caplog):
    """Warning must mention the ticker so it's searchable in Railway logs."""
    async def _test():
        with patch.object(ss, "_SUPABASE_URL", None), \
             patch.object(ss, "_SUPABASE_KEY", None), \
             caplog.at_level(logging.WARNING, logger="signal_store"):
            await persist_composite_signal(_sig(ticker="NVDA"))

    asyncio.get_event_loop().run_until_complete(_test())
    assert any(
        "NVDA" in r.message or "NVDA" in str(r.args)
        for r in caplog.records
    ), "Warning must include the ticker name"


# 6
def test_persist_does_not_call_build_row_when_not_configured():
    """_build_row must NOT be called -- no wasted CPU when env vars missing."""
    async def _test():
        with patch.object(ss, "_SUPABASE_URL", None), \
             patch.object(ss, "_SUPABASE_KEY", None), \
             patch.object(ss, "_build_row", wraps=ss._build_row) as mock_br:
            await persist_composite_signal(_sig())
            assert mock_br.call_count == 0

    asyncio.get_event_loop().run_until_complete(_test())


# ============================================================
# start_signal_writer — S-05 consistency
# ============================================================

# 7
def test_start_signal_writer_uses_is_configured(caplog):
    """start_signal_writer must log its warning and not touch the bus."""
    async def _test():
        with patch.object(ss, "_SUPABASE_URL", None), \
             patch.object(ss, "_SUPABASE_KEY", None), \
             patch.object(ss, "_bus_signal_listener", new_callable=AsyncMock) as mock_bus, \
             caplog.at_level(logging.WARNING, logger="signal_store"):
            await start_signal_writer()
            assert mock_bus.call_count == 0

    asyncio.get_event_loop().run_until_complete(_test())
    assert any(
        "SUPABASE_SERVICE_ROLE_KEY" in r.message
        for r in caplog.records
    )
