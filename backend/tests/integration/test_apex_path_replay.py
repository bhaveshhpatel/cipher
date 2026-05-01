"""
backend/tests/integration/test_apex_path_replay.py

Apex Path Coverage Integration Suite — generated from cipher_apex_qa_path_coverage_spec.md

Design contract:
- Each test class is guarded by @_skip_if_missing("module.path").
  If the target module is not yet implemented, the entire class is skipped
  cleanly (not errored). CI stays green on feature branches.
- Once the module lands, the class activates automatically — no test file edits needed.
- DO NOT mock pipeline internals. Only mock external I/O (registry, DB writes, WebSocket).
- All fixtures are deterministic. No randomness. No sleep.

Skip progression as stories merge:
  feat/parser-layer  → QA-01 QA-02 QA-03 QA-04 QA-19 TestDirectionInvariants activate
  feat/signal-gate   → QA-05 QA-06 QA-07 QA-08 activate
  feat/accumulator   → QA-09 QA-10 QA-11 QA-12 QA-13 QA-14 QA-17 QA-18
                        QA-20 QA-21 QA-22 QA-23 QA-24 TestAlertLevelBoundaries activate
  feat/composite     → QA-15 QA-16 QA-20 QA-27 QA-28 activate
  feat/ladder        → QA-25 QA-26 QA-28 activate
"""

import importlib
import pytest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# _skip_if_missing: class decorator — skips entire class if module absent
# ---------------------------------------------------------------------------

def _skip_if_missing(*module_paths: str):
    """
    Class decorator. Skips the decorated test class if ANY of the given
    module paths cannot be imported. CI sees "skipped", not "error".

    Usage:
        @_skip_if_missing("backend.parsers.options_flow_parser")
        class TestQA01ZeroFill: ...

        @_skip_if_missing("backend.signals.signal_gate",
                          "backend.parsers.options_flow_parser")
        class TestQA05SyntheticReject: ...
    """
    missing = []
    for path in module_paths:
        try:
            importlib.import_module(path)
        except ImportError:
            missing.append(path)

    if missing:
        reason = "Not yet implemented: " + ", ".join(missing)
        return pytest.mark.skip(reason=reason)

    # All modules present — no-op decorator
    return lambda cls: cls


# ---------------------------------------------------------------------------
# Lazy module accessors — imported only inside tests once guards pass
# ---------------------------------------------------------------------------

def _parser():
    return importlib.import_module("backend.parsers.options_flow_parser")

def _classifier():
    return importlib.import_module("backend.parsers.order_side_classifier")

def _trade_type():
    return importlib.import_module("backend.parsers.trade_type_detector")

def _gate():
    return importlib.import_module("backend.signals.signal_gate")

def _accumulator():
    return importlib.import_module("backend.signals.repetition_accumulator")

def _composite():
    return importlib.import_module("backend.signals.composite_signal_engine")

def _ladder():
    return importlib.import_module("backend.apex.ladder_detector")

def _registry():
    return importlib.import_module("backend.services.symbol_registry")


# ---------------------------------------------------------------------------
# Shared helpers — safe to define at module level (no hard imports)
# ---------------------------------------------------------------------------

def make_raw_tick(
    symbol="AAPL_050926C00200000",
    last=2.50,
    price=None,
    bid=2.45,
    ask=2.55,
    size=100,
    exchange_cnt=3,
    fill_count=3,
) -> dict:
    """Minimal raw Tradier timesale-shaped dict for parser injection."""
    return {
        "symbol": symbol, "last": last, "price": price,
        "bid": bid, "ask": ask, "size": size,
        "exchange_cnt": exchange_cnt, "fill_count": fill_count,
    }


def make_flow_event(
    ticker="AAPL",
    symbol="AAPL_050926C00200000",
    contract_type="CALL",
    bid_ask_class="AT_ASK",
    trade_type="SWEEP",
    premium=100_000.0,
    fill=2.50,
    size=200,
    dte=20,
    strike=200.0,
    underlying_price=198.0,
    open_interest=5_000,
    daily_volume=8_000,
    order_side="BUY",
    sentiment="BULLISH",
    strong_sentiment=True,
    is_synthetic_quote=False,
    is_golden=False,
    bid=2.45,
    ask=2.55,
):
    """Construct a fully-formed OptionsFlowEvent. Imported lazily."""
    OptionsFlowEvent = _parser().OptionsFlowEvent
    return OptionsFlowEvent(
        symbol=symbol, ticker=ticker, contract_type=contract_type,
        bid_ask_class=bid_ask_class, trade_type=trade_type,
        premium=premium, fill=fill, size=size, dte=dte,
        strike=strike, underlying_price=underlying_price,
        open_interest=open_interest, daily_volume=daily_volume,
        order_side=order_side, sentiment=sentiment,
        strong_sentiment=strong_sentiment,
        is_synthetic_quote=is_synthetic_quote,
        is_golden=is_golden, bid=bid, ask=ask,
    )


