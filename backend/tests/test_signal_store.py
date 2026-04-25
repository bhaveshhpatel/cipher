"""
Unit tests for services/signal_store.py

Covers:
  _normalise_direction
  1.  BUY / SELL / HOLD pass through unchanged
  2.  REPEAT_BUY → BUY
  3.  REPEAT_SELL → SELL
  4.  Empty string → HOLD
  5.  Unrecognised value → HOLD
  6.  Case-insensitive: 'buy' → 'BUY'

  _normalise_trade_type
  7.  SWEEP / BLOCK / SPLIT / SINGLE pass through
  8.  Unknown value → SINGLE
  9.  Empty string → SINGLE
  10. Case-insensitive: 'sweep' → 'SWEEP'

  _normalise_influence_tier
  11. WHALE / INSTITUTIONAL / LARGE / RETAIL pass through
  12. Unknown value → RETAIL
  13. Empty string → RETAIL
  14. Case-insensitive: 'whale' → 'WHALE'

  _build_row
  15. Keys present for all NOT NULL columns
  16. alert_level derived from composite_score ≥ 0.85 → CONVICTION
  17. alert_level STRONG_SIGNAL for score 0.70–0.85
  18. alert_level ALERT for score 0.55–0.70
  19. alert_level WATCH below 0.55
  20. Explicit alert_level in sig takes precedence
  21. sentiment BULLISH when direction contains BUY
  22. sentiment BEARISH when contract_type = PUT
  23. sentiment NEUTRAL fallback
  24. Explicit sentiment in sig takes precedence
  25. direction column is always in {BUY, SELL, HOLD}
  26. trade_type column is always in {SWEEP, BLOCK, SPLIT, SINGLE}
  27. influence_tier column is always in {WHALE, INSTITUTIONAL, LARGE, RETAIL}
  28. is_golden_sweep cast to bool
  29. Phase 5A swarm fields persisted from sig dict
  30. episode total_premium used for premium column
  31. ep=None does not crash _build_row

  _insert_signal
  32. Returns False when SUPABASE_URL not set
  33. Returns True on 201 response
  34. Returns False on 4xx response
  35. Returns False on network exception

  persist_composite_signal
  36. Calls _insert_signal with correctly built row
  37. Logs warning on failed insert

  _bus_signal_listener
  38. Calls persist_composite_signal on composite_signal bus message
  39. Non-matching message type is ignored
  40. CancelledError unsubscribes and re-raises
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.signal_store import (
    _build_row,
    _normalise_direction,
    _normalise_influence_tier,
    _normalise_trade_type,
    _insert_signal,
    persist_composite_signal,
    _bus_signal_listener,
)


# ── fixtures ─────────────────────────────────────────────────────────────────
def _sig(
    ticker="AAPL",
    recommendation="BUY",
    composite_score=0.72,
    flow_score=0.65,
    backtest_score=0.70,
    volume_premium_factor=0.80,
    reasoning="Test reasoning.",
    direction="BUY",
    swarm_direction="BUY",
    swarm_confidence=0.82,
    swarm_bull_votes=6,
    swarm_bear_votes=2,
    swarm_hold_votes=2,
    swarm_agents=None,
):
    return {
        "ticker":                ticker,
        "recommendation":        recommendation,
        "composite_score":       composite_score,
        "flow_score":            flow_score,
        "backtest_score":        backtest_score,
        "volume_premium_factor": volume_premium_factor,
        "reasoning":             reasoning,
        "direction":             direction,
        "swarm_direction":       swarm_direction,
        "swarm_confidence":      swarm_confidence,
        "swarm_bull_votes":      swarm_bull_votes,
        "swarm_bear_votes":      swarm_bear_votes,
        "swarm_hold_votes":      swarm_hold_votes,
        "swarm_agents":          swarm_agents or [],
    }


def _ep(
    contract_type="CALL",
    trade_type="SWEEP",
    influence_tier="WHALE",
    total_premium=2_000_000.0,
    trade_count=8,
    is_accelerating=True,
    is_golden_sweep=True,
    timestamp="2026-04-25T10:00:00",
    direction="BUY",
):
    return {
        "contract_type":  contract_type,
        "trade_type":     trade_type,
        "influence_tier": influence_tier,
        "total_premium":  total_premium,
        "trade_count":    trade_count,
        "is_accelerating": is_accelerating,
        "is_golden_sweep": is_golden_sweep,
        "timestamp":      timestamp,
        "direction":      direction,
    }


_VALID_DIRECTIONS  = {"BUY", "SELL", "HOLD"}
_VALID_TRADE_TYPES = {"SWEEP", "BLOCK", "SPLIT", "SINGLE"}
_VALID_TIERS       = {"WHALE", "INSTITUTIONAL", "LARGE", "RETAIL"}


# ============================================================
# _normalise_direction
# ============================================================

# 1
@pytest.mark.parametrize("v", ["BUY", "SELL", "HOLD"])
def test_normalise_direction_valid_pass_through(v):
    assert _normalise_direction(v) == v


# 2
def test_normalise_direction_repeat_buy():
    assert _normalise_direction("REPEAT_BUY") == "BUY"


# 3
def test_normalise_direction_repeat_sell():
    assert _normalise_direction("REPEAT_SELL") == "SELL"


# 4
def test_normalise_direction_empty_string():
    assert _normalise_direction("") == "HOLD"


# 5
def test_normalise_direction_unknown():
    assert _normalise_direction("LATERAL_MOVE") == "HOLD"


# 6
def test_normalise_direction_case_insensitive():
    assert _normalise_direction("buy") == "BUY"


# ============================================================
# _normalise_trade_type
# ============================================================

# 7
@pytest.mark.parametrize("v", ["SWEEP", "BLOCK", "SPLIT", "SINGLE"])
def test_normalise_trade_type_valid_pass_through(v):
    assert _normalise_trade_type(v) == v


# 8
def test_normalise_trade_type_unknown():
    assert _normalise_trade_type("UNKNOWN") == "SINGLE"


# 9
def test_normalise_trade_type_empty():
    assert _normalise_trade_type("") == "SINGLE"


# 10
def test_normalise_trade_type_case_insensitive():
    assert _normalise_trade_type("sweep") == "SWEEP"


# ============================================================
# _normalise_influence_tier
# ============================================================

# 11
@pytest.mark.parametrize("v", ["WHALE", "INSTITUTIONAL", "LARGE", "RETAIL"])
def test_normalise_tier_valid_pass_through(v):
    assert _normalise_influence_tier(v) == v


# 12
def test_normalise_tier_unknown():
    assert _normalise_influence_tier("VIP") == "RETAIL"


# 13
def test_normalise_tier_empty():
    assert _normalise_influence_tier("") == "RETAIL"


# 14
def test_normalise_tier_case_insensitive():
    assert _normalise_influence_tier("whale") == "WHALE"


# ============================================================
# _build_row
# ============================================================

# 15
def test_build_row_has_all_not_null_keys():
    row = _build_row(_sig(), _ep())
    required = [
        "ticker", "recommendation", "composite_score",
        "alert_level", "direction", "sentiment",
        "premium", "trade_type", "influence_tier", "is_golden_sweep",
    ]
    for key in required:
        assert key in row, f"Missing required key: {key}"
        assert row[key] is not None, f"NOT NULL column is None: {key}"


# 16
def test_build_row_alert_conviction():
    row = _build_row(_sig(composite_score=0.90), _ep())
    assert row["alert_level"] == "CONVICTION"


# 17
def test_build_row_alert_strong_signal():
    row = _build_row(_sig(composite_score=0.75), _ep())
    assert row["alert_level"] == "STRONG_SIGNAL"


# 18
def test_build_row_alert_alert():
    row = _build_row(_sig(composite_score=0.60), _ep())
    assert row["alert_level"] == "ALERT"


# 19
def test_build_row_alert_watch():
    row = _build_row(_sig(composite_score=0.40), _ep())
    assert row["alert_level"] == "WATCH"


# 20
def test_build_row_explicit_alert_level_wins():
    sig = _sig(composite_score=0.20)  # would be WATCH
    sig["alert_level"] = "CONVICTION"
    row = _build_row(sig, _ep())
    assert row["alert_level"] == "CONVICTION"


# 21
def test_build_row_sentiment_bullish_from_direction():
    row = _build_row(_sig(direction="BUY"), _ep(direction="BUY", contract_type="CALL"))
    assert row["sentiment"] == "BULLISH"


# 22
def test_build_row_sentiment_bearish_from_put():
    row = _build_row(_sig(direction="HOLD"), _ep(direction="SELL", contract_type="PUT"))
    assert row["sentiment"] == "BEARISH"


# 23
def test_build_row_sentiment_neutral_fallback():
    row = _build_row(_sig(direction="HOLD"), _ep(direction="HOLD", contract_type=""))
    assert row["sentiment"] == "NEUTRAL"


# 24
def test_build_row_explicit_sentiment_wins():
    sig = _sig(direction="HOLD")
    sig["sentiment"] = "BEARISH"
    row = _build_row(sig, _ep(direction="HOLD", contract_type=""))
    assert row["sentiment"] == "BEARISH"


# 25
def test_build_row_direction_always_valid():
    for raw in ["", "LATERAL", "REPEAT_BUY", "HOLD", "sell"]:
        sig = _sig(direction=raw)
        row = _build_row(sig, _ep(direction=raw))
        assert row["direction"] in _VALID_DIRECTIONS


# 26
def test_build_row_trade_type_always_valid():
    for raw in ["", "UNKNOWN", "SWEEP", "block"]:
        ep  = _ep(trade_type=raw)
        row = _build_row(_sig(), ep)
        assert row["trade_type"] in _VALID_TRADE_TYPES


# 27
def test_build_row_influence_tier_always_valid():
    for raw in ["", "VIP", "WHALE", "institutional"]:
        ep  = _ep(influence_tier=raw)
        row = _build_row(_sig(), ep)
        assert row["influence_tier"] in _VALID_TIERS


# 28
def test_build_row_is_golden_sweep_is_bool():
    row = _build_row(_sig(), _ep(is_golden_sweep=True))
    assert isinstance(row["is_golden_sweep"], bool)
    assert row["is_golden_sweep"] is True


# 29
def test_build_row_swarm_fields_persisted():
    sig = _sig(
        swarm_direction="SELL",
        swarm_confidence=0.77,
        swarm_bull_votes=2,
        swarm_bear_votes=7,
        swarm_hold_votes=1,
        swarm_agents=[{"agent": "a1", "vote": "SELL"}],
    )
    row = _build_row(sig, _ep())
    assert row["swarm_direction"]  == "SELL"
    assert row["swarm_confidence"] == pytest.approx(0.77)
    assert row["swarm_bull_votes"] == 2
    assert row["swarm_bear_votes"] == 7
    assert row["swarm_hold_votes"] == 1
    assert len(row["swarm_agents"]) == 1


# 30
def test_build_row_episode_premium_used():
    row = _build_row(_sig(), _ep(total_premium=1_234_567.0))
    assert row["premium"] == pytest.approx(1_234_567.0)


# 31
def test_build_row_no_episode_does_not_crash():
    row = _build_row(_sig(), None)
    assert row is not None
    assert row["direction"] in _VALID_DIRECTIONS


# ============================================================
# _insert_signal
# ============================================================

# 32
def test_insert_signal_returns_false_when_url_not_set():
    import services.signal_store as ss
    original_url = ss._SUPABASE_URL
    original_key = ss._SUPABASE_KEY
    ss._SUPABASE_URL = None
    ss._SUPABASE_KEY = None
    try:
        result = asyncio.get_event_loop().run_until_complete(
            _insert_signal({"ticker": "AAPL"})
        )
        assert result is False
    finally:
        ss._SUPABASE_URL = original_url
        ss._SUPABASE_KEY = original_key


# 33
def test_insert_signal_returns_true_on_201():
    import services.signal_store as ss
    import httpx

    fake_resp = MagicMock()
    fake_resp.status_code = 201

    async def _test():
        with patch.object(ss, "_SUPABASE_URL", "https://example.supabase.co"), \
             patch.object(ss, "_SUPABASE_KEY", "test-key"), \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__  = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=fake_resp)
            mock_client_cls.return_value = mock_client
            return await _insert_signal({"ticker": "AAPL"})

    assert asyncio.get_event_loop().run_until_complete(_test()) is True


# 34
def test_insert_signal_returns_false_on_4xx():
    import services.signal_store as ss
    import httpx

    fake_resp = MagicMock()
    fake_resp.status_code = 422
    fake_resp.text        = "Unprocessable Entity"

    async def _test():
        with patch.object(ss, "_SUPABASE_URL", "https://example.supabase.co"), \
             patch.object(ss, "_SUPABASE_KEY", "test-key"), \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__  = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=fake_resp)
            mock_client_cls.return_value = mock_client
            return await _insert_signal({"ticker": "AAPL"})

    assert asyncio.get_event_loop().run_until_complete(_test()) is False


# 35
def test_insert_signal_returns_false_on_exception():
    import services.signal_store as ss

    async def _test():
        with patch.object(ss, "_SUPABASE_URL", "https://example.supabase.co"), \
             patch.object(ss, "_SUPABASE_KEY", "test-key"), \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__  = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=ConnectionError("network down"))
            mock_client_cls.return_value = mock_client
            return await _insert_signal({"ticker": "AAPL"})

    assert asyncio.get_event_loop().run_until_complete(_test()) is False


# ============================================================
# persist_composite_signal
# ============================================================

# 36
def test_persist_calls_insert_with_row():
    import services.signal_store as ss

    async def _test():
        with patch.object(ss, "_insert_signal", new_callable=AsyncMock, return_value=True) as mock_ins:
            await persist_composite_signal(_sig(), _ep())
            assert mock_ins.call_count == 1
            row = mock_ins.call_args[0][0]
            assert row["ticker"]    == "AAPL"
            assert row["direction"] in _VALID_DIRECTIONS

    asyncio.get_event_loop().run_until_complete(_test())


# 37
def test_persist_logs_warning_on_failed_insert(caplog):
    import services.signal_store as ss
    import logging

    async def _test():
        with patch.object(ss, "_insert_signal", new_callable=AsyncMock, return_value=False):
            with caplog.at_level(logging.WARNING, logger="signal_store"):
                await persist_composite_signal(_sig(), _ep())

    asyncio.get_event_loop().run_until_complete(_test())
    assert any("FAILED" in r.message or "NOT saved" in r.message for r in caplog.records)


# ============================================================
# _bus_signal_listener
# ============================================================

# 38
def test_bus_listener_calls_persist_on_composite_signal_message():
    import services.signal_store as ss

    msg = {
        "type": "composite_signal",
        "data": {
            "signal":  _sig(),
            "episode": _ep(),
        },
    }

    async def _test():
        mock_q = asyncio.Queue()
        await mock_q.put(msg)
        # Second item causes CancelledError via cancellation
        mock_bus = MagicMock()
        mock_bus.subscribe.return_value   = mock_q
        mock_bus.unsubscribe              = MagicMock()

        with patch.object(ss, "persist_composite_signal", new_callable=AsyncMock) as mock_persist, \
             patch("services.signal_store.bus", mock_bus):
            task = asyncio.create_task(_bus_signal_listener())
            # Wait for the message to be processed
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert mock_persist.call_count >= 1

    asyncio.get_event_loop().run_until_complete(_test())


# 39
def test_bus_listener_ignores_non_matching_message_type():
    import services.signal_store as ss

    msg = {"type": "heartbeat", "data": {}}

    async def _test():
        mock_q = asyncio.Queue()
        await mock_q.put(msg)
        mock_bus = MagicMock()
        mock_bus.subscribe.return_value = mock_q
        mock_bus.unsubscribe            = MagicMock()

        with patch.object(ss, "persist_composite_signal", new_callable=AsyncMock) as mock_persist, \
             patch("services.signal_store.bus", mock_bus):
            task = asyncio.create_task(_bus_signal_listener())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert mock_persist.call_count == 0

    asyncio.get_event_loop().run_until_complete(_test())


# 40
def test_bus_listener_cancelled_error_unsubscribes():
    import services.signal_store as ss

    async def _test():
        mock_q = asyncio.Queue()  # empty — blocks immediately
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
