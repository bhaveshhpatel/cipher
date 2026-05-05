"""
tests/test_multiday_repeat.py

Unit tests for the ING-007 is_multi_day_repeat feature:
  1. _process_trade() sets is_multi_day_repeat=False when cache shows prior_days=0.
  2. _process_trade() sets is_multi_day_repeat=True when cache shows prior_days>=2.
  3. persist_flow_episode() receives is_multi_day_repeat in the row dict.

All external I/O (httpx, Supabase, bus) is mocked — no network calls.

ING-007 wiring notes (2026-05-04):
  Tests 1 & 2 patch utils.contract_day_cache._cache directly.
  The stream reads _cache synchronously (non-blocking) to set is_multi_day_repeat
  before the async lookback worker patches the DB row.

PBE-NEW-1 (2026-05-04):
  Tests for get_contract_prior_days() (previously tests 1-3) removed.
  That function was dead code — superseded by the async queue +
  contract_day_cache route before merge. No production call site exists.
"""
import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

def _make_mock_sig_ep(
    ticker="AAPL", contract_type="CALL", strike=150.0,
    expiry="2026-06-20", trade_count=5, total_premium=200_000.0,
):
    ep = MagicMock()
    ep.ticker = ticker
    ep.contract_type = contract_type
    ep.strike = strike
    ep.expiry = expiry
    ep.trade_count = trade_count
    ep.total_premium = total_premium
    ep.is_accelerating = False
    ep.dominant_direction = "REPEAT_BUY"
    return ep


def _make_mock_ev():
    ev = MagicMock()
    ev.ticker = "AAPL"
    ev.contract_type = "CALL"
    ev.strike = 150.0
    ev.expiry = "2026-06-20"
    ev.premium = 80_000.0
    ev.dte = 30
    ev.size = 100
    ev.fill_price = 2.50
    ev.bid = 2.45
    ev.ask = 2.55
    ev.trade_type = "BTO"
    ev.bid_ask_class = "AT_ASK"
    ev.is_aggressive = True
    ev.is_golden_sweep = False
    ev.sentiment = "BULLISH"
    ev.influence_tier = "INSTITUTIONAL"
    ev.conviction_score = 0.75
    ev.exchange_count = 1
    ev.fill_count = 1
    ev.open_interest = 5000
    ev.iv = 0.35
    ev.underlying_price = 148.0
    ev.is_synthetic_quote = False
    ev.timestamp = datetime.datetime(2026, 5, 4, 14, 0, 0)
    return ev


def _make_cache_entry(prior_days_active: int, fresh: bool = True):
    """
    Build a minimal LookbackResult-like stub that contract_day_cache._cache
    would hold.  The stream reads:
      entry.prior_days_active (int)
    and calls _is_fresh(entry) (patched via _lbc_fresh).
    """
    entry = MagicMock()
    entry.prior_days_active = prior_days_active
    entry.prior_days_aggressive = prior_days_active  # close enough for tests
    return entry, fresh


# ---------------------------------------------------------------------------
# Test 1: cache miss / prior_days=0  →  is_multi_day_repeat=False
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_trade_is_multi_day_repeat_false_when_prior_days_zero():
    """
    Cache returns prior_days_active=0 (first-day contract).
    is_multi_day_repeat must be False in persist_flow_episode() payload.
    enqueue_lookback() must be called exactly once.
    """
    sig_ep = _make_mock_sig_ep()
    ev     = _make_mock_ev()

    cache_entry, _ = _make_cache_entry(prior_days_active=0)

    import services.tradier_stream as ts
    ts._signal_last_emit.clear()
    ts._lookback_result_cache.clear()
    ts._stats["ticks"] = 0

    mock_cache = MagicMock()
    mock_cache.get = MagicMock(return_value=cache_entry)

    with patch("services.tradier_stream.persist_flow_event", new=AsyncMock()), \
         patch("services.tradier_stream.persist_flow_episode", new=AsyncMock()) as mock_persist_ep, \
         patch("services.tradier_stream.enqueue_lookback") as mock_enqueue, \
         patch("services.tradier_stream.bus") as mock_bus, \
         patch("services.tradier_stream.accumulator") as mock_acc, \
         patch("services.tradier_stream.flow_dedup") as mock_dedup, \
         patch("services.tradier_stream.build_composite", return_value=None), \
         patch("services.tradier_stream.is_directionally_aggressive", return_value=True), \
         patch("utils.contract_day_cache._cache", mock_cache), \
         patch("utils.contract_day_cache._is_fresh", return_value=True):

        mock_bus.publish_all = AsyncMock()
        mock_acc.ingest_tick     = AsyncMock(return_value=sig_ep)
        mock_acc.get_alert_level = MagicMock(return_value="CONVICTION")
        mock_dedup.is_duplicate  = MagicMock(return_value=False)
        mock_dedup.is_sweep      = MagicMock(return_value=False)

        raw = {"type": "timesale", "timesale": {
            "symbol": "AAPL260620C00150000",
            "last": "2.50", "bid": "2.45", "ask": "2.55",
            "size": "100", "exch": "C",
        }}

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev):
            await ts._process_trade(raw)

    assert mock_enqueue.called, "enqueue_lookback() was not called"
    assert mock_persist_ep.called, "persist_flow_episode() was not called"
    call_kwargs = mock_persist_ep.call_args[0][0]
    assert call_kwargs["is_multi_day_repeat"] is False


