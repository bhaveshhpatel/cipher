"""
Regression tests for services/universe_screener.py

Note: universe_screener.screen_universe() is DEPRECATED as of Step 3 refactor.
Stream eligibility is now handled in symbols_loader._fetch_batch_quotes().
These tests cover the data structures and logic that remain in the file.

Covers:
  ScreenResult:
  - Default values: eligible=[], ineligible=[], priority=[], screened=0, duration_s=0.0
  - .total property = len(eligible) + len(ineligible)
  - .summary() returns dict with keys: eligible, ineligible, priority, screened,
    duration_s, source
  - source default is 'screener'

  screen_universe:
  - Empty list → returns ScreenResult with all empty lists, no HTTP call made
  - No TRADIER_API_KEY + UNIVERSE_STREAM_ELIGIBLE_DEFAULT=True → all candidates eligible
  - No TRADIER_API_KEY + UNIVERSE_STREAM_ELIGIBLE_DEFAULT=False → all candidates ineligible

  _nearest_expiry_param:
  - Returns a string in YYYY-MM-DD format
  - The returned date is always a Friday (weekday 4)
  - The returned date is always in the future

  get_stream_eligible:
  - Returns a list of strings (subset of input)
  - When all eligible, len(result) == len(input)
"""
import pytest
import re
from datetime import date
from unittest.mock import patch

from services.universe_screener import (
    ScreenResult,
    screen_universe,
    get_stream_eligible,
    _nearest_expiry_param,
)


# ── ScreenResult dataclass ─────────────────────────────────────────────────────

def test_screen_result_defaults():
    r = ScreenResult()
    assert r.eligible   == []
    assert r.ineligible == []
    assert r.priority   == []
    assert r.screened   == 0
    assert r.duration_s == 0.0
    assert r.source     == "screener"


def test_screen_result_total_property():
    r = ScreenResult(eligible=["AAPL", "TSLA"], ineligible=["FOO"])
    assert r.total == 3


def test_screen_result_total_both_empty():
    r = ScreenResult()
    assert r.total == 0


def test_screen_result_summary_has_all_keys():
    r = ScreenResult(eligible=["AAPL"], ineligible=["BAD"], screened=2, duration_s=1.5)
    s = r.summary()
    for key in ("eligible", "ineligible", "priority", "screened", "duration_s", "source"):
        assert key in s, f"summary() missing key '{key}'"


def test_screen_result_summary_eligible_count():
    r = ScreenResult(eligible=["AAPL", "TSLA", "NVDA"])
    assert r.summary()["eligible"] == 3


def test_screen_result_summary_source_is_screener():
    r = ScreenResult()
    assert r.summary()["source"] == "screener"


def test_screen_result_summary_duration_rounded_to_2dp():
    r = ScreenResult(duration_s=1.23456)
    assert r.summary()["duration_s"] == round(1.23456, 2)


# ── screen_universe ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_screen_universe_empty_list_returns_empty_result():
    result = await screen_universe([])
    assert isinstance(result, ScreenResult)
    assert result.eligible   == []
    assert result.ineligible == []
    assert result.screened   == 0


@pytest.mark.asyncio
async def test_screen_universe_no_api_key_all_eligible_when_default_true():
    """No TRADIER_API_KEY + UNIVERSE_STREAM_ELIGIBLE_DEFAULT=True → all candidates eligible."""
    symbols = ["AAPL", "TSLA", "NVDA"]
    with patch("services.universe_screener.settings") as ms:
        ms.TRADIER_API_KEY = ""
        ms.UNIVERSE_STREAM_ELIGIBLE_DEFAULT = True
        ms.priority_symbols = []   # no priority symbols
        result = await screen_universe(symbols)
    assert set(result.eligible) == set(symbols)
    assert result.ineligible == []


@pytest.mark.asyncio
async def test_screen_universe_no_api_key_all_ineligible_when_default_false():
    """No TRADIER_API_KEY + UNIVERSE_STREAM_ELIGIBLE_DEFAULT=False → all candidates ineligible."""
    symbols = ["AAPL", "TSLA", "NVDA"]
    with patch("services.universe_screener.settings") as ms:
        ms.TRADIER_API_KEY = ""
        ms.UNIVERSE_STREAM_ELIGIBLE_DEFAULT = False
        ms.priority_symbols = []
        result = await screen_universe(symbols)
    assert result.eligible   == []
    assert set(result.ineligible) == set(symbols)


# ── _nearest_expiry_param ──────────────────────────────────────────────────────

def test_nearest_expiry_param_format_yyyy_mm_dd():
    result = _nearest_expiry_param()
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", result), \
        f"Expected YYYY-MM-DD format, got: '{result}'"


def test_nearest_expiry_param_is_a_friday():
    result = _nearest_expiry_param()
    d = date.fromisoformat(result)
    assert d.weekday() == 4, f"Expected Friday (4), got weekday {d.weekday()}"


def test_nearest_expiry_param_is_in_the_future():
    today = date.today()
    result = _nearest_expiry_param()
    d = date.fromisoformat(result)
    assert d > today


# ── get_stream_eligible ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_stream_eligible_returns_list():
    symbols = ["AAPL", "TSLA"]
    with patch("services.universe_screener.settings") as ms:
        ms.TRADIER_API_KEY = ""
        ms.UNIVERSE_STREAM_ELIGIBLE_DEFAULT = True
        ms.priority_symbols = []
        result = await get_stream_eligible(symbols)
    assert isinstance(result, list)


@pytest.mark.asyncio
async def test_get_stream_eligible_all_eligible_when_default_true():
    symbols = ["AAPL", "TSLA", "NVDA", "SPY"]
    with patch("services.universe_screener.settings") as ms:
        ms.TRADIER_API_KEY = ""
        ms.UNIVERSE_STREAM_ELIGIBLE_DEFAULT = True
        ms.priority_symbols = []
        result = await get_stream_eligible(symbols)
    assert len(result) == len(symbols)
