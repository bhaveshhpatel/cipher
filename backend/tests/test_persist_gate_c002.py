"""
C-002 — Persist Gate: persist_flow_event() fires only AFTER accumulator threshold.

Fix verified:
  Before fix: persist_flow_event() called before accumulator.ingest().
              Every dedup-passing tick wrote to flow_events, including
              sub-threshold retail noise.
  After fix:  accumulator.ingest_tick() called first. If ep is None (threshold
              not yet crossed), function returns early — no DB write.
              persist_flow_event() only fires for qualifying ticks.
              Signal (bus) only fires when accumulator.get_signal() passes
              the C-007 cooldown gate.

Test IDs:
  C002-1  sub-threshold ticks do NOT call persist_flow_event
  C002-2  threshold-crossing tick DOES call persist_flow_event
  C002-3  persist is called on every qualifying tick after threshold is crossed
  C002-4  persist receives correct ev fields (ticker, premium, occ_symbol etc.)
  C002-5  deduped events still never reach persist_flow_event
  C002-6  order guarantee — ingest_tick always called before persist on qualifying tick
  C002-7  episode=None early-return path skips persist entirely (no await)
  C002-8  regression — signal and bus.publish_all still fires after threshold

Run: pytest backend/tests/test_persist_gate_c002.py -v
"""
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_ev(
    ticker="TSLA", contract_type="CALL", strike=375.0,
    premium=200_000.0, size=50, fill_price=4.0,
    bid=3.95, ask=4.05, trade_type="BTO",
    bid_ask_class="ASK", is_aggressive=True,
    is_golden_sweep=False, sentiment="BULLISH",
    influence_tier="INSTITUTIONAL", conviction_score=0.72,
    exchange_count=1, fill_count=1, open_interest=12000,
    iv=0.45, underlying_price=370.0, dte=14,
    expiry="2026-05-16", is_synthetic_quote=False,
):
    import datetime
    ev = MagicMock()
    ev.ticker = ticker
    ev.contract_type = contract_type
    ev.strike = strike
    ev.premium = premium
    ev.size = size
    ev.fill_price = fill_price
    ev.bid = bid
    ev.ask = ask
    ev.trade_type = trade_type
    ev.bid_ask_class = bid_ask_class
    ev.is_aggressive = is_aggressive
    ev.is_golden_sweep = is_golden_sweep
    ev.sentiment = sentiment
    ev.influence_tier = influence_tier
    ev.conviction_score = conviction_score
    ev.exchange_count = exchange_count
    ev.fill_count = fill_count
    ev.open_interest = open_interest
    ev.iv = iv
    ev.underlying_price = underlying_price
    ev.dte = dte
    ev.expiry = expiry
    ev.is_synthetic_quote = is_synthetic_quote
    ev.timestamp = datetime.datetime(2026, 4, 26, 14, 30, 0)
    return ev


def _make_ep(ticker="TSLA", contract_type="CALL", trade_count=3, total_premium=600_000.0):
    ep = MagicMock()
    ep.ticker = ticker
    ep.contract_type = contract_type
    ep.strike = 375.0
    ep.expiry = "2026-05-16"
    ep.trade_count = trade_count
    ep.total_premium = total_premium
    ep.is_accelerating = False
    ep.summary_str.return_value = f"{ticker} {contract_type} x{trade_count}"
    return ep


def _make_raw(occ="TSLA  260516C00375000", exchange="C"):
    return {
        "type": "timesale",
        "timesale": {
            "symbol": occ,
            "exch": exchange,
            "last": 4.0,
            "bid": 3.95,
            "ask": 4.05,
            "size": 50,
            "date": 1745686200000,
        },
    }


class TestC002SubThreshold:
    @pytest.mark.asyncio
    async def test_c002_1_no_persist_below_threshold(self):
        from services import tradier_stream as ts

        ev = _make_ev()
        raw = _make_raw()

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", new_callable=AsyncMock) as mock_persist, \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick = AsyncMock(return_value=None)
            mock_acc.get_signal = AsyncMock(return_value=None)
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        mock_acc.ingest_tick.assert_called_once_with(ev)
        mock_persist.assert_not_called()


class TestC002ThresholdCrossing:
    @pytest.mark.asyncio
    async def test_c002_2_persist_on_threshold_crossing(self):
        from services import tradier_stream as ts

        ev = _make_ev()
        ep = _make_ep()
        raw = _make_raw()

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", new_callable=AsyncMock) as mock_persist, \
             patch("services.tradier_stream.build_composite", return_value=None), \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick = AsyncMock(return_value=ep)
            mock_acc.get_signal = AsyncMock(return_value=ep)
            mock_acc.get_alert_level.return_value = "ALERT"
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        mock_persist.assert_awaited_once()