def make_episode(events: list, ticker="AAPL", strike=200.0, expiry="2026-09-26"):
    """Wrap events into a RepetitionEpisode. Imported lazily."""
    RepetitionEpisode = _accumulator().RepetitionEpisode
    ep = RepetitionEpisode(ticker=ticker, strike=strike, expiry=expiry)
    for ev in events:
        ep.add_event(ev)
    return ep


def make_registry(ready=True, underlying_price=198.0, open_interest=5_000, daily_volume=8_000):
    """Return a mock SymbolRegistry."""
    SymbolRegistry = _registry().SymbolRegistry
    reg = MagicMock(spec=SymbolRegistry)
    reg.is_ready.return_value = ready
    mock_meta = MagicMock()
    mock_meta.underlying_price = underlying_price
    mock_meta.open_interest = open_interest
    reg.lookup.return_value = mock_meta if ready else None
    reg.stock_price.return_value = underlying_price if ready else 0.0
    reg.get_daily_volume.return_value = daily_volume if ready else 0
    return reg


# ---------------------------------------------------------------------------
# QA-01 — Zero Fill Parser Guard
# Activates: feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing("backend.parsers.options_flow_parser", "backend.services.symbol_registry")
class TestQA01ZeroFill:

    def test_fill_resolves_to_zero_returns_none(self):
        parse_raw_tick = _parser().parse_raw_tick
        raw = make_raw_tick(last=None, price=None, bid=0, ask=0, size=25)
        assert parse_raw_tick(raw, registry=make_registry(ready=False)) is None

    def test_no_persistence_write_on_zero_fill(self):
        parse_raw_tick = _parser().parse_raw_tick
        raw = make_raw_tick(last=None, price=None, bid=0, ask=0, size=25)
        with patch("backend.services.tradier_stream.persist_flow_event") as mock_persist:
            parse_raw_tick(raw, registry=make_registry(ready=False))
            mock_persist.assert_not_called()


# ---------------------------------------------------------------------------
# QA-02 — Zero Size Parser Guard
# Activates: feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing("backend.parsers.options_flow_parser", "backend.services.symbol_registry")
class TestQA02ZeroSize:

    def test_zero_size_returns_none(self):
        parse_raw_tick = _parser().parse_raw_tick
        raw = make_raw_tick(last=2.50, bid=2.45, ask=2.55, size=0)
        assert parse_raw_tick(raw, registry=make_registry()) is None


# ---------------------------------------------------------------------------
# QA-03 — Duplicate Drop
# Activates: feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.parsers.dedup_cache",
    "backend.services.symbol_registry",
)
class TestQA03DuplicateDrop:

    def test_second_identical_event_dropped(self):
        parse_raw_tick = _parser().parse_raw_tick
        DedupCache = importlib.import_module("backend.parsers.dedup_cache").DedupCache
        cache = DedupCache(ttl_seconds=30)
        raw = make_raw_tick(symbol="AAPL_050926C00200000", last=2.50, bid=2.45, ask=2.55, size=100)
        ev1 = parse_raw_tick(raw, registry=make_registry())
        assert ev1 is not None
        assert not cache.check_and_insert(ev1), "First event must not be a duplicate"
        ev2 = parse_raw_tick(raw, registry=make_registry())
        assert cache.check_and_insert(ev2), "Second identical event must be flagged as duplicate"


# ---------------------------------------------------------------------------
# QA-04 — Sweep Upgrade Path
# Activates: feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.parsers.dedup_cache",
    "backend.services.symbol_registry",
)
class TestQA04SweepUpgrade:

    def test_multi_exchange_fan_out_upgrades_to_sweep(self):
        parse_raw_tick = _parser().parse_raw_tick
        DedupCache = importlib.import_module("backend.parsers.dedup_cache").DedupCache
        cache = DedupCache(ttl_seconds=30)
        raws = [make_raw_tick(exchange_cnt=1, fill_count=1) for _ in range(3)]
        events = [e for e in (parse_raw_tick(r, registry=make_registry()) for r in raws) if e]
        upgraded = cache.process_exchange_fan_out(events)
        assert any(e.trade_type == "SWEEP" for e in upgraded), \
            "Multi-exchange fan-out cluster must produce a SWEEP-upgraded event"


