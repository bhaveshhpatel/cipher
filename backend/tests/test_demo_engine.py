"""
Regression tests for services/demo_engine.py

Covers:
  - _nearest_friday always returns a Friday
  - _round_to_strike uses correct increment for price ranges
  - _build_occ_symbol produces correct OCC format
  - _build_timesale_envelope produces correct Tradier envelope shape
  - _build_timesale_envelope sets both 'exch' and 'exchange' fields (C-019)
  - is_running() returns False initially
  - get_stats() returns all expected keys
  - start_demo() is idempotent when already running
  - stop_demo() is idempotent when not running
  - start_demo() resets stats counters
  - start_demo() + stop_demo() lifecycle
"""
import asyncio
import pytest
from datetime import date
from unittest.mock import patch, AsyncMock

from services import demo_engine as de


# ── _nearest_friday ────────────────────────────────────────────────────────────

def test_nearest_friday_is_a_friday():
    for weeks in range(1, 9):
        d = de._nearest_friday(weeks)
        assert d.weekday() == 4, f"Expected Friday (weekday 4), got {d.strftime('%A')} for weeks={weeks}"


def test_nearest_friday_is_in_the_future():
    assert de._nearest_friday(1) >= date.today()


# ── _round_to_strike ──────────────────────────────────────────────────────────

def test_round_to_strike_sub_50_uses_50_cent_increments():
    """Prices below $50 should produce strikes at $0.50 increments."""
    for _ in range(20):  # run multiple times to cover random.choice
        strike = de._round_to_strike(30.0, "CALL")
        # Should be divisible by 0.5
        assert round(strike * 2) == strike * 2, f"Strike {strike} is not a 0.50 increment"


def test_round_to_strike_50_to_200_uses_1_dollar_increments():
    for _ in range(20):
        strike = de._round_to_strike(150.0, "CALL")
        assert round(strike) == strike, f"Strike {strike} is not a whole dollar"


def test_round_to_strike_above_200_uses_5_dollar_increments():
    for _ in range(20):
        strike = de._round_to_strike(500.0, "CALL")
        assert round(strike / 5) == strike / 5, f"Strike {strike} is not a $5 increment"


# ── _build_occ_symbol ──────────────────────────────────────────────────────────

def test_build_occ_symbol_call_format():
    expiry = date(2026, 6, 20)
    sym = de._build_occ_symbol("AAPL", expiry, "CALL", 195.0)
    # AAPL  260620C00195000
    assert sym.startswith("AAPL  ")
    assert "260620" in sym
    assert "C" in sym
    assert sym.endswith("00195000")


def test_build_occ_symbol_put_format():
    expiry = date(2026, 6, 20)
    sym = de._build_occ_symbol("TSLA", expiry, "PUT", 245.0)
    assert sym.startswith("TSLA  ")
    assert "P" in sym
    assert sym.endswith("00245000")


def test_build_occ_symbol_strike_precision():
    """Strike 375.5 → should encode as 00375500 (strike * 1000)."""
    expiry = date(2026, 6, 20)
    sym = de._build_occ_symbol("SPY", expiry, "CALL", 375.5)
    assert "00375500" in sym


def test_build_occ_symbol_short_ticker_padded_to_6():
    expiry = date(2026, 6, 20)
    sym = de._build_occ_symbol("SPY", expiry, "CALL", 500.0)
    # Ticker field is first 6 chars — 'SPY   '
    assert sym[:6] == "SPY   "


# ── _build_timesale_envelope ───────────────────────────────────────────────────────

def test_build_timesale_envelope_shape():
    env = de._build_timesale_envelope(
        occ_symbol="AAPL  260620C00195000",
        ticker="AAPL",
        fill=3.45,
        bid=3.40,
        ask=3.50,
        size=100,
        exchange="C",
        ts_ms=1745529600000,
    )
    assert env["type"] == "timesale"
    ts = env["timesale"]
    assert ts["symbol"] == "AAPL  260620C00195000"
    assert ts["last"] == 3.45
    assert ts["bid"] == 3.40
    assert ts["ask"] == 3.50
    assert ts["size"] == 100
    assert ts["date"] == 1745529600000


def test_build_timesale_envelope_has_both_exch_and_exchange_fields():
    """C-019: Both 'exch' (primary) and 'exchange' (alias) must be present."""
    env = de._build_timesale_envelope(
        occ_symbol="SPY   260620C00500000",
        ticker="SPY",
        fill=2.10, bid=2.05, ask=2.15,
        size=50, exchange="N", ts_ms=1000000,
    )
    ts = env["timesale"]
    assert "exch" in ts
    assert "exchange" in ts
    assert ts["exch"] == "N"
    assert ts["exchange"] == "N"


# ── public API: is_running / get_stats ──────────────────────────────────────────

def test_is_running_false_initially():
    # Reset global state
    de._demo_running = False
    de._demo_task = None
    assert de.is_running() is False


def test_get_stats_returns_expected_keys():
    stats = de.get_stats()
    for key in ("running", "ticks_emitted", "signals_generated", "last_ticker", "started_at"):
        assert key in stats, f"Missing key: {key}"


# ── start_demo / stop_demo lifecycle ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_demo_sets_running_true():
    de._demo_running = False
    de._demo_task = None
    # Patch _run_demo_loop so it doesn't actually process trades
    async def _fake_loop():
        await asyncio.sleep(9999)

    with patch("services.demo_engine._run_demo_loop", new=AsyncMock(side_effect=_fake_loop)):
        result = await de.start_demo()
    assert result["ok"] is True
    assert de.is_running() is True
    # Cleanup
    await de.stop_demo()


@pytest.mark.asyncio
async def test_start_demo_is_idempotent():
    """Calling start_demo() when already running must return ok=False."""
    de._demo_running = True
    de._demo_task = asyncio.create_task(asyncio.sleep(9999))
    result = await de.start_demo()
    assert result["ok"] is False
    assert "already" in result["message"].lower()
    # Cleanup
    de._demo_task.cancel()
    de._demo_running = False


@pytest.mark.asyncio
async def test_stop_demo_is_idempotent():
    """Calling stop_demo() when not running must return ok=False."""
    de._demo_running = False
    de._demo_task = None
    result = await de.stop_demo()
    assert result["ok"] is False
    assert "not running" in result["message"].lower()


@pytest.mark.asyncio
async def test_start_and_stop_demo_lifecycle():
    """Full start → running → stop → not running cycle."""
    de._demo_running = False
    de._demo_task = None

    async def _fake_loop():
        await asyncio.sleep(9999)

    with patch("services.demo_engine._run_demo_loop", new=AsyncMock(side_effect=_fake_loop)):
        start_result = await de.start_demo()
    assert start_result["ok"] is True

    stop_result = await de.stop_demo()
    assert stop_result["ok"] is True
    assert de.is_running() is False


@pytest.mark.asyncio
async def test_start_demo_resets_stats():
    """start_demo() must zero the tick/signal counters."""
    de._demo_stats["ticks_emitted"] = 999
    de._demo_stats["signals_generated"] = 42
    de._demo_running = False
    de._demo_task = None

    async def _fake_loop():
        await asyncio.sleep(9999)

    with patch("services.demo_engine._run_demo_loop", new=AsyncMock(side_effect=_fake_loop)):
        await de.start_demo()

    assert de._demo_stats["ticks_emitted"] == 0
    assert de._demo_stats["signals_generated"] == 0
    await de.stop_demo()