class TestC002SubsequentQualifyingTicks:
    @pytest.mark.asyncio
    async def test_c002_3_persist_on_every_qualifying_tick(self):
        from services import tradier_stream as ts

        ep = _make_ep()
        raw = _make_raw()

        with patch("services.tradier_stream.parse_tradier_trade", return_value=_make_ev()), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", new_callable=AsyncMock) as mock_persist, \
             patch("services.tradier_stream.build_composite", return_value=None), \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick = AsyncMock(return_value=ep)
            mock_acc.get_signal = AsyncMock(return_value=None)
            mock_acc.get_alert_level.return_value = "WATCH"
            mock_bus.publish_all = AsyncMock()

            for _ in range(5):
                await ts._process_trade(raw)

        assert mock_persist.await_count == 5


class TestC002PersistPayload:
    @pytest.mark.asyncio
    async def test_c002_4_persist_receives_correct_fields(self):
        from services import tradier_stream as ts

        ev = _make_ev(ticker="NVDA", premium=500_000.0, strike=900.0)
        ep = _make_ep(ticker="NVDA")
        raw = _make_raw(occ="NVDA  260516C00900000", exchange="Q")

        captured = {}

        async def _capture(d):
            captured.update(d)

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", side_effect=_capture), \
             patch("services.tradier_stream.build_composite", return_value=None), \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick = AsyncMock(return_value=ep)
            mock_acc.get_signal = AsyncMock(return_value=ep)
            mock_acc.get_alert_level.return_value = "STRONG_SIGNAL"
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        assert captured["ticker"] == "NVDA"
        assert captured["premium"] == 500_000.0
        assert captured["strike"] == 900.0
        assert captured["occ_symbol"] == "NVDA  260516C00900000"


class TestC002DedupedNeverPersist:
    @pytest.mark.asyncio
    async def test_c002_5_deduped_event_no_persist(self):
        from services import tradier_stream as ts

        ev = _make_ev()
        raw = _make_raw()

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", new_callable=AsyncMock) as mock_persist:

            mock_dedup.is_duplicate.return_value = True

            await ts._process_trade(raw)

        mock_acc.ingest_tick.assert_not_called()
        mock_persist.assert_not_called()


class TestC002OrderGuarantee:
    @pytest.mark.asyncio
    async def test_c002_6_ingest_before_persist(self):
        from services import tradier_stream as ts

        ev = _make_ev()
        ep = _make_ep()
        raw = _make_raw()
        call_order = []

        async def _ingest_tick(e):
            call_order.append("ingest_tick")
            return ep

        async def _persist(d):
            call_order.append("persist")

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", side_effect=_persist), \
             patch("services.tradier_stream.build_composite", return_value=None), \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick = _ingest_tick
            mock_acc.get_signal = AsyncMock(return_value=ep)
            mock_acc.get_alert_level.return_value = "WATCH"
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        assert call_order == ["ingest_tick", "persist"], f"Expected ingest_tick before persist, got: {call_order}"


class TestC002EarlyReturn:
    @pytest.mark.asyncio
    async def test_c002_7_none_episode_early_return(self):
        from services import tradier_stream as ts

        ev = _make_ev()
        raw = _make_raw()
        persist_called = []

        async def _persist(d):
            persist_called.append(d)

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", side_effect=_persist), \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick = AsyncMock(return_value=None)
            mock_acc.get_signal = AsyncMock(return_value=None)
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        assert len(persist_called) == 0, f"persist_flow_event called {len(persist_called)} time(s) but should be 0"


class TestC002BusRegression:
    @pytest.mark.asyncio
    async def test_c002_8_bus_still_fires_after_fix(self):
        from services import tradier_stream as ts

        ev = _make_ev()
        ep = _make_ep()
        raw = _make_raw()
        published = []

        async def _capture(msg):
            published.append(msg)

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", new_callable=AsyncMock), \
             patch("services.tradier_stream.build_composite", return_value=None), \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick = AsyncMock(return_value=ep)
            mock_acc.get_signal = AsyncMock(return_value=ep)
            mock_acc.get_alert_level.return_value = "CONVICTION"
            mock_bus.publish_all = _capture

            await ts._process_trade(raw)

        assert len(published) == 1, f"Expected 1 signal published, got {len(published)}"
        assert published[0]["type"] == "signal"
        assert published[0]["data"]["ticker"] == "TSLA"
