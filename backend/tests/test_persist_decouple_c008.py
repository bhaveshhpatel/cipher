"""
C-008 — Decouple Persist Tier from Signal Tier.

Fix verified:
  Before fix: persist_flow_event() gated on ingest() return value.
              C-007 cooldown suppressed ingest() -> ticks 4-N during cooldown
              window never wrote to flow_events. Backtesting gap.
  After fix:  _process_trade calls ingest_tick() (persist gate, no cooldown)
              and get_signal() (bus gate, cooldown applied) independently.
              Every qualifying tick writes to flow_events.
              Bus signals only fire when cooldown passes.

Test IDs:
  C008-1  qualifying tick during cooldown -> persist fires, bus does NOT
  C008-2  qualifying tick after cooldown  -> persist fires AND bus fires
  C008-3  sub-threshold tick              -> neither persist nor bus fires
  C008-4  first threshold crossing        -> both persist and bus fire
  C008-5  ingest() shim still works       -> backward compat for C-002/C-007 tests
  C008-6  ingest_tick() ignores cooldown  -> returns ep every time above threshold
  C008-7  get_signal() applies cooldown   -> returns None during cooldown, ep after
  C008-8  regression: deduped events still never reach ingest_tick or persist

Run: pytest backend/tests/test_persist_decouple_c008.py -v
"""
import asyncio
import sys, os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

base_ts = datetime(2026, 4, 26, 14, 30, 0)


def _make_ev(
    ticker="AAPL", contract_type="CALL", strike=200.0,
    premium=100_000.0, size=100, fill_price=3.0,
    bid=2.95, ask=3.05, trade_type="BTO",
    bid_ask_class="ASK", is_aggressive=True,
    is_golden_sweep=False, sentiment="BULLISH",
    influence_tier="INSTITUTIONAL", conviction_score=0.75,
    exchange_count=1, fill_count=1, open_interest=5000,
    iv=0.4, underlying_price=198.0, dte=21, expiry="2026-05-21",
    is_synthetic_quote=False, timestamp=None,
):
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
    ev.timestamp = timestamp or base_ts
    return ev


def _make_raw(occ="AAPL  260521C00200000", exchange="C"):
    return {
        "type": "timesale",
        "timesale": {
            "symbol": occ,
            "exch": exchange,
            "last": 3.0,
            "bid": 2.95,
            "ask": 3.05,
            "size": 100,
            "date": 1745686200000,
        },
    }


def _make_ep(trade_count=3, total_premium=300_000.0):
    ep = MagicMock()
    ep.ticker = "AAPL"
    ep.contract_type = "CALL"
    ep.strike = 200.0
    ep.expiry = "2026-05-21"
    ep.trade_count = trade_count
    ep.total_premium = total_premium
    ep.is_accelerating = False
    ep.summary_str.return_value = "AAPL CALL x3"
    return ep


# ---------------------------------------------------------------------------
# C008-1: qualifying tick during cooldown -> persist fires, bus does NOT
# ---------------------------------------------------------------------------
class TestC008PersistDuringCooldown:
    @pytest.mark.asyncio
    async def test_c008_1_persist_fires_bus_silent_during_cooldown(self):
        """C008-1: when ingest_tick returns ep but get_signal returns None (cooldown),
        persist_flow_event must be called but bus.publish_all must NOT."""
        from services import tradier_stream as ts

        ev = _make_ev()
        persist_ep = _make_ep()
        raw = _make_raw()

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", new_callable=AsyncMock) as mock_persist, \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick.return_value = persist_ep   # above threshold
            mock_acc.get_signal.return_value = None          # cooldown active
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        mock_persist.assert_awaited_once()   # persist fires
        mock_bus.publish_all.assert_not_called()  # bus silent


# ---------------------------------------------------------------------------
# C008-2: qualifying tick after cooldown -> persist AND bus fire
# ---------------------------------------------------------------------------
class TestC008BothFireAfterCooldown:
    @pytest.mark.asyncio
    async def test_c008_2_both_fire_after_cooldown(self):
        """C008-2: when both ingest_tick and get_signal return ep,
        both persist_flow_event and bus.publish_all must be called."""
        from services import tradier_stream as ts

        ev = _make_ev()
        persist_ep = _make_ep()
        raw = _make_raw()

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", new_callable=AsyncMock) as mock_persist, \
             patch("services.tradier_stream.build_composite", return_value=None), \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick.return_value = persist_ep
            mock_acc.get_signal.return_value = persist_ep   # cooldown passed
            mock_acc.get_alert_level.return_value = "ALERT"
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        mock_persist.assert_awaited_once()
        mock_bus.publish_all.assert_called_once()


# ---------------------------------------------------------------------------
# C008-3: sub-threshold tick -> neither fires
# ---------------------------------------------------------------------------
class TestC008SubThresholdNeither:
    @pytest.mark.asyncio
    async def test_c008_3_sub_threshold_neither_fires(self):
        """C008-3: when ingest_tick returns None (sub-threshold),
        neither persist nor bus should fire."""
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
            mock_acc.ingest_tick.return_value = None   # sub-threshold
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        mock_persist.assert_not_called()
        mock_bus.publish_all.assert_not_called()