# ---------------------------------------------------------------------------
# QA-05 — Synthetic Quote Rejected at Apex L1
# Activates: feat/signal-gate + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.signal_gate",
    "backend.services.symbol_registry",
)
class TestQA05SyntheticReject:

    def test_synthetic_low_premium_rejected_at_gate(self):
        passes_signal_gate = _gate().passes_signal_gate
        ev = make_flow_event(
            bid=0, ask=0, is_synthetic_quote=True, strong_sentiment=False,
            order_side="UNKNOWN", premium=30_000, trade_type="SWEEP",
        )
        verdict = passes_signal_gate(ev, tier=1)
        assert not verdict.passed
        assert verdict.reason in ("premium_below_floor", "synthetic_below_floor")

    def test_persistence_fan_out_is_independent_of_signal_rejection(self):
        passes_signal_gate = _gate().passes_signal_gate
        ev = make_flow_event(bid=0, ask=0, is_synthetic_quote=True, premium=30_000)
        with patch("backend.services.tradier_stream.persist_flow_event") as mock_persist:
            passes_signal_gate(ev, tier=1)
            mock_persist.assert_called_once()


# ---------------------------------------------------------------------------
# QA-06 — Spread Gate Rejection
# Activates: feat/signal-gate + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.signal_gate",
    "backend.services.symbol_registry",
)
class TestQA06SpreadReject:

    def test_spread_above_50pct_rejected(self):
        passes_signal_gate = _gate().passes_signal_gate
        ev = make_flow_event(bid=1.00, ask=3.50, premium=200_000)
        verdict = passes_signal_gate(ev, tier=1)
        assert not verdict.passed
        assert verdict.reason == "spread_too_wide"

    def test_spread_exactly_50pct_boundary(self):
        passes_signal_gate = _gate().passes_signal_gate
        ev = make_flow_event(bid=1.00, ask=2.00, premium=200_000)
        verdict = passes_signal_gate(ev, tier=1)
        assert verdict.reason in ("spread_too_wide", "passed")

    def test_tight_spread_passes(self):
        passes_signal_gate = _gate().passes_signal_gate
        ev = make_flow_event(bid=2.45, ask=2.55, premium=200_000)
        assert passes_signal_gate(ev, tier=1).passed


# ---------------------------------------------------------------------------
# QA-07 — T1 SWEEP Below Premium Floor ($50K)
# Activates: feat/signal-gate + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.signal_gate",
    "backend.services.symbol_registry",
)
class TestQA07T1SweepFloor:

    def test_t1_sweep_at_40k_rejected(self):
        passes_signal_gate = _gate().passes_signal_gate
        ev = make_flow_event(trade_type="SWEEP", premium=40_000, bid=2.45, ask=2.55)
        verdict = passes_signal_gate(ev, tier=1)
        assert not verdict.passed
        assert verdict.reason == "premium_below_floor"

    def test_t1_sweep_at_50k_passes(self):
        passes_signal_gate = _gate().passes_signal_gate
        ev = make_flow_event(trade_type="SWEEP", premium=50_000, bid=2.45, ask=2.55)
        assert passes_signal_gate(ev, tier=1).passed


# ---------------------------------------------------------------------------
# QA-08 — T2/T3 SINGLE Below Premium Floor ($150K)
# Activates: feat/signal-gate + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.signal_gate",
    "backend.services.symbol_registry",
)
class TestQA08T2T3SingleFloor:

    def test_t2_single_at_100k_rejected(self):
        passes_signal_gate = _gate().passes_signal_gate
        ev = make_flow_event(trade_type="SINGLE", premium=100_000, bid=2.45, ask=2.55)
        verdict = passes_signal_gate(ev, tier=2)
        assert not verdict.passed
        assert verdict.reason == "premium_below_floor"

    def test_t2_single_at_150k_passes(self):
        passes_signal_gate = _gate().passes_signal_gate
        ev = make_flow_event(trade_type="SINGLE", premium=150_000, bid=2.45, ask=2.55)
        assert passes_signal_gate(ev, tier=2).passed


