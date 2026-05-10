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

Fix (2026-05-01): Reset module-level _signal_last_emit and _stats["ticks"] before
  every test that exercises _process_trade so SIG-DEBOUNCE state from a prior test
  does not suppress signals in subsequent tests (C008-2, C008-4 were failing because
  the first-crossing emit-key written by C008-1/C008-3 caused _should_emit_signal to
  return should_emit=False in later tests).

Fix (REARCH-002 2026-05-10): patch _ingestion_processor in all _process_trade tests.
  With REARCH-002 wired, the real IngestionProcessor runs against the MagicMock ev;
  its numeric gate checks (ev.dte >= min_dte, ev.open_interest >= min_oi, etc.) get
  MagicMock values back and the processor returns None, dropping the tick before
  persist_flow_event is ever reached. Fix: mock _ingestion_processor.process to
  return ev (pass-through) in every test that calls _process_trade.

Fix (REARCH-002 composite 2026-05-10): C008-2 and C008-4 patched build_composite
  to return_value=None, which hits the LAT-1 'if composite is None: return' guard
  and exits before bus.publish_all. Fix: replace None patch with a mock composite
  object that has .score and .s1_score-.s6_score so the log line and bus.publish_all
  can execute.

Fix (PBE-BLOCKING-1 2026-05-10): persist_flow_event is fire-and-forget via
  asyncio.create_task() — it is never directly awaited in _process_trade.
  assert_awaited_once() always reports 0 awaits regardless of whether the
  call fires. Fix for C008-1/2/4: patch asyncio.create_task, capture all
  scheduled coroutine names, assert persist_flow_event coroutine was (or
  was not) scheduled. C008-3/8 unaffected — they assert mock_persist
  is never called at all (code returns before create_task line).

Fix (PBE-CORO-NAME 2026-05-10): AsyncMock coroutines have __name__="_execute_mock_call".
  When patch("...persist_flow_event", new_callable=AsyncMock) is used and
  _process_trade calls persist_flow_event({...}), the coroutine handed to
  create_task has __name__="_execute_mock_call", not "persist_flow_event".
  _scheduled_coro_names therefore never found "persist_flow_event" in the list.
  Fix: use _named_coro_mock(name) — a helper that creates a regular MagicMock
  whose side_effect is an async def with the correct __name__ attribute. The
  coroutine passed to create_task now has __name__=="persist_flow_event" exactly
  as the assertion expects. _scheduled_coro_names is unchanged.