# ---------------------------------------------------------------------------
# C008-4: first threshold crossing -> both fire
# ---------------------------------------------------------------------------
class TestC008FirstCrossingBothFire:
    @pytest.mark.asyncio
    async def test_c008_4_first_crossing_both_fire(self):
        """C008-4: on the very first threshold crossing (last_signal_at=None),
        both persist and bus must fire."""
        from services import tradier_stream as ts

        ev = _make_ev()
        persist_ep = _make_ep()
        raw = _make_raw()

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", new_callable=AsyncMock) as mock_persist, \
             patch("services.tradier_stream.build_composite", return_value=None), \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick.return_value = persist_ep
            mock_acc.get_signal.return_value = persist_ep   # first signal
            mock_acc.get_alert_level.return_value = "CONVICTION"
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        mock_persist.assert_awaited_once()
        mock_bus.publish_all.assert_called_once()


# ---------------------------------------------------------------------------
# C008-5: ingest() shim backward compat
# ---------------------------------------------------------------------------
class TestC008IngestShim:
    def test_c008_5_ingest_shim_calls_both(self):
        """C008-5: ingest() shim must call ingest_tick + get_signal and return signal ep."""
        from signals.repetition_accumulator import RepetitionAccumulator

        acc = RepetitionAccumulator(min_trades=2, min_premium=10_000, signal_cooldown=5)

        def _ev(prem, ts):
            ev = MagicMock()
            ev.ticker = "SPY"
            ev.contract_type = "CALL"
            ev.strike = 500.0
            ev.expiry = "2026-05-16"
            ev.premium = prem
            ev.timestamp = ts
            return ev

        ts1 = base_ts
        ts2 = base_ts + timedelta(seconds=10)

        assert acc.ingest(_ev(6_000.0, ts1)) is None   # 1 tick, below threshold
        result = acc.ingest(_ev(6_000.0, ts2))         # 2 ticks, crosses threshold
        assert result is not None
        assert result.trade_count == 2


# ---------------------------------------------------------------------------
# C008-6: ingest_tick ignores cooldown
# ---------------------------------------------------------------------------
class TestC008IngestTickIgnoresCooldown:
    def test_c008_6_ingest_tick_returns_ep_every_qualifying_tick(self):
        """C008-6: ingest_tick() must return ep on every call once above threshold,
        regardless of cooldown state."""
        from signals.repetition_accumulator import RepetitionAccumulator

        acc = RepetitionAccumulator(min_trades=3, min_premium=50_000, signal_cooldown=5)

        def _ev(ts):
            ev = MagicMock()
            ev.ticker = "NVDA"
            ev.contract_type = "CALL"
            ev.strike = 900.0
            ev.expiry = "2026-05-16"
            ev.premium = 20_000.0
            ev.timestamp = ts
            return ev

        for i in range(3):
            acc.ingest_tick(_ev(base_ts + timedelta(seconds=i * 10)))

        # Ticks 4 and 5 — within cooldown window — ingest_tick must still return ep
        ep4 = acc.ingest_tick(_ev(base_ts + timedelta(minutes=1)))
        ep5 = acc.ingest_tick(_ev(base_ts + timedelta(minutes=2)))

        assert ep4 is not None, "ingest_tick should return ep even during cooldown"
        assert ep5 is not None, "ingest_tick should return ep even during cooldown"


# ---------------------------------------------------------------------------
# C008-7: get_signal applies cooldown
# ---------------------------------------------------------------------------
class TestC008GetSignalCooldown:
    def test_c008_7_get_signal_applies_cooldown(self):
        """C008-7: get_signal() must return None during cooldown and ep after."""
        from signals.repetition_accumulator import RepetitionAccumulator

        acc = RepetitionAccumulator(min_trades=3, min_premium=50_000, signal_cooldown=5)

        def _ev(ts):
            ev = MagicMock()
            ev.ticker = "META"
            ev.contract_type = "PUT"
            ev.strike = 550.0
            ev.expiry = "2026-05-16"
            ev.premium = 20_000.0
            ev.timestamp = ts
            return ev

        # Reach threshold
        ep = None
        for i in range(3):
            ep = acc.ingest_tick(_ev(base_ts + timedelta(seconds=i * 10)))

        # First signal fires
        sig1 = acc.get_signal(base_ts + timedelta(seconds=20), ep)
        assert sig1 is not None

        # Within cooldown — suppressed
        ep2 = acc.ingest_tick(_ev(base_ts + timedelta(minutes=1)))
        sig2 = acc.get_signal(base_ts + timedelta(minutes=1), ep2)
        assert sig2 is None, "get_signal should return None during cooldown"

        # After cooldown — fires again
        ep3 = acc.ingest_tick(_ev(base_ts + timedelta(minutes=6)))
        sig3 = acc.get_signal(base_ts + timedelta(minutes=6), ep3)
        assert sig3 is not None, "get_signal should fire after cooldown expires"


# ---------------------------------------------------------------------------
# C008-8: regression — deduped events still never reach ingest_tick or persist
# ---------------------------------------------------------------------------
class TestC008DedupRegression:
    @pytest.mark.asyncio
    async def test_c008_8_deduped_never_reach_ingest_or_persist(self):
        """C008-8: deduped events must not call ingest_tick or persist_flow_event."""
        from services import tradier_stream as ts

        ev = _make_ev()
        raw = _make_raw()

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", new_callable=AsyncMock) as mock_persist, \
             patch("services.tradier_stream.asyncio.create_task"):

            mock_dedup.is_duplicate.return_value = True
            mock_dedup.get_exchange_count.return_value = 2
            mock_dedup._sweep_min = 3

            await ts._process_trade(raw)

        mock_acc.ingest_tick.assert_not_called()
        mock_persist.assert_not_called()