# ---------------------------------------------------------------------------
# Test 2: cache hit / prior_days>=2  →  is_multi_day_repeat=True
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_trade_is_multi_day_repeat_true_when_prior_days_positive():
    """
    Cache returns prior_days_active=2 (repeat contract, meets min_days=2 default).
    is_multi_day_repeat must be True in persist_flow_episode() payload
    AND in the signal bus message.
    enqueue_lookback() must be called exactly once.
    """
    sig_ep = _make_mock_sig_ep()
    ev     = _make_mock_ev()

    cache_entry, _ = _make_cache_entry(prior_days_active=2)

    published: list = []

    import services.tradier_stream as ts
    ts._signal_last_emit.clear()
    ts._lookback_result_cache.clear()
    ts._stats["ticks"] = 0

    mock_cache = MagicMock()
    mock_cache.get = MagicMock(return_value=cache_entry)

    with patch("services.tradier_stream.persist_flow_event", new=AsyncMock()), \
         patch("services.tradier_stream.persist_flow_episode", new=AsyncMock()) as mock_persist_ep, \
         patch("services.tradier_stream.enqueue_lookback") as mock_enqueue, \
         patch("services.tradier_stream.bus") as mock_bus, \
         patch("services.tradier_stream.accumulator") as mock_acc, \
         patch("services.tradier_stream.flow_dedup") as mock_dedup, \
         patch("services.tradier_stream.build_composite", return_value=None), \
         patch("services.tradier_stream.is_directionally_aggressive", return_value=True), \
         patch("utils.contract_day_cache._cache", mock_cache), \
         patch("utils.contract_day_cache._is_fresh", return_value=True):

        mock_bus.publish_all = AsyncMock(side_effect=lambda m: published.append(m))
        mock_acc.ingest_tick     = AsyncMock(return_value=sig_ep)
        mock_acc.get_alert_level = MagicMock(return_value="CONVICTION")
        mock_dedup.is_duplicate  = MagicMock(return_value=False)
        mock_dedup.is_sweep      = MagicMock(return_value=False)

        raw = {"type": "timesale", "timesale": {
            "symbol": "AAPL260620C00150000",
            "last": "2.50", "bid": "2.45", "ask": "2.55",
            "size": "100", "exch": "C",
        }}

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev):
            await ts._process_trade(raw)

    assert mock_enqueue.called, "enqueue_lookback() was not called"
    assert mock_persist_ep.called, "persist_flow_episode() was not called"
    call_kwargs = mock_persist_ep.call_args[0][0]
    assert call_kwargs["is_multi_day_repeat"] is True

    signal_msgs = [m for m in published if m.get("type") == "signal"]
    assert signal_msgs, "No signal published"
    assert signal_msgs[0]["data"]["is_multi_day_repeat"] is True


# ---------------------------------------------------------------------------
# Test 3: persist_flow_episode row contains is_multi_day_repeat
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persist_flow_episode_writes_is_multi_day_repeat():
    """persist_flow_episode() must include is_multi_day_repeat in the inserted row."""
    captured_rows: list = []

    async def fake_insert(table, rows):
        if table == "flow_episodes":
            captured_rows.extend(rows)
        return True

    with patch("services.flow_store._SUPABASE_URL", "https://fake.supabase.co"), \
         patch("services.flow_store._SUPABASE_KEY", "fake-key"), \
         patch("services.flow_store._insert_rows", new=fake_insert):

        from services.flow_store import persist_flow_episode
        await persist_flow_episode({
            "ticker":              "AAPL",
            "direction":           "REPEAT_BUY",
            "contract_type":       "CALL",
            "strike":              150.0,
            "expiry":              "2026-06-20",
            "total_premium":       200_000.0,
            "trade_count":         5,
            "alert_level":         "CONVICTION",
            "is_accelerating":     False,
            "is_multi_day_repeat": True,
            "seed_episode":        "AAPL CALL $150 2026-06-20 trades=5 prem=$200,000",
            "timestamp":           "2026-05-04T14:00:00",
        })

    assert captured_rows, "No rows were inserted into flow_episodes"
    assert captured_rows[0]["is_multi_day_repeat"] is True