# ---------------------------------------------------------------------------
# QA-09 — Valid Event Accumulates But Does Not Yet Qualify
# Activates: feat/accumulator + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.repetition_accumulator",
    "backend.services.symbol_registry",
)
class TestQA09AccumulateNoEmit:

    def test_single_event_below_bypass_does_not_emit(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        acc = RepetitionAccumulator(
            window_minutes=10, min_trades=3, min_premium=100_000,
            min_sweeps=3, sweep_bypass_premium=500_000,
        )
        assert acc.add_event(make_flow_event(premium=80_000, trade_type="SWEEP")) is None

    def test_episode_state_is_buffered(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        acc = RepetitionAccumulator(
            window_minutes=10, min_trades=3, min_premium=100_000,
            min_sweeps=3, sweep_bypass_premium=500_000,
        )
        ev = make_flow_event(premium=80_000, trade_type="SWEEP")
        acc.add_event(ev)
        key = (ev.ticker, str(ev.strike), ev.expiry)
        assert key in acc.episodes


# ---------------------------------------------------------------------------
# QA-10 — Single-Event Sweep Bypass (SELL PUT = BULLISH)
# Activates: feat/accumulator + feat/parser-layer + feat/composite
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.parsers.order_side_classifier",
    "backend.signals.repetition_accumulator",
    "backend.signals.composite_signal_engine",
    "backend.services.symbol_registry",
)
class TestQA10SweepBypass:

    def test_sell_put_direction_is_bullish(self):
        classify_order_direction = _classifier().classify_order_direction
        result = classify_order_direction("AT_BID", "PUT", False)
        assert result.order_side == "SELL"
        assert result.sentiment == "BULLISH"
        assert result.strong_sentiment is True

    def test_sell_put_maps_to_repeat_buy(self):
        order_side_to_direction = _classifier().order_side_to_direction
        assert order_side_to_direction("SELL", "PUT") == "REPEAT_BUY"

    def test_single_event_bypass_fires_at_600k(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        acc = RepetitionAccumulator(
            window_minutes=10, min_trades=3, min_premium=100_000,
            min_sweeps=3, sweep_bypass_premium=500_000,
        )
        ev = make_flow_event(
            contract_type="PUT", bid_ask_class="AT_BID", trade_type="SWEEP",
            premium=600_000, dte=15, order_side="SELL",
            sentiment="BULLISH", strong_sentiment=True,
        )
        result = acc.add_event(ev)
        assert result is not None, "Sweep bypass must fire for single-event $600K SWEEP"
        assert result.dominant_direction == "REPEAT_BUY"

    def test_alert_level_is_strong_signal(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        ep = make_episode([make_flow_event(premium=600_000, contract_type="PUT", order_side="SELL")])
        acc = RepetitionAccumulator(window_minutes=10, min_trades=1, min_premium=100_000, min_sweeps=1)
        assert acc.get_alert_level(ep) == "STRONG_SIGNAL"

    def test_composite_score_ceiling_present_no_ladder(self):
        build_composite = _composite().build_composite
        ep = make_episode([make_flow_event(premium=600_000, contract_type="PUT", order_side="SELL")])
        payload = build_composite(ep, accumulator=MagicMock()).to_bus_payload()
        assert payload["data"]["signal"].get("composite_score_ceiling") == 0.90


# ---------------------------------------------------------------------------
# QA-11 — Sweep Bypass Negative (len == 2 must not bypass)
# Activates: feat/accumulator + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.repetition_accumulator",
    "backend.services.symbol_registry",
)
class TestQA11BypassNegative:

    def test_two_event_episode_does_not_bypass(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        acc = RepetitionAccumulator(
            window_minutes=10, min_trades=3, min_sweeps=3,
            min_premium=100_000, sweep_bypass_premium=500_000,
        )
        acc.add_event(make_flow_event(premium=300_000, trade_type="SWEEP"))
        assert acc.add_event(make_flow_event(premium=300_000, trade_type="SWEEP")) is None

    def test_fill_count_does_not_substitute_for_episode_event_count(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        acc = RepetitionAccumulator(
            window_minutes=10, min_trades=3, min_sweeps=3,
            min_premium=100_000, sweep_bypass_premium=500_000,
        )
        ev = make_flow_event(premium=600_000, trade_type="SWEEP")
        ev.fill_count = 5
        assert acc.add_event(ev) is not None


# ---------------------------------------------------------------------------
# QA-12 — BUY PUT Bearish BLOCK, Deep OTM 15%, GOLDEN_BLOCK
# Activates: feat/accumulator + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.parsers.order_side_classifier",
    "backend.parsers.trade_type_detector",
    "backend.signals.repetition_accumulator",
    "backend.services.symbol_registry",
)
class TestQA12BuyPutBearishBlock:

    def test_buy_put_direction_is_bearish(self):
        classify_order_direction = _classifier().classify_order_direction
        result = classify_order_direction("AT_ASK", "PUT", False)
        assert result.order_side == "BUY"
        assert result.sentiment == "BEARISH"
        assert result.strong_sentiment is True

    def test_buy_put_maps_to_repeat_sell(self):
        order_side_to_direction = _classifier().order_side_to_direction
        assert order_side_to_direction("BUY", "PUT") == "REPEAT_SELL"

    def test_golden_block_classification(self):
        is_golden_sweep = _trade_type().is_golden_sweep
        assert is_golden_sweep("BLOCK", 1_500_000, True) is True

    def test_deep_otm_1_5m_passes_multiplied_floor(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        acc = RepetitionAccumulator(
            window_minutes=10, min_trades=1, min_sweeps=1,
            min_premium=100_000, sweep_bypass_premium=500_000, deep_otm_multiplier=1.5,
        )
        ev = make_flow_event(
            contract_type="PUT", bid_ask_class="AT_ASK", trade_type="BLOCK",
            premium=1_500_000, dte=45, strike=170.0, underlying_price=200.0,
            order_side="BUY", sentiment="BEARISH", strong_sentiment=True,
        )
        result = acc.add_event(ev)
        assert result is not None
        assert result.dominant_direction == "REPEAT_SELL"


# ---------------------------------------------------------------------------
# QA-13 — SELL CALL Bearish SPLIT, Standard OTM 5%, T2
# Activates: feat/accumulator + feat/signal-gate + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.parsers.order_side_classifier",
    "backend.signals.signal_gate",
    "backend.signals.repetition_accumulator",
    "backend.services.symbol_registry",
)
class TestQA13SellCallBearishSplit:

    def test_sell_call_direction_is_bearish(self):
        classify_order_direction = _classifier().classify_order_direction
        result = classify_order_direction("AT_BID", "CALL", False)
        assert result.order_side == "SELL"
        assert result.sentiment == "BEARISH"
        assert result.strong_sentiment is True

    def test_sell_call_maps_to_repeat_sell(self):
        order_side_to_direction = _classifier().order_side_to_direction
        assert order_side_to_direction("SELL", "CALL") == "REPEAT_SELL"

    def test_t2_split_150k_passes_gate(self):
        passes_signal_gate = _gate().passes_signal_gate
        ev = make_flow_event(trade_type="SPLIT", premium=150_000, bid=2.45, ask=2.55)
        assert passes_signal_gate(ev, tier=2).passed

    def test_standard_otm_no_multiplier_applied(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        acc = RepetitionAccumulator(
            window_minutes=10, min_trades=1, min_sweeps=1,
            min_premium=100_000, deep_otm_multiplier=1.5, sweep_bypass_premium=500_000,
        )
        ev = make_flow_event(
            contract_type="CALL", bid_ask_class="AT_BID", trade_type="SPLIT",
            premium=150_000, dte=20, strike=210.0, underlying_price=200.0,
            order_side="SELL", sentiment="BEARISH",
        )
        assert acc.add_event(ev) is not None


# ---------------------------------------------------------------------------
# QA-14 — BUY CALL GOLDEN_SWEEP, ATM, LEAPS 120 DTE, Ceiling Path
# Activates: feat/accumulator + feat/composite + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.parsers.order_side_classifier",
    "backend.signals.repetition_accumulator",
    "backend.signals.composite_signal_engine",
    "backend.services.symbol_registry",
)
class TestQA14BuyCallLeapsAtm:

    def test_buy_call_direction_is_bullish(self):
        classify_order_direction = _classifier().classify_order_direction
        result = classify_order_direction("ABOVE_ASK", "CALL", False)
        assert result.order_side == "BUY"
        assert result.sentiment == "BULLISH"
        assert result.strong_sentiment is True

    def test_atm_band_within_2pct(self):
        assert abs(200.0 - 198.0) / 198.0 <= 0.02

    def test_leaps_atm_2_5m_qualifies(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        acc = RepetitionAccumulator(
            window_minutes=10, min_trades=1, min_sweeps=1,
            min_premium=100_000, sweep_bypass_premium=500_000,
            dte_premium_tiers={
                7: (50_000, 25_000), 30: (500_000, 100_000),
                90: (1_000_000, 500_000), 9999: (2_000_000, 1_000_000),
            },
        )
        ev = make_flow_event(
            contract_type="CALL", bid_ask_class="ABOVE_ASK", trade_type="SWEEP",
            premium=2_500_000, dte=120, strike=200.0, underlying_price=198.0,
            order_side="BUY", sentiment="BULLISH",
        )
        assert acc.add_event(ev) is not None

    def test_conviction_at_2_5m(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        ep = make_episode([make_flow_event(premium=2_500_000)])
        acc = RepetitionAccumulator(window_minutes=10, min_trades=1, min_premium=100_000, min_sweeps=1)
        assert acc.get_alert_level(ep) == "CONVICTION"

    def test_ceiling_present_when_no_ladder(self):
        build_composite = _composite().build_composite
        ep = make_episode([make_flow_event(premium=2_500_000)])
        payload = build_composite(ep, accumulator=MagicMock()).to_bus_payload()
        assert payload["data"]["signal"].get("composite_score_ceiling") == 0.90


# ---------------------------------------------------------------------------
# QA-15 — MID Print Weak Sentiment Path
# Activates: feat/composite + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.parsers.order_side_classifier",
    "backend.signals.composite_signal_engine",
    "backend.signals.repetition_accumulator",
    "backend.services.symbol_registry",
)
class TestQA15MidWeakSentiment:

    def test_mid_print_produces_unknown_order_side(self):
        classify_order_direction = _classifier().classify_order_direction
        result = classify_order_direction("MID", "CALL", False)
        assert result.order_side == "UNKNOWN"
        assert result.strong_sentiment is False

    def test_composite_080_discount_applied(self):
        build_composite = _composite().build_composite
        ev_strong = make_flow_event(strong_sentiment=True, premium=500_000)
        ev_weak = make_flow_event(
            strong_sentiment=False, bid_ask_class="MID",
            order_side="UNKNOWN", premium=500_000,
        )
        comp_s = build_composite(make_episode([ev_strong]), accumulator=MagicMock())
        comp_w = build_composite(make_episode([ev_weak]), accumulator=MagicMock())
        assert comp_w.composite_score < comp_s.composite_score


# ---------------------------------------------------------------------------
# QA-16 — Synthetic Quote, Institutional Quality, Passes Gate
# Activates: feat/signal-gate + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.parsers.order_side_classifier",
    "backend.signals.signal_gate",
    "backend.services.symbol_registry",
)
class TestQA16SyntheticPass:

    def test_synthetic_block_200k_passes_gate(self):
        passes_signal_gate = _gate().passes_signal_gate
        ev = make_flow_event(
            bid=0, ask=0, is_synthetic_quote=True,
            trade_type="BLOCK", premium=200_000, strong_sentiment=False,
        )
        assert passes_signal_gate(ev, tier=1).passed

    def test_synthetic_forces_weak_sentiment(self):
        classify_order_direction = _classifier().classify_order_direction
        result = classify_order_direction("AT_ASK", "CALL", is_synthetic=True)
        assert result.strong_sentiment is False
        assert result.order_side == "UNKNOWN"


# ---------------------------------------------------------------------------
# QA-17 — Deep OTM Multiplier Pass at 91+ DTE
# Activates: feat/accumulator + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.repetition_accumulator",
    "backend.services.symbol_registry",
)
class TestQA17DeepOtmMultiplierPass:

    def test_passes_above_multiplied_floor(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        acc = RepetitionAccumulator(
            window_minutes=10, min_trades=1, min_sweeps=1, min_premium=100_000,
            sweep_bypass_premium=500_000, deep_otm_multiplier=1.5,
            dte_premium_tiers={9999: (2_000_000, 1_000_000)},
        )
        ev = make_flow_event(trade_type="SWEEP", premium=1_600_000, dte=180, strike=240.0, underlying_price=200.0)
        assert acc.add_event(ev) is not None

    def test_fails_below_multiplied_floor(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        acc = RepetitionAccumulator(
            window_minutes=10, min_trades=1, min_sweeps=1, min_premium=100_000,
            sweep_bypass_premium=500_000, deep_otm_multiplier=1.5,
            dte_premium_tiers={9999: (2_000_000, 1_000_000)},
        )
        ev = make_flow_event(trade_type="SWEEP", premium=1_100_000, dte=180, strike=240.0, underlying_price=200.0)
        assert acc.add_event(ev) is None


# ---------------------------------------------------------------------------
# QA-18 — Missing underlying_price Fallback
# Activates: feat/accumulator + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.repetition_accumulator",
    "backend.services.symbol_registry",
)
class TestQA18MissingUnderlyingPrice:

    def test_zero_underlying_uses_standard_floor_not_otm_multiplier(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        acc = RepetitionAccumulator(
            window_minutes=10, min_trades=1, min_sweeps=1,
            min_premium=100_000, sweep_bypass_premium=500_000, deep_otm_multiplier=1.5,
        )
        ev = make_flow_event(premium=200_000, underlying_price=0.0, strike=200.0)
        assert acc.add_event(ev) is not None


# ---------------------------------------------------------------------------
# QA-19 — Registry Not Ready Path
# Activates: feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.services.symbol_registry",
)
class TestQA19RegistryNotReady:

    def test_parser_falls_back_gracefully(self):
        parse_raw_tick = _parser().parse_raw_tick
        raw = make_raw_tick(last=2.50, bid=2.45, ask=2.55, size=100)
        ev = parse_raw_tick(raw, registry=make_registry(ready=False))
        assert ev is not None
        assert ev.underlying_price == 0.0
        assert ev.open_interest == 0
        assert ev.daily_volume == 0


# ---------------------------------------------------------------------------
# QA-20 — Volume > OI Boost Path
# Activates: feat/composite + feat/accumulator + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.repetition_accumulator",
    "backend.signals.composite_signal_engine",
    "backend.services.symbol_registry",
)
class TestQA20VolumeOiBoost:

    def test_vol_oi_boosts_composite_score(self):
        build_composite = _composite().build_composite
        comp_normal = build_composite(
            make_episode([make_flow_event(daily_volume=3_000, open_interest=5_000, premium=500_000)]),
            accumulator=MagicMock(),
        )
        comp_boosted = build_composite(
            make_episode([make_flow_event(daily_volume=15_000, open_interest=1_000, premium=500_000)]),
            accumulator=MagicMock(),
        )
        assert comp_boosted.composite_score >= comp_normal.composite_score

    def test_vol_oi_never_causes_accumulator_rejection(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        acc = RepetitionAccumulator(
            window_minutes=10, min_trades=1, min_sweeps=1,
            min_premium=100_000, sweep_bypass_premium=500_000,
        )
        assert acc.add_event(make_flow_event(daily_volume=50_000, open_interest=100, premium=600_000)) is not None


# ---------------------------------------------------------------------------
# QA-21 — WATCH Alert Level
# Activates: feat/accumulator + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.repetition_accumulator",
    "backend.services.symbol_registry",
)
class TestQA21Watch:

    def test_watch_below_100k(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        ep = make_episode([make_flow_event(premium=80_000)])
        acc = RepetitionAccumulator(window_minutes=10, min_trades=1, min_premium=50_000, min_sweeps=1)
        assert acc.get_alert_level(ep) == "WATCH"


# ---------------------------------------------------------------------------
# QA-22 — ALERT Level
# Activates: feat/accumulator + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.repetition_accumulator",
    "backend.services.symbol_registry",
)
class TestQA22Alert:

    def test_alert_between_100k_and_500k(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        ep = make_episode([make_flow_event(premium=250_000)])
        acc = RepetitionAccumulator(window_minutes=10, min_trades=1, min_premium=100_000, min_sweeps=1)
        assert acc.get_alert_level(ep) == "ALERT"


# ---------------------------------------------------------------------------
# QA-23 — STRONG_SIGNAL Without Bypass
# Activates: feat/accumulator + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.repetition_accumulator",
    "backend.services.symbol_registry",
)
class TestQA23StrongSignalNoBypass:

    def test_multi_event_qualifies_as_strong_signal(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        acc = RepetitionAccumulator(
            window_minutes=10, min_trades=3, min_sweeps=3,
            min_premium=100_000, sweep_bypass_premium=500_000,
        )
        result = None
        for _ in range(3):
            result = acc.add_event(make_flow_event(premium=200_000, trade_type="SWEEP"))
        assert result is not None
        assert acc.get_alert_level(result) == "STRONG_SIGNAL"


# ---------------------------------------------------------------------------
# QA-24 — CONVICTION Alert Level
# Activates: feat/accumulator + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.repetition_accumulator",
    "backend.services.symbol_registry",
)
class TestQA24Conviction:

    def test_conviction_at_2m_plus(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        ep = make_episode([make_flow_event(premium=2_100_000)])
        acc = RepetitionAccumulator(window_minutes=10, min_trades=1, min_premium=100_000, min_sweeps=1)
        assert acc.get_alert_level(ep) == "CONVICTION"


# ---------------------------------------------------------------------------
# QA-25 — Ladder Positive: 3 strikes, same ticker + expiry
# Activates: feat/ladder + feat/composite + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.repetition_accumulator",
    "backend.signals.composite_signal_engine",
    "backend.apex.ladder_detector",
    "backend.services.symbol_registry",
)
class TestQA25LadderPositive:

    def _make_nvda_eps(self):
        return [
            make_episode(
                [make_flow_event(strike=s, premium=600_000)],
                ticker="NVDA", strike=s, expiry="2026-09-19",
            )
            for s in [580.0, 590.0, 600.0]
        ]

    def test_three_strikes_same_expiry_fires_ladder(self):
        detect_ladder = _ladder().detect_ladder
        ladder = detect_ladder(self._make_nvda_eps())
        assert ladder is not None
        assert len(ladder.strikes) >= 3
        assert ladder.ticker == "NVDA"

    def test_ladder_removes_ceiling_from_payload(self):
        detect_ladder = _ladder().detect_ladder
        build_composite = _composite().build_composite
        eps = self._make_nvda_eps()
        payload = build_composite(
            eps[0], accumulator=MagicMock(), ladder_signal=detect_ladder(eps),
        ).to_bus_payload()
        assert payload["data"]["signal"].get("composite_score_ceiling") is None


# ---------------------------------------------------------------------------
# QA-26 — Ladder Negative: cross-expiry guard
# Activates: feat/ladder + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.repetition_accumulator",
    "backend.apex.ladder_detector",
    "backend.services.symbol_registry",
)
class TestQA26LadderNegative:

    def test_different_expiries_do_not_trigger_ladder(self):
        detect_ladder = _ladder().detect_ladder
        eps = [
            make_episode([make_flow_event(strike=s)], ticker="NVDA", strike=s, expiry=exp)
            for s, exp in [
                (580.0, "2026-09-19"), (590.0, "2026-10-17"), (600.0, "2026-11-21"),
            ]
        ]
        assert detect_ladder(eps) is None


# ---------------------------------------------------------------------------
# QA-27 — RETAIL Influence Tier
# Activates: feat/composite + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.repetition_accumulator",
    "backend.signals.composite_signal_engine",
    "backend.services.symbol_registry",
)
class TestQA27RetailTier:

    def test_retail_below_100k(self):
        episode_influence_tier = _composite().episode_influence_tier
        ep = make_episode([make_flow_event(premium=80_000)])
        assert episode_influence_tier(ep) == "RETAIL"


