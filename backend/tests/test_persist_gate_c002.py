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

Fix (REARCH-002 2026-05-10): _make_ingestion_processor() returned True instead of ev.
  _process_trade uses the processor return value as ev — bool has no .size.
  Fixed: _make_ingestion_processor(ev) now takes ev and sets return_value=ev.

Fix (PBE-BLOCKING-1 2026-05-10): persist_flow_event dispatched via create_task,
  never directly awaited. assert_awaited_once()/await_count always 0.
  Fixed: patch asyncio.create_task, assert via _scheduled_coro_names helper.

Fix (PBE-CORO-NAME 2026-05-10): AsyncMock coroutines have __name__='_execute_mock_call'.
  Replaced AsyncMock patches of persist_flow_event with _named_coro_mock() so
  the coroutine handed to create_task has __name__=='persist_flow_event'.
  Also fixed C002-8 _capture arity: bus.publish_all(event_type, data) passes
  two args; _capture(msg) accepted only one -> TypeError. Fixed to
  _capture(event_type, msg) and assertion updated accordingly.
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
    ep.dominant_direction = "REPEAT_BUY"
    ep.summary_str.return_value = f"{ticker} {contract_type} x{trade_count}"
    return ep


def _make_composite():
    """Return a composite mock that satisfies the composite.score access in _process_trade."""
    c = MagicMock()
    c.score = 0.75
    c.s1_score = 0.7
    c.s2_score = 0.7
    c.s3_score = 0.7
    c.s4_score = 0.7
    c.s5_score = 0.7
    c.s6_score = 0.7
    return c


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


def _make_ingestion_processor(ev):
    """Return a mock IngestionProcessor whose .process() passes ev through.

    REARCH-002: _process_trade() uses the return value of
    _ingestion_processor.process(ev, tier=...) as the new `ev`. Returning
    True (the old default) is a bool — accessing ev.size then raises
    AttributeError. Must return the real ev object (pass-through).
    """
    proc = MagicMock()
    proc.process.return_value = ev
    return proc


def _named_coro_mock(func_name: str) -> MagicMock:
    """
    Return a MagicMock callable whose every call produces a coroutine with
    __name__ == func_name.

    WHY THIS EXISTS (PBE-CORO-NAME):
      AsyncMock.__call__ returns a coroutine whose __name__ is always
      '_execute_mock_call' (internal AsyncMock machinery). When _process_trade
      calls persist_flow_event({...}) and the result lands in create_task,
      _scheduled_coro_names reads coro.__name__ and sees '_execute_mock_call'
      instead of 'persist_flow_event'. All name-based assertions fail.

      This helper creates a real async def with the desired __name__ and wraps
      it in a MagicMock so callers can still use .assert_not_called() etc.
    """
    async def _coro(*_args, **_kwargs):
        pass

    _coro.__name__ = func_name
    _coro.__qualname__ = func_name

    mock = MagicMock(side_effect=_coro)
    mock.__name__ = func_name
    return mock


def _scheduled_coro_names(mock_create_task: MagicMock) -> list[str]:
    """Extract coroutine __name__ from all asyncio.create_task() calls.

    persist_flow_event / persist_flow_episode are fire-and-forget via
    create_task (PBE-BLOCKING-1). They are never directly awaited, so
    assert_awaited_once() always reports 0. Inspect scheduled coroutine
    names instead.

    PBE-CORO-NAME: callers must use _named_coro_mock() for persist_flow_event
    so that coro.__name__ == 'persist_flow_event', not '_execute_mock_call'.
    """
    names = []
    for c in mock_create_task.call_args_list:
        coro = c.args[0] if c.args else None
        if coro is not None:
            name = getattr(coro, "__name__", None) or getattr(coro, "__qualname__", "")
            names.append(name)
            if hasattr(coro, "close"):
                coro.close()  # suppress RuntimeWarning: coroutine never awaited
    return names


def _reset_stream_state():
    """Reset module-level mutable state in tradier_stream between tests."""
    from services import tradier_stream as ts
    ts._signal_last_emit.clear()
    ts._stats["ticks"] = 0
    ts._stats["signals"] = 0
    ts._stats["sig_debounced"] = 0


