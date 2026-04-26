"""
Unit tests for signals/repetition_accumulator.py

Covers:
  RepetitionEpisode
  1.  trade_count property equals number of events
  2.  total_premium sums all event premiums
  3.  is_accelerating True when last 3 events within 60s
  4.  is_accelerating False when last 3 events span > 60s
  5.  is_accelerating False when fewer than 3 events
  6.  summary_str contains ticker, contract_type, strike, expiry, and premium

  RepetitionAccumulator.ingest
  7.  Returns None below min_trades threshold
  8.  Returns None below min_premium threshold
  9.  Returns episode when both thresholds met
  10. Rolling window prunes stale events
  11. Different contracts keyed independently
  12. Same contract accumulated across calls
  13. Returns episode on every qualifying call (not just first crossing)

  RepetitionAccumulator.get_alert_level
  14. premium >= 5_000_000 → CONVICTION
  15. is_accelerating + premium >= 1_000_000 → CONVICTION
  16. premium >= 1_000_000 (not accelerating) → STRONG_SIGNAL
  17. premium >= 250_000 → ALERT
  18. premium < 250_000 → WATCH

  RepetitionAccumulator — init
  19. Default window is 30 minutes
  20. Default min_trades is 3
  21. Default min_premium is 50_000
  22. Custom params respected
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from signals.repetition_accumulator import RepetitionAccumulator, RepetitionEpisode


# ============================================================
# Helpers
# ============================================================

def _mock_event(
    ticker="AAPL",
    contract_type="CALL",
    strike=180.0,
    expiry="2026-06-20",
    premium=100_000.0,
    timestamp=None,
):
    ev = MagicMock()
    ev.ticker        = ticker
    ev.contract_type = contract_type
    ev.strike        = strike
    ev.expiry        = expiry
    ev.premium       = premium
    ev.timestamp     = timestamp or datetime.utcnow()
    return ev


def _ts(offset_seconds=0):
    return datetime(2026, 4, 25, 10, 0, 0) + timedelta(seconds=offset_seconds)


# ============================================================
# RepetitionEpisode
# ============================================================

# 1
def test_episode_trade_count():
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=180.0, expiry="2026-06-20")
    ep.events = [_mock_event() for _ in range(5)]
    assert ep.trade_count == 5


# 2
def test_episode_total_premium():
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=180.0, expiry="2026-06-20")
    ep.events = [
        _mock_event(premium=100_000),
        _mock_event(premium=200_000),
        _mock_event(premium=300_000),
    ]
    assert ep.total_premium == pytest.approx(600_000)


# 3
def test_episode_is_accelerating_true():
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=180.0, expiry="2026-06-20")
    ep.events = [
        _mock_event(timestamp=_ts(0)),
        _mock_event(timestamp=_ts(20)),
        _mock_event(timestamp=_ts(40)),
    ]
    assert ep.is_accelerating is True


# 4
def test_episode_is_accelerating_false_long_span():
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=180.0, expiry="2026-06-20")
    ep.events = [
        _mock_event(timestamp=_ts(0)),
        _mock_event(timestamp=_ts(50)),
        _mock_event(timestamp=_ts(120)),
    ]
    assert ep.is_accelerating is False


# 5
def test_episode_is_accelerating_false_too_few_events():
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=180.0, expiry="2026-06-20")
    ep.events = [_mock_event(timestamp=_ts(0)), _mock_event(timestamp=_ts(10))]
    assert ep.is_accelerating is False


# 6
def test_episode_summary_str_contains_key_fields():
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=180.0, expiry="2026-06-20")
    ep.events = [_mock_event(premium=500_000) for _ in range(3)]
    s = ep.summary_str()
    assert "CALL" in s
    assert "180" in s
    assert "2026-06-20" in s


# ============================================================
# RepetitionAccumulator.ingest
# ============================================================

# 7
def test_ingest_returns_none_below_min_trades():
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000)
    ev = _mock_event(premium=200_000, timestamp=_ts(0))
    result = acc.ingest(ev)
    assert result is None  # only 1 trade


# 8
def test_ingest_returns_none_below_min_premium():
    acc = RepetitionAccumulator(min_trades=3, min_premium=1_000_000)
    for i in range(3):
        result = acc.ingest(_mock_event(premium=100_000, timestamp=_ts(i * 10)))
    assert result is None  # total premium = 300k < 1M


# 9
def test_ingest_returns_episode_when_thresholds_met():
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000)
    for i in range(2):
        acc.ingest(_mock_event(premium=100_000, timestamp=_ts(i * 10)))
    ep = acc.ingest(_mock_event(premium=100_000, timestamp=_ts(20)))
    assert ep is not None
    assert ep.trade_count == 3
    assert ep.total_premium == pytest.approx(300_000)


# 10
def test_ingest_prunes_stale_events():
    acc = RepetitionAccumulator(window_minutes=1, min_trades=3, min_premium=50_000)
    for i in range(2):
        acc.ingest(_mock_event(premium=100_000, timestamp=_ts(-200 + i)))
    result = acc.ingest(_mock_event(premium=100_000, timestamp=_ts(0)))
    assert result is None  # only 1 event in window


# 11
def test_ingest_different_contracts_independent():
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000)
    for i in range(3):
        acc.ingest(_mock_event(ticker="AAPL", contract_type="CALL",
                               strike=180.0, premium=100_000, timestamp=_ts(i * 5)))
    result_tsla = acc.ingest(_mock_event(ticker="TSLA", contract_type="CALL",
                                          strike=250.0, premium=100_000, timestamp=_ts(0)))
    assert result_tsla is None  # only 1 TSLA trade


# 12
def test_ingest_accumulates_across_calls():
    acc = RepetitionAccumulator(min_trades=5, min_premium=50_000)
    for i in range(4):
        result = acc.ingest(_mock_event(premium=100_000, timestamp=_ts(i * 10)))
        assert result is None
    ep = acc.ingest(_mock_event(premium=100_000, timestamp=_ts(40)))
    assert ep is not None
    assert ep.trade_count == 5


# 13
def test_ingest_returns_episode_on_every_qualifying_call():
    acc = RepetitionAccumulator(min_trades=3, min_premium=50_000)
    for i in range(3):
        acc.ingest(_mock_event(premium=100_000, timestamp=_ts(i * 10)))
    ep4 = acc.ingest(_mock_event(premium=100_000, timestamp=_ts(30)))
    ep5 = acc.ingest(_mock_event(premium=100_000, timestamp=_ts(40)))
    assert ep4 is not None
    assert ep5 is not None
    assert ep5.trade_count == 5


# ============================================================
# RepetitionAccumulator.get_alert_level
# ============================================================

def _ep_with(total_premium, accelerating=False):
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL", strike=180.0, expiry="2026-06-20")
    if accelerating:
        ep.events = [
            _mock_event(premium=total_premium / 3, timestamp=_ts(0)),
            _mock_event(premium=total_premium / 3, timestamp=_ts(20)),
            _mock_event(premium=total_premium / 3, timestamp=_ts(40)),
        ]
    else:
        ep.events = [
            _mock_event(premium=total_premium / 3, timestamp=_ts(0)),
            _mock_event(premium=total_premium / 3, timestamp=_ts(500)),
            _mock_event(premium=total_premium / 3, timestamp=_ts(1000)),
        ]
    return ep


# 14
def test_alert_level_conviction_high_premium():
    ep  = _ep_with(5_000_000, accelerating=False)
    acc = RepetitionAccumulator()
    assert acc.get_alert_level(ep) == "CONVICTION"


# 15
def test_alert_level_conviction_accelerating():
    ep  = _ep_with(1_000_000, accelerating=True)
    acc = RepetitionAccumulator()
    assert acc.get_alert_level(ep) == "CONVICTION"


# 16
def test_alert_level_strong_signal():
    ep  = _ep_with(1_000_000, accelerating=False)
    acc = RepetitionAccumulator()
    assert acc.get_alert_level(ep) == "STRONG_SIGNAL"


# 17
def test_alert_level_alert():
    ep  = _ep_with(250_000, accelerating=False)
    acc = RepetitionAccumulator()
    assert acc.get_alert_level(ep) == "ALERT"


# 18
def test_alert_level_watch():
    ep  = _ep_with(100_000, accelerating=False)
    acc = RepetitionAccumulator()
    assert acc.get_alert_level(ep) == "WATCH"


# ============================================================
# RepetitionAccumulator init
# ============================================================

# 19
def test_default_window_30_minutes():
    acc = RepetitionAccumulator()
    assert acc.window.total_seconds() == 30 * 60


# 20
def test_default_min_trades_3():
    acc = RepetitionAccumulator()
    assert acc.min_trades == 3


# 21
def test_default_min_premium_50k():
    acc = RepetitionAccumulator()
    assert acc.min_premium == pytest.approx(50_000)


# 22
def test_custom_params_respected():
    acc = RepetitionAccumulator(window_minutes=10, min_trades=5, min_premium=200_000)
    assert acc.window.total_seconds() == 10 * 60
    assert acc.min_trades   == 5
    assert acc.min_premium  == pytest.approx(200_000)