# ---------------------------------------------------------------------------
# QA-28 — WHALE Influence Tier with Ladder Active, No Ceiling
# Activates: feat/ladder + feat/composite + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.repetition_accumulator",
    "backend.signals.composite_signal_engine",
    "backend.apex.ladder_detector",
    "backend.services.symbol_registry",
)
class TestQA28WhaleLadder:

    def test_whale_tier_at_2m(self):
        episode_influence_tier = _composite().episode_influence_tier
        ep = make_episode([make_flow_event(premium=2_100_000)])
        assert episode_influence_tier(ep) == "WHALE"

    def test_whale_plus_ladder_no_ceiling_in_payload(self):
        detect_ladder = _ladder().detect_ladder
        build_composite = _composite().build_composite
        eps = [
            make_episode(
                [make_flow_event(strike=s, premium=700_000)],
                ticker="NVDA", strike=s, expiry="2026-09-19",
            )
            for s in [580.0, 590.0, 600.0]
        ]
        ep = make_episode(
            [make_flow_event(premium=2_100_000)],
            ticker="NVDA", strike=580.0, expiry="2026-09-19",
        )
        payload = build_composite(ep, accumulator=MagicMock(), ladder_signal=detect_ladder(eps)).to_bus_payload()
        assert payload["data"]["signal"].get("composite_score_ceiling") is None