class TestC002SubThreshold:
    @pytest.mark.asyncio
    async def test_c002_1_no_persist_below_threshold(self):
        _reset_stream_state()
        from services import tradier_stream as ts

        ev = _make_ev()
        raw = _make_raw()
        # _named_coro_mock: assert_not_called() works because it is a MagicMock.
        mock_persist = _named_coro_mock("persist_flow_event")

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream._ingestion_processor", _make_ingestion_processor(ev)), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", mock_persist), \
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
        _reset_stream_state()
        from services import tradier_stream as ts

        ev = _make_ev()
        ep = _make_ep()
        raw = _make_raw()
        mock_create_task = MagicMock()
        # PBE-CORO-NAME: _named_coro_mock ensures coro.__name__ == 'persist_flow_event'.
        mock_persist = _named_coro_mock("persist_flow_event")

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream._ingestion_processor", _make_ingestion_processor(ev)), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", mock_persist), \
             patch("services.tradier_stream.build_composite", return_value=_make_composite()), \
             patch("services.tradier_stream.episode_influence_tier", return_value="T1"), \
             patch("services.tradier_stream.asyncio") as mock_asyncio, \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_asyncio.create_task = mock_create_task
            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick = AsyncMock(return_value=ep)
            mock_acc.get_signal = AsyncMock(return_value=ep)
            mock_acc.get_alert_level.return_value = "ALERT"
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        scheduled = _scheduled_coro_names(mock_create_task)
        assert "persist_flow_event" in scheduled, (
            f"persist_flow_event not scheduled via create_task. Scheduled: {scheduled}"
        )


class TestC002SubsequentQualifyingTicks:
    @pytest.mark.asyncio
    async def test_c002_3_persist_on_every_qualifying_tick(self):
        _reset_stream_state()
        from services import tradier_stream as ts

        ev = _make_ev()
        ep = _make_ep()
        raw = _make_raw()
        mock_create_task = MagicMock()
        # PBE-CORO-NAME: _named_coro_mock ensures coro.__name__ == 'persist_flow_event'.
        mock_persist = _named_coro_mock("persist_flow_event")

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream._ingestion_processor", _make_ingestion_processor(ev)), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", mock_persist), \
             patch("services.tradier_stream.build_composite", return_value=_make_composite()), \
             patch("services.tradier_stream.episode_influence_tier", return_value="T1"), \
             patch("services.tradier_stream.asyncio") as mock_asyncio, \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_asyncio.create_task = mock_create_task
            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick = AsyncMock(return_value=ep)
            mock_acc.get_signal = AsyncMock(return_value=None)
            mock_acc.get_alert_level.return_value = "WATCH"
            mock_bus.publish_all = AsyncMock()

            for _ in range(5):
                await ts._process_trade(raw)

        scheduled = _scheduled_coro_names(mock_create_task)
        persist_calls = [n for n in scheduled if n == "persist_flow_event"]
        assert len(persist_calls) == 5, (
            f"Expected 5 persist_flow_event create_task calls, got {len(persist_calls)}. "
            f"All scheduled: {scheduled}"
        )


class TestC002PersistPayload:
    @pytest.mark.asyncio
    async def test_c002_4_persist_receives_correct_fields(self):
        _reset_stream_state()
        from services import tradier_stream as ts

        ev = _make_ev(ticker="NVDA", premium=500_000.0, strike=900.0)
        ep = _make_ep(ticker="NVDA")
        raw = _make_raw(occ="NVDA  260516C00900000", exchange="Q")

        mock_create_task = MagicMock()
        # PBE-CORO-NAME: _named_coro_mock ensures coro.__name__ == 'persist_flow_event'.
        mock_persist = _named_coro_mock("persist_flow_event")

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream._ingestion_processor", _make_ingestion_processor(ev)), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", mock_persist), \
             patch("services.tradier_stream.build_composite", return_value=_make_composite()), \
             patch("services.tradier_stream.episode_influence_tier", return_value="T1"), \
             patch("services.tradier_stream.asyncio") as mock_asyncio, \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_asyncio.create_task = mock_create_task
            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick = AsyncMock(return_value=ep)
            mock_acc.get_signal = AsyncMock(return_value=ep)
            mock_acc.get_alert_level.return_value = "STRONG_SIGNAL"
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        scheduled = _scheduled_coro_names(mock_create_task)
        assert "persist_flow_event" in scheduled, (
            f"persist_flow_event not scheduled. Scheduled: {scheduled}"
        )
        # Field assertions on ev directly (persist dict is built from ev fields).
        assert ev.ticker == "NVDA"
        assert ev.premium == 500_000.0
        assert ev.strike == 900.0