"""
import asyncio
import sys
import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

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
    ep.dominant_direction = "REPEAT_BUY"
    ep.summary_str.return_value = "AAPL CALL x3"
    return ep


def _make_composite():
    """Mock composite object with all fields accessed by _process_trade after build_composite()."""
    c = MagicMock()
    c.score = 0.75
    c.s1_score = 0.8
    c.s2_score = 0.7
    c.s3_score = 0.75
    c.s4_score = 0.6
    c.s5_score = 0.8
    c.s6_score = 0.7
    return c


def _named_coro_mock(func_name: str) -> MagicMock:
    """
    Return a MagicMock callable whose every call produces a coroutine with
    __name__ == func_name.

    WHY THIS EXISTS (PBE-CORO-NAME):
      AsyncMock.__call__ returns a coroutine whose __name__ is always
      "_execute_mock_call" (the internal AsyncMock machinery).  When
      _process_trade calls persist_flow_event({...}) and the result is handed
      to asyncio.create_task(), _scheduled_coro_names reads coro.__name__ and
      gets "_execute_mock_call" instead of "persist_flow_event".  The assertion
      `"persist_flow_event" in scheduled` therefore always fails.

      This helper builds a real async def with the desired __name__ and wraps it
      in a MagicMock so callers can still use .assert_not_called() etc.  The
      coroutine that lands in create_task now has the correct __name__ and
      _scheduled_coro_names finds it.
    """
    async def _coro(*_args, **_kwargs):
        pass

    _coro.__name__ = func_name
    _coro.__qualname__ = func_name

    mock = MagicMock(side_effect=_coro)
    mock.__name__ = func_name
    return mock


def _reset_stream_state():
    """Reset module-level mutable state in tradier_stream between tests.

    _signal_last_emit is a dict[str, dict] that persists across test runs within
    the same pytest session. If a prior test wrote an emit-key, _should_emit_signal
    will return should_emit=False in the next test (debounce window not elapsed).
    Resetting it ensures each test starts from a clean "never emitted" state.
    """
    from services import tradier_stream as ts
    ts._signal_last_emit.clear()
    ts._stats["ticks"] = 0
    ts._stats["signals"] = 0
    ts._stats["sig_debounced"] = 0


def _scheduled_coro_names(mock_create_task: MagicMock) -> list[str]:
    """
    Extract coroutine function names from all asyncio.create_task() calls.

    persist_flow_event and persist_flow_episode are dispatched as
    asyncio.create_task(coroutine) — never directly awaited (PBE-BLOCKING-1).
    This helper inspects each call's first positional argument and returns its
    __name__ (or __qualname__) so tests can assert which coroutines were scheduled
    without relying on assert_awaited_once().

    PBE-CORO-NAME: callers must use _named_coro_mock() (not AsyncMock) for
    persist_flow_event so that the coroutine's __name__ is "persist_flow_event"
    rather than AsyncMock's internal "_execute_mock_call".
    """
    names = []
    for c in mock_create_task.call_args_list:
        coro = c.args[0] if c.args else None
        if coro is not None:
            name = getattr(coro, "__name__", None) or getattr(coro, "__qualname__", "")
            names.append(name)
            # Always close the coroutine to suppress RuntimeWarning.
            if hasattr(coro, "close"):
                coro.close()
    return names


# ---------------------------------------------------------------------------
# C008-1: qualifying tick during cooldown -> persist fires, bus does NOT
# ---------------------------------------------------------------------------
class TestC008PersistDuringCooldown:
    @pytest.mark.asyncio
    async def test_c008_1_persist_fires_bus_silent_during_cooldown(self):
        _reset_stream_state()
        from services import tradier_stream as ts

        ev = _make_ev()
        persist_ep = _make_ep()
        raw = _make_raw()

        mock_ingestion_processor = MagicMock()
        mock_ingestion_processor.process = MagicMock(return_value=ev)

        mock_create_task = MagicMock()
        # PBE-CORO-NAME: use _named_coro_mock so the coroutine __name__ is correct.
        mock_persist = _named_coro_mock("persist_flow_event")

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream._ingestion_processor", mock_ingestion_processor), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", mock_persist), \
             patch("services.tradier_stream.asyncio") as mock_asyncio, \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_asyncio.create_task = mock_create_task
            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick = AsyncMock(return_value=persist_ep)
            mock_acc.get_signal = AsyncMock(return_value=None)  # cooldown: no signal
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        scheduled = _scheduled_coro_names(mock_create_task)
        assert "persist_flow_event" in scheduled, (
            f"persist_flow_event was not scheduled via create_task. Scheduled: {scheduled}"
        )
        mock_bus.publish_all.assert_not_called()


# ---------------------------------------------------------------------------
# C008-2: qualifying tick after cooldown -> persist AND bus fire
# ---------------------------------------------------------------------------
class TestC008BothFireAfterCooldown:
    @pytest.mark.asyncio
    async def test_c008_2_both_fire_after_cooldown(self):
        _reset_stream_state()
        from services import tradier_stream as ts

        ev = _make_ev()
        persist_ep = _make_ep()
        raw = _make_raw()

        mock_ingestion_processor = MagicMock()
        mock_ingestion_processor.process = MagicMock(return_value=ev)

        mock_composite = _make_composite()
        mock_create_task = MagicMock()
        # PBE-CORO-NAME: use _named_coro_mock so the coroutine __name__ is correct.
        mock_persist = _named_coro_mock("persist_flow_event")

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream._ingestion_processor", mock_ingestion_processor), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", mock_persist), \
             patch("services.tradier_stream.build_composite", return_value=mock_composite), \
             patch("services.tradier_stream.episode_influence_tier", return_value="T1"), \
             patch("services.tradier_stream.asyncio") as mock_asyncio, \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_asyncio.create_task = mock_create_task
            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick = AsyncMock(return_value=persist_ep)
            mock_acc.get_signal = AsyncMock(return_value=persist_ep)
            mock_acc.get_alert_level.return_value = "ALERT"
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        scheduled = _scheduled_coro_names(mock_create_task)
        assert "persist_flow_event" in scheduled, (
            f"persist_flow_event was not scheduled via create_task. Scheduled: {scheduled}"
        )
        mock_bus.publish_all.assert_called_once()


# ---------------------------------------------------------------------------
# C008-3: sub-threshold tick -> neither fires
# ---------------------------------------------------------------------------
class TestC008SubThresholdNeither:
    @pytest.mark.asyncio
    async def test_c008_3_sub_threshold_neither_fires(self):
        _reset_stream_state()
        from services import tradier_stream as ts

        ev = _make_ev()
        raw = _make_raw()

        mock_ingestion_processor = MagicMock()
        mock_ingestion_processor.process = MagicMock(return_value=ev)

        # C008-3 asserts mock_persist.assert_not_called() — _named_coro_mock
        # is a MagicMock so .assert_not_called() works here too.
        mock_persist = _named_coro_mock("persist_flow_event")

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream._ingestion_processor", mock_ingestion_processor), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", mock_persist), \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick = AsyncMock(return_value=None)  # sub-threshold
            mock_acc.get_signal = AsyncMock(return_value=None)
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
        _reset_stream_state()
        from services import tradier_stream as ts

        ev = _make_ev()
        persist_ep = _make_ep()
        raw = _make_raw()

        mock_ingestion_processor = MagicMock()
        mock_ingestion_processor.process = MagicMock(return_value=ev)

        mock_composite = _make_composite()
        mock_create_task = MagicMock()
        # PBE-CORO-NAME: use _named_coro_mock so the coroutine __name__ is correct.
        mock_persist = _named_coro_mock("persist_flow_event")

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream._ingestion_processor", mock_ingestion_processor), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", mock_persist), \
             patch("services.tradier_stream.build_composite", return_value=mock_composite), \
             patch("services.tradier_stream.episode_influence_tier", return_value="T1"), \
             patch("services.tradier_stream.asyncio") as mock_asyncio, \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_asyncio.create_task = mock_create_task
            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick = AsyncMock(return_value=persist_ep)
            mock_acc.get_signal = AsyncMock(return_value=persist_ep)
            mock_acc.get_alert_level.return_value = "CONVICTION"
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        scheduled = _scheduled_coro_names(mock_create_task)
        assert "persist_flow_event" in scheduled, (
            f"persist_flow_event was not scheduled via create_task. Scheduled: {scheduled}"
        )
        mock_bus.publish_all.assert_called_once()


# ---------------------------------------------------------------------------
# C008-5: ingest() shim backward compat
# ---------------------------------------------------------------------------
class TestC008IngestShim:
    def test_c008_5_ingest_shim_calls_both(self):
        from signals.repetition_accumulator import RepetitionAccumulator

        acc = RepetitionAccumulator(min_trades=2, min_premium=10_000, signal_cooldown=5)

        def _ev(prem, ts):
            ev = MagicMock()
            ev.ticker = "SPY"
            ev.contract_type = "CALL"
            ev.strike = 500.0
            ev.expiry = "2026-05-16"
            ev.premium = prem
            ev.dte = 21
            ev.timestamp = ts
            return ev

        ts1 = base_ts
        ts2 = base_ts + timedelta(seconds=10)

        assert asyncio.run(acc.ingest(_ev(6_000.0, ts1))) is None
        result = asyncio.run(acc.ingest(_ev(6_000.0, ts2)))
        assert result is not None
        assert result.trade_count == 2


# ---------------------------------------------------------------------------
# C008-6: ingest_tick ignores cooldown
# ---------------------------------------------------------------------------
class TestC008IngestTickIgnoresCooldown:
    def test_c008_6_ingest_tick_returns_ep_every_qualifying_tick(self):
        from signals.repetition_accumulator import RepetitionAccumulator

        acc = RepetitionAccumulator(min_trades=3, min_premium=50_000, signal_cooldown=5)

        def _ev(ts):
            ev = MagicMock()
            ev.ticker = "NVDA"
            ev.contract_type = "CALL"
            ev.strike = 900.0
            ev.expiry = "2026-05-16"
            ev.premium = 20_000.0
            ev.dte = 21
            ev.timestamp = ts
            return ev

        for i in range(3):
            asyncio.run(acc.ingest_tick(_ev(base_ts + timedelta(seconds=i * 10))))

        ep4 = asyncio.run(acc.ingest_tick(_ev(base_ts + timedelta(minutes=1))))
        ep5 = asyncio.run(acc.ingest_tick(_ev(base_ts + timedelta(minutes=2))))

        assert ep4 is not None, "ingest_tick should return ep even during cooldown"
        assert ep5 is not None, "ingest_tick should return ep even during cooldown"


# ---------------------------------------------------------------------------
# C008-7: get_signal applies cooldown
# ---------------------------------------------------------------------------
class TestC008GetSignalCooldown:
    def test_c008_7_get_signal_applies_cooldown(self):
        from signals.repetition_accumulator import RepetitionAccumulator

        acc = RepetitionAccumulator(min_trades=3, min_premium=50_000, signal_cooldown=5)

        def _ev(ts):
            ev = MagicMock()
            ev.ticker = "META"
            ev.contract_type = "PUT"
            ev.strike = 550.0
            ev.expiry = "2026-05-16"
            ev.premium = 20_000.0
            ev.dte = 21
            ev.timestamp = ts
            return ev

        ep = None
        for i in range(3):
            ep = asyncio.run(acc.ingest_tick(_ev(base_ts + timedelta(seconds=i * 10))))

        cooldown = timedelta(minutes=5)

        sig1 = asyncio.run(acc.get_signal(base_ts + timedelta(seconds=20), cooldown, ep))
        assert sig1 is not None

        ep2 = asyncio.run(acc.ingest_tick(_ev(base_ts + timedelta(minutes=1))))
        sig2 = asyncio.run(acc.get_signal(base_ts + timedelta(minutes=1), cooldown, ep2))
        assert sig2 is None, "get_signal should return None during cooldown"

        ep3 = asyncio.run(acc.ingest_tick(_ev(base_ts + timedelta(minutes=6))))
        sig3 = asyncio.run(acc.get_signal(base_ts + timedelta(minutes=6), cooldown, ep3))
        assert sig3 is not None, "get_signal should fire after cooldown expires"


# ---------------------------------------------------------------------------
# C008-8: regression — deduped events still never reach ingest_tick or persist
# ---------------------------------------------------------------------------
class TestC008DedupRegression:
    @pytest.mark.asyncio
    async def test_c008_8_deduped_never_reach_ingest_or_persist(self):
        _reset_stream_state()
        from services import tradier_stream as ts

        ev = _make_ev()
        raw = _make_raw()

        mock_ingestion_processor = MagicMock()
        mock_ingestion_processor.process = MagicMock(return_value=ev)

        mock_persist = _named_coro_mock("persist_flow_event")

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream._ingestion_processor", mock_ingestion_processor), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", mock_persist), \
             patch("services.tradier_stream.asyncio.create_task"):

            mock_dedup.is_duplicate.return_value = True
            mock_dedup.get_exchange_count.return_value = 2
            mock_dedup._sweep_min = 3

            await ts._process_trade(raw)

        mock_acc.ingest_tick.assert_not_called()
        mock_persist.assert_not_called()
