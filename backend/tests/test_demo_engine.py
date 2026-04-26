"""
Regression tests for services/demo_engine.py

Covers (matched to actual source):
  _nearest_friday:
  - Returns a date whose weekday() == 4 (Friday)
  - Returns a future date (never today if today is not Friday, never past)
  - weeks=1 returns nearest Friday, weeks=2 returns one week later
  - Offset between weeks=1 and weeks=2 is exactly 7 days

  _round_to_strike:
  - price < 50 → increment 0.50 (e.g. 12.3 → 12.5, 10.7 → 11.0)
  - price in [50, 200) → increment 1.0 (e.g. 150.7 → 151.0)
  - price >= 200 → increment 5.0 (e.g. 500.0 → 500.0, 502.0 → 500.0)
  - Boundary: price == 50 → uses 1.0 increment
  - Boundary: price == 200 → uses 5.0 increment
  - Returns a float

  _build_occ_symbol:
  - ticker is left-padded to 6 chars (AAPL → 'AAPL  ')
  - expiry is formatted as YYMMDD
  - CALL → 'C', PUT → 'P'
  - 'call' (lowercase) → 'C'
  - strike * 1000 zero-padded to 8 digits (180.0 → '00180000')
  - full format: TICKER(6)YYMMDD(6)C/P(1)STRIKE_INT(8) = 21 chars

  _build_timesale_envelope:
  - returns a dict with keys: 'type', 'timesale'
  - type == 'timesale'
  - timesale contains 'symbol', 'last', 'bid', 'ask', 'size', 'exch', 'exchange', 'date'
  - bid < last < ask
  - both 'exch' and 'exchange' present (Tradier compat)
  - occ symbol embedded in timesale.symbol matches _build_occ_symbol

  Public API:
  - is_running() == False at module import time
  - get_stats() returns dict with keys: running, ticks_emitted, signals_emitted, errors
  - get_stats().running == False when not started
  - start_demo() returns {ok: True, status: 'started'} on first call
  - start_demo() returns {ok: True, status: 'already_running'} if already running
  - stop_demo() returns {ok: True, status: 'already_stopped'} when not running
  - start_demo() + stop_demo() → is_running() == False
  - stop_demo() after stop → status == 'already_stopped'
"""
import pytest
import asyncio
from datetime import date
from unittest.mock import patch

from services.demo_engine import (
    _nearest_friday,
    _round_to_strike,
    _build_occ_symbol,
    _build_timesale_envelope,
    is_running,
    get_stats,
    start_demo,
    stop_demo,
)


# ── test isolation: stop any running demo before each test ──────────────────

@pytest.fixture(autouse=True)
async def _stop_demo_after():
    """Ensure the demo engine is stopped after every test that starts it."""
    yield
    if is_running():
        await stop_demo()


# ── _nearest_friday ─────────────────────────────────────────────────────────

def test_nearest_friday_is_a_friday():
    result = _nearest_friday(1)
    assert result.weekday() == 4, f"Expected Friday (4), got weekday {result.weekday()}"


def test_nearest_friday_is_in_the_future():
    today = date.today()
    result = _nearest_friday(1)
    assert result > today


def test_nearest_friday_weeks_2_is_also_friday():
    result = _nearest_friday(2)
    assert result.weekday() == 4


def test_nearest_friday_weeks_2_is_7_days_after_weeks_1():
    f1 = _nearest_friday(1)
    f2 = _nearest_friday(2)
    assert (f2 - f1).days == 7


def test_nearest_friday_weeks_8_is_future():
    today = date.today()
    result = _nearest_friday(8)
    assert result > today


# ── _round_to_strike ───────────────────────────────────────────────────────

@pytest.mark.parametrize("price,expected", [
    (12.3,  12.5),
    (10.7,  11.0),
    (49.9,  50.0),
    (25.0,  25.0),
])
def test_round_to_strike_below_50(price, expected):
    assert _round_to_strike(price, "CALL") == expected


@pytest.mark.parametrize("price,expected", [
    (150.7, 151.0),
    (100.4, 100.0),
    (50.0,  50.0),
    (199.6, 200.0),
])
def test_round_to_strike_50_to_200(price, expected):
    assert _round_to_strike(price, "PUT") == expected


@pytest.mark.parametrize("price,expected", [
    (500.0, 500.0),
    (502.0, 500.0),
    (503.0, 505.0),
    (200.0, 200.0),
])
def test_round_to_strike_200_and_above(price, expected):
    assert _round_to_strike(price, "CALL") == expected


def test_round_to_strike_returns_float():
    result = _round_to_strike(150.0, "CALL")
    assert isinstance(result, float)


# ── _build_occ_symbol ───────────────────────────────────────────────────────

def test_build_occ_symbol_total_length_is_21():
    expiry = date(2026, 6, 20)
    result = _build_occ_symbol("AAPL", expiry, "CALL", 180.0)
    assert len(result) == 21, f"OCC symbol length should be 21, got {len(result)}: '{result}'"


def test_build_occ_symbol_ticker_padded_to_6():
    expiry = date(2026, 6, 20)
    result = _build_occ_symbol("AAPL", expiry, "CALL", 180.0)
    assert result[:6] == "AAPL  ", f"Expected 'AAPL  ', got '{result[:6]}'"


def test_build_occ_symbol_expiry_format_yymmdd():
    expiry = date(2026, 1, 17)
    result = _build_occ_symbol("AAPL", expiry, "CALL", 180.0)
    assert result[6:12] == "260117"


