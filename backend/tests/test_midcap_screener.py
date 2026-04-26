"""
Regression tests for signals/midcap_screener.py

Covers (matched to actual source):
  is_midcap:
  - Returns True for every ticker in KNOWN_MIDCAP
  - Case-insensitive: 'pltr', 'Pltr', 'PLTR' all return True
  - Returns False for tickers not in KNOWN_MIDCAP (AAPL, MSFT, unknown)
  - Empty string returns False

  unusual_oi_ratio:
  - Returns float rounded to 3 decimal places
  - size / open_interest = correct ratio
  - open_interest == 0 returns 0.0 (no division by zero)
  - open_interest < 0 returns 0.0
  - size == 0 returns 0.0
  - Large values computed correctly

  is_unusual_activity:
  - ratio >= default threshold (0.10) returns True
  - ratio < default threshold returns False
  - ratio == threshold exactly returns True (>= not >)
  - Custom threshold respected
  - open_interest == 0 always returns False (ratio is 0.0)
  - open_interest < 0 always returns False
"""
import pytest
from signals.midcap_screener import (
    is_midcap,
    unusual_oi_ratio,
    is_unusual_activity,
    KNOWN_MIDCAP,
)


# ── is_midcap ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ticker", list(KNOWN_MIDCAP))
def test_is_midcap_known_tickers_return_true(ticker):
    assert is_midcap(ticker) is True


@pytest.mark.parametrize("ticker", ["pltr", "Pltr", "PLTR", "sofi", "CRWD"])
def test_is_midcap_case_insensitive(ticker):
    assert is_midcap(ticker) is True


@pytest.mark.parametrize("ticker", ["AAPL", "MSFT", "TSLA", "UNKNOWN", "XYZ"])
def test_is_midcap_unknown_tickers_return_false(ticker):
    assert is_midcap(ticker) is False


def test_is_midcap_empty_string_returns_false():
    assert is_midcap("") is False


# ── unusual_oi_ratio ──────────────────────────────────────────────────────────

def test_unusual_oi_ratio_happy_path():
    # 100 / 1000 = 0.1
    assert unusual_oi_ratio(100, 1000) == 0.1


def test_unusual_oi_ratio_rounded_to_3dp():
    # 1 / 3 = 0.33333... → 0.333
    result = unusual_oi_ratio(1, 3)
    assert result == 0.333


def test_unusual_oi_ratio_zero_oi_returns_zero():
    assert unusual_oi_ratio(500, 0) == 0.0


def test_unusual_oi_ratio_negative_oi_returns_zero():
    assert unusual_oi_ratio(500, -100) == 0.0


def test_unusual_oi_ratio_zero_size_returns_zero():
    assert unusual_oi_ratio(0, 1000) == 0.0


def test_unusual_oi_ratio_large_values():
    result = unusual_oi_ratio(10_000, 50_000)
    assert result == 0.2


def test_unusual_oi_ratio_returns_float():
    result = unusual_oi_ratio(100, 1000)
    assert isinstance(result, float)


# ── is_unusual_activity ────────────────────────────────────────────────────────

def test_is_unusual_activity_above_threshold_returns_true():
    # 200 / 1000 = 0.2 >= 0.10
    assert is_unusual_activity(200, 1000) is True


def test_is_unusual_activity_below_threshold_returns_false():
    # 50 / 1000 = 0.05 < 0.10
    assert is_unusual_activity(50, 1000) is False


def test_is_unusual_activity_at_exact_threshold_returns_true():
    """Threshold is >=, so exact match must be True."""
    # 100 / 1000 = 0.1 == threshold
    assert is_unusual_activity(100, 1000, threshold=0.10) is True


def test_is_unusual_activity_custom_threshold():
    # 300 / 1000 = 0.3 >= 0.25
    assert is_unusual_activity(300, 1000, threshold=0.25) is True
    # 200 / 1000 = 0.2 < 0.25
    assert is_unusual_activity(200, 1000, threshold=0.25) is False


def test_is_unusual_activity_zero_oi_always_false():
    """Zero OI → ratio is 0.0 → never unusual regardless of size."""
    assert is_unusual_activity(9999, 0) is False


def test_is_unusual_activity_negative_oi_always_false():
    assert is_unusual_activity(9999, -1) is False


# ── KNOWN_MIDCAP sanity ──────────────────────────────────────────────────────────

def test_known_midcap_is_a_set():
    assert isinstance(KNOWN_MIDCAP, set)


def test_known_midcap_not_empty():
    assert len(KNOWN_MIDCAP) > 0


def test_known_midcap_contains_expected_names():
    for ticker in ("PLTR", "SOFI", "CRWD", "DDOG", "NET"):
        assert ticker in KNOWN_MIDCAP
