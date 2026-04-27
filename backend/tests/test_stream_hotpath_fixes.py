"""
Tests for tradier_stream._process_trade hot-path fixes:
  - issue #4: persist_flow_event timeout (asyncio.wait_for 2s)
  - issue #5: sweep upgrade double-dispatch guard
  - accumulator async call sites correctly await ingest_tick / get_signal
"""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_raw_timesale(ticker="AAPL", occ="AAPL  260620C00180000", exchange="C"):
    return {
        "type": "timesale",
        "timesale": {
            "symbol":  occ,
            "price":   3.50,
            "bid":     3.40,
            "ask":     3.60,
            "size":    100,
            "exch":    exchange,
        },
    }


def _make_ev(ticker="AAPL"):
    return SimpleNamespace(
        ticker=ticker,
        contract_type="CALL",
        strike=180.0,
        expiry="2026-06-20",
        fill_price=3.50,
        bid=3.40,
        ask=3.60,
        size=100,
        premium=35_000.0,
        trade_type="BTO",
        bid_ask_class="ASK",
        is_aggressive=True,
        is_golden_sweep=False,
        sentiment="BULLISH",
        influence_tier="LARGE",
        conviction_score=0.75,
        exchange_count=1,
        fill_count=1,
        open_interest=5000,
        iv=0.35,
        underlying_price=178.0,
        is_synthetic_quote=False,
        dte=55,
        timestamp=datetime.utcnow(),
    )


# ---------------------------------------------------------------------------
# Issue #4: persist_flow_event timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_timeout_does_not_block_hotpath():
    """
    If persist_flow_event hangs beyond _PERSIST_TIMEOUT, _process_trade
    should catch the TimeoutError, increment _stats['errors'], and return
    without raising.
    """
    import services.tradier_stream as ts_mod

    ev = _make_ev()

    async def slow_persist(_):
        await asyncio.sleep(10)  # way beyond the 2s timeout

    mock_ep = MagicMock()
    mock_ep.ticker = "AAPL"
    mock_ep.contract_type = "CALL"
    mock_ep.is_accelerating = False
    mock_ep.total_premium = 200_000.0
    mock_ep.trade_count = 5
    mock_ep.summary_str.return_value = "5x CALL $180.0 exp 2026-06-20"

    errors_before = ts_mod._stats["errors"]

    with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
         patch("services.tradier_stream.flow_dedup.is_duplicate", return_value=False), \
         patch("services.tradier_stream.flow_dedup.is_sweep", return_value=False), \
         patch("services.tradier_stream.accumulator.ingest_tick", new_callable=AsyncMock, return_value=mock_ep), \
         patch("services.tradier_stream.accumulator.get_signal", new_callable=AsyncMock, return_value=None), \
         patch("services.tradier_stream.persist_flow_event", side_effect=slow_persist):

        raw = _make_raw_timesale()
        await ts_mod._process_trade(raw)

    assert ts_mod._stats["errors"] == errors_before + 1


@pytest.mark.asyncio
async def test_persist_success_does_not_increment_errors():
    """
    A fast persist_flow_event should not increment _stats['errors'].
    """
    import services.tradier_stream as ts_mod

    ev = _make_ev()

    mock_ep = MagicMock()
    mock_ep.ticker = "AAPL"
    mock_ep.contract_type = "CALL"
    mock_ep.is_accelerating = False
    mock_ep.total_premium = 200_000.0
    mock_ep.trade_count = 5
    mock_ep.summary_str.return_value = "5x CALL $180.0 exp 2026-06-20"

    errors_before = ts_mod._stats["errors"]

    with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
         patch("services.tradier_stream.flow_dedup.is_duplicate", return_value=False), \
         patch("services.tradier_stream.flow_dedup.is_sweep", return_value=False), \
         patch("services.tradier_stream.accumulator.ingest_tick", new_callable=AsyncMock, return_value=mock_ep), \
         patch("services.tradier_stream.accumulator.get_signal", new_callable=AsyncMock, return_value=None), \
         patch("services.tradier_stream.persist_flow_event", new_callable=AsyncMock):

        raw = _make_raw_timesale()
        await ts_mod._process_trade(raw)

    assert ts_mod._stats["errors"] == errors_before


# ---------------------------------------------------------------------------
# Issue #5: sweep upgrade double-dispatch guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sweep_upgrade_dispatched_only_once():
    """
    When two concurrent _process_trade calls both detect exch_count == sweep_min
    for the same key, upgrade_to_sweep_in_db should be create_task'd exactly once.
    """
    import services.tradier_stream as ts_mod

    # Clear the dispatch guard so this test is isolated
    ts_mod._sweep_upgrade_dispatched.clear()

    ev = _make_ev()
    raw = _make_raw_timesale(occ="AAPL  260620C00180000", exchange="C")

    upgrade_call_count = 0

    async def fake_upgrade(**_kwargs):
        nonlocal upgrade_call_count
        upgrade_call_count += 1

    with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
         patch("services.tradier_stream.flow_dedup.is_duplicate", return_value=True), \
         patch("services.tradier_stream.flow_dedup.get_exchange_count", return_value=3), \
         patch("services.tradier_stream.flow_dedup._sweep_min", 3), \
         patch("services.tradier_stream.upgrade_to_sweep_in_db", side_effect=fake_upgrade):

        # Fire 5 concurrent duplicate events on the same key
        await asyncio.gather(*[ts_mod._process_trade(dict(raw)) for _ in range(5)])
        # Drain any created tasks
        await asyncio.sleep(0)

    assert upgrade_call_count == 1, (
        f"upgrade_to_sweep_in_db should fire exactly once, got {upgrade_call_count}"
    )