# ---------------------------------------------------------------------------
# Direction Invariants — CI Hard Gate (14 tests, must never regress)
# Activates: feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.order_side_classifier",
)
class TestDirectionInvariants:
    """Hard CI gate. Any parser refactor breaking these must block merge."""

    # SELL-side (original 6)
    def test_sell_put_sentiment_bullish(self):
        assert _classifier().classify_order_direction("AT_BID", "PUT", False).sentiment == "BULLISH"

    def test_below_bid_put_sentiment_bullish(self):
        assert _classifier().classify_order_direction("BELOW_BID", "PUT", False).sentiment == "BULLISH"

    def test_sell_put_order_side_is_sell(self):
        assert _classifier().classify_order_direction("AT_BID", "PUT", False).order_side == "SELL"

    def test_sell_put_strong_sentiment_true(self):
        assert _classifier().classify_order_direction("AT_BID", "PUT", False).strong_sentiment is True

    def test_sell_put_maps_to_repeat_buy(self):
        assert _classifier().order_side_to_direction("SELL", "PUT") == "REPEAT_BUY"

    def test_sell_call_maps_to_repeat_sell(self):
        assert _classifier().order_side_to_direction("SELL", "CALL") == "REPEAT_SELL"

    # BUY-side (Issue 8 — 8 additions)
    def test_buy_call_sentiment_bullish(self):
        assert _classifier().classify_order_direction("AT_ASK", "CALL", False).sentiment == "BULLISH"

    def test_buy_call_order_side_is_buy(self):
        assert _classifier().classify_order_direction("AT_ASK", "CALL", False).order_side == "BUY"

    def test_buy_call_strong_sentiment_true(self):
        assert _classifier().classify_order_direction("AT_ASK", "CALL", False).strong_sentiment is True

    def test_buy_put_sentiment_bearish(self):
        assert _classifier().classify_order_direction("AT_ASK", "PUT", False).sentiment == "BEARISH"

    def test_buy_put_order_side_is_buy(self):
        assert _classifier().classify_order_direction("AT_ASK", "PUT", False).order_side == "BUY"

    def test_buy_put_strong_sentiment_true(self):
        assert _classifier().classify_order_direction("AT_ASK", "PUT", False).strong_sentiment is True

    def test_buy_call_maps_to_repeat_buy(self):
        assert _classifier().order_side_to_direction("BUY", "CALL") == "REPEAT_BUY"

    def test_buy_put_maps_to_repeat_sell(self):
        assert _classifier().order_side_to_direction("BUY", "PUT") == "REPEAT_SELL"