def test_build_occ_symbol_call_is_c():
    expiry = date(2026, 6, 20)
    result = _build_occ_symbol("AAPL", expiry, "CALL", 180.0)
    assert result[12] == "C"


def test_build_occ_symbol_put_is_p():
    expiry = date(2026, 6, 20)
    result = _build_occ_symbol("AAPL", expiry, "PUT", 180.0)
    assert result[12] == "P"


def test_build_occ_symbol_lowercase_call_accepted():
    expiry = date(2026, 6, 20)
    result = _build_occ_symbol("AAPL", expiry, "call", 180.0)
    assert result[12] == "C"


def test_build_occ_symbol_strike_zero_padded_to_8():
    expiry = date(2026, 6, 20)
    result = _build_occ_symbol("AAPL", expiry, "CALL", 180.0)
    assert result[13:] == "00180000"


def test_build_occ_symbol_short_ticker_padded():
    expiry = date(2026, 6, 20)
    result = _build_occ_symbol("SPY", expiry, "CALL", 500.0)
    assert result[:6] == "SPY   "


def test_build_occ_symbol_example_from_docstring():
    """Verify example from docstring: AAPL  260117C00180000"""
    expiry = date(2026, 1, 17)
    result = _build_occ_symbol("AAPL", expiry, "CALL", 180.0)
    assert result == "AAPL  260117C00180000"


# ── _build_timesale_envelope ───────────────────────────────────────────────

def _sample_envelope():
    return _build_timesale_envelope(
        ticker="AAPL",
        expiry=date(2026, 6, 20),
        ctype="CALL",
        strike=180.0,
        fill=3.50,
        size=10,
        exchange="C",
    )


def test_timesale_envelope_top_keys():
    env = _sample_envelope()
    assert "type" in env
    assert "timesale" in env


def test_timesale_envelope_type_is_timesale():
    env = _sample_envelope()
    assert env["type"] == "timesale"


def test_timesale_envelope_inner_keys():
    ts = _sample_envelope()["timesale"]
    for key in ("symbol", "last", "bid", "ask", "size", "exch", "exchange", "date"):
        assert key in ts, f"timesale dict missing key '{key}'"


def test_timesale_envelope_bid_lt_last_lt_ask():
    ts = _sample_envelope()["timesale"]
    assert ts["bid"] < ts["last"] < ts["ask"]


def test_timesale_envelope_both_exch_fields_present():
    """Both 'exch' (Tradier field) and 'exchange' (human-readable) must be present."""
    ts = _sample_envelope()["timesale"]
    assert ts["exch"] == "C"
    assert ts["exchange"] == "C"


def test_timesale_envelope_symbol_matches_build_occ_symbol():
    expiry = date(2026, 6, 20)
    expected_occ = _build_occ_symbol("AAPL", expiry, "CALL", 180.0)
    env = _build_timesale_envelope(
        ticker="AAPL", expiry=expiry, ctype="CALL",
        strike=180.0, fill=3.50, size=10, exchange="C"
    )
    assert env["timesale"]["symbol"] == expected_occ


def test_timesale_envelope_size_preserved():
    env = _build_timesale_envelope(
        ticker="SPY", expiry=date(2026, 6, 20), ctype="PUT",
        strike=500.0, fill=2.0, size=42, exchange="M"
    )
    assert env["timesale"]["size"] == 42


# ── Public API ───────────────────────────────────────────────────────────────

def test_is_running_false_at_import():
    assert not is_running()


def test_get_stats_has_required_keys():
    stats = get_stats()
    for key in ("running", "ticks_emitted", "signals_emitted", "errors"):
        assert key in stats, f"get_stats() missing key: '{key}'"


def test_get_stats_running_false_when_not_started():
    stats = get_stats()
    assert stats["running"] is False


@pytest.mark.asyncio
async def test_stop_demo_when_not_running_returns_already_stopped():
    if is_running():
        await stop_demo()
    result = await stop_demo()
    assert result == {"ok": True, "status": "already_stopped"}


@pytest.mark.asyncio
async def test_start_demo_returns_started():
    async def _noop_loop(tickers):
        await asyncio.sleep(9999)

    with patch("services.demo_engine._run_demo_loop", side_effect=_noop_loop):
        result = await start_demo()
    assert result == {"ok": True, "status": "started"}


@pytest.mark.asyncio
async def test_start_demo_idempotent_returns_already_running():
    async def _noop_loop(tickers):
        await asyncio.sleep(9999)

    with patch("services.demo_engine._run_demo_loop", side_effect=_noop_loop):
        await start_demo()
        result = await start_demo()
    assert result == {"ok": True, "status": "already_running"}


@pytest.mark.asyncio
async def test_start_then_stop_is_running_becomes_false():
    async def _noop_loop(tickers):
        try:
            await asyncio.sleep(9999)
        except asyncio.CancelledError:
            raise

    with patch("services.demo_engine._run_demo_loop", side_effect=_noop_loop):
        await start_demo()
        assert is_running()
        result = await stop_demo()

    assert result == {"ok": True, "status": "stopped"}
    assert not is_running()


@pytest.mark.asyncio
async def test_stop_after_stop_returns_already_stopped():
    async def _noop_loop(tickers):
        try:
            await asyncio.sleep(9999)
        except asyncio.CancelledError:
            raise

    with patch("services.demo_engine._run_demo_loop", side_effect=_noop_loop):
        await start_demo()
        await stop_demo()

    result = await stop_demo()
    assert result == {"ok": True, "status": "already_stopped"}