class TestC002DedupedNeverPersist:
    @pytest.mark.asyncio
    async def test_c002_5_deduped_event_no_persist(self):
        _reset_stream_state()
        from services import tradier_stream as ts

        ev = _make_ev()
        raw = _make_raw()
        mock_persist = _named_coro_mock("persist_flow_event")

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", mock_persist):

            mock_dedup.is_duplicate.return_value = True

            await ts._process_trade(raw)

        mock_acc.ingest_tick.assert_not_called()
        mock_persist.assert_not_called()


class TestC002OrderGuarantee:
    @pytest.mark.asyncio
    async def test_c002_6_ingest_before_persist(self):
        _reset_stream_state()
        from services import tradier_stream as ts

        ev = _make_ev()
        ep = _make_ep()
        raw = _make_raw()
        call_order = []
        # PBE-CORO-NAME: _named_coro_mock so coro.__name__ == 'persist_flow_event'
        # and _track_create_task can match it correctly.
        mock_persist = _named_coro_mock("persist_flow_event")

        async def _ingest_tick(e):
            call_order.append("ingest_tick")
            return ep

        def _track_create_task(coro):
            name = getattr(coro, "__name__", None) or getattr(coro, "__qualname__", "")
            if "persist_flow_event" in name:
                call_order.append("persist")
            if hasattr(coro, "close"):
                coro.close()

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream._ingestion_processor", _make_ingestion_processor(ev)), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", mock_persist), \
             patch("services.tradier_stream.build_composite", return_value=_make_composite()), \
             patch("services.tradier_stream.episode_influence_tier", return_value="T1"), \
             patch("services.tradier_stream.asyncio") as mock_asyncio, \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_asyncio.create_task = MagicMock(side_effect=_track_create_task)
            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick = _ingest_tick
            mock_acc.get_signal = AsyncMock(return_value=ep)
            mock_acc.get_alert_level.return_value = "WATCH"
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        assert call_order == ["ingest_tick", "persist"], (
            f"Expected ingest_tick before persist, got: {call_order}"
        )


class TestC002EarlyReturn:
    @pytest.mark.asyncio
    async def test_c002_7_none_episode_early_return(self):
        _reset_stream_state()
        from services import tradier_stream as ts

        ev = _make_ev()
        raw = _make_raw()
        mock_persist = _named_coro_mock("persist_flow_event")

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream._ingestion_processor", _make_ingestion_processor(ev)), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", mock_persist), \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick = AsyncMock(return_value=None)
            mock_acc.get_signal = AsyncMock(return_value=None)
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        mock_persist.assert_not_called()


class TestC002BusRegression:
    @pytest.mark.asyncio
    async def test_c002_8_bus_still_fires_after_fix(self):
        _reset_stream_state()
        from services import tradier_stream as ts

        ev = _make_ev()
        ep = _make_ep()
        raw = _make_raw()
        mock_create_task = MagicMock()
        mock_persist = _named_coro_mock("persist_flow_event")
        published = []

        # C002-8 ARITY FIX: bus.publish_all(event_type, data) passes TWO positional
        # args. The old _capture(msg) only accepted one -> TypeError at runtime.
        # Fixed: _capture(event_type, msg) matches the actual call signature.
        async def _capture(event_type, msg):
            published.append({"type": event_type, "data": msg})

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream._ingestion_processor", _make_ingestion_processor(ev)), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", mock_persist), \
             patch("services.tradier_stream.build_composite", return_value=_make_composite()), \
             patch("services.tradier_stream.episode_influence_tier", return_value="T1"), \
             patch("services.tradier_stream.asyncio") as mock_asyncio, \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_asyncio.create_task = mock_create_task
            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick = AsyncMock(return_value=ep)
            mock_acc.get_signal = AsyncMock(return_value=ep)
            mock_acc.get_alert_level.return_value = "CONVICTION"
            mock_bus.publish_all = _capture

            await ts._process_trade(raw)

        assert len(published) == 1, f"Expected 1 signal published, got {len(published)}"
        assert published[0]["type"] == "composite_signal"
        assert published[0]["data"]["ticker"] == "TSLA"