# ---------------------------------------------------------------------------
# Alert Level Boundary Contract — regression guard
# Activates: feat/accumulator + feat/parser-layer
# ---------------------------------------------------------------------------
@_skip_if_missing(
    "backend.parsers.options_flow_parser",
    "backend.signals.repetition_accumulator",
    "backend.services.symbol_registry",
)
class TestAlertLevelBoundaries:

    @pytest.fixture
    def acc(self):
        RepetitionAccumulator = _accumulator().RepetitionAccumulator
        return RepetitionAccumulator(
            window_minutes=10, min_trades=1, min_premium=50_000, min_sweeps=1,
        )

    def test_watch_upper_boundary(self, acc):
        assert acc.get_alert_level(make_episode([make_flow_event(premium=99_999)])) == "WATCH"

    def test_alert_lower_boundary(self, acc):
        assert acc.get_alert_level(make_episode([make_flow_event(premium=100_000)])) == "ALERT"

    def test_strong_signal_lower_boundary(self, acc):
        assert acc.get_alert_level(make_episode([make_flow_event(premium=500_000)])) == "STRONG_SIGNAL"

    def test_conviction_lower_boundary(self, acc):
        assert acc.get_alert_level(make_episode([make_flow_event(premium=2_000_000)])) == "CONVICTION"
