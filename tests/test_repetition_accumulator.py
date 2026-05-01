"""
tests/test_repetition_accumulator.py

S4 — Apex L2 Dual-Window Accumulator test suite.

Coverage targets (100% line + branch):
  - DTE-adjusted premium floors (all 4 DTE buckets x T1 and T2/T3)
  - OTM classification: ATM (<=2%), standard OTM (2-12%), deep OTM (>12%)
  - Deep OTM multiplier gate: reject below, pass above
  - deep_otm_multiplier=1.0: reject-then-pass pair pins the > 1.0 branch (S4-POST-1)
  - underlying_price == 0 fallback (no OTM classification, standard floor)
  - Sweep bypass: len(ep.events)==1 AND SWEEP AND premium >= threshold
  - Sweep bypass negative: len(ep.events)==2, bypass does NOT fire
  - min_sweeps gate: reject when sweep_count < min_sweeps
  - min_sweeps gate disabled when min_sweeps==0
  - dominant_direction: premium-weighted REPEAT_BUY and REPEAT_SELL
  - dominant_direction: SELL PUT resolves to REPEAT_BUY (S2 invariant)
  - dominant_direction: tie -> REPEAT_BUY (>= comparison)
  - Alert levels: WATCH, ALERT, STRONG_SIGNAL, CONVICTION (normal + accelerating)
  - CONVICTION accelerating boundary: is_accelerating=True AND premium==500K exactly
  - get_signal cooldown gate
  - get_signal int-timestamp coercion (L471)
  - ingest shim backward-compat
  - ingest shim int-timestamp coercion (L504-505)
  - cleanup_expired
  - cleanup_expired last_seen=None guard (L577)
  - set_tier_map
  - _get_episode_min_premium: empty dte_premium_tiers fallback
  - _get_episode_min_premium: DTE exceeds all explicit keys + log.debug (L294)
  - _get_episode_min_premium: unknown ticker defaults to T1 strict (Finding 2)
  - _get_episode_min_premium: _max_dte_key=None guard (S4-POST-2)
  - _get_episode_min_premium: standard floor reject else-branch (L341→344)
  - _is_single_whale_sweep: all False branches
  - _DictEventWrapper allocated at module level (not per-tick)
  - _DictEventWrapper timestamp missing-key fallback (L128-129)
  - Window pruning

QA scenarios exercised:
  QA-09 (accumulate/no emit), QA-10 (bypass positive), QA-11 (bypass negative),
  QA-17 (deep OTM pass), QA-18 (missing underlying fallback),
  QA-21 (WATCH), QA-22 (ALERT at $250K boundary), QA-23 (STRONG_SIGNAL at $1M),
  QA-24 (CONVICTION)

Alert level thresholds (reconciled, panel deliberation May 1 2026):
  CONVICTION    >= 2_000_000
  CONVICTION    is_accelerating AND >= 500_000
  STRONG_SIGNAL >= 1_000_000
  ALERT         >= 250_000
  WATCH         < 250_000
"""
import asyncio
import sys
import os
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path bootstrapping — allow running from repo root or tests/ directory
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from signals.repetition_accumulator import (
    RepetitionAccumulator,
    RepetitionEpisode,
    _DEFAULT_DTE_PREMIUM_TIERS,
    _DictEventWrapper,
    order_side_to_direction,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    """Run a coroutine synchronously in tests."""
    return asyncio.run(coro)


def _ts(offset_seconds: int = 0) -> datetime:
    """Return a UTC datetime offset by `offset_seconds` from a fixed base."""
    base = datetime(2026, 5, 1, 14, 0, 0, tzinfo=timezone.utc)
    return base + timedelta(seconds=offset_seconds)


def make_event(
    ticker="AAPL",
    contract_type="CALL",
    strike=150.0,
    expiry="2026-06-20",
    premium=200_000.0,
    trade_type="SWEEP",
    dte=15,
    underlying_price=150.0,
    order_side="BUY",
    timestamp=None,
):
    ev = MagicMock()
    ev.ticker           = ticker
    ev.contract_type    = contract_type
    ev.strike           = strike
    ev.expiry           = expiry
    ev.premium          = premium
    ev.trade_type       = trade_type
    ev.dte              = dte
    ev.underlying_price = underlying_price
    ev.order_side       = order_side
    ev.occ_symbol       = None
    ev.direction        = None
    ev.sentiment        = None
    ev.timestamp        = timestamp or _ts()
    return ev


def make_accumulator(**kwargs) -> RepetitionAccumulator:
    defaults = dict(
        window_minutes=10,
        min_trades=3,
        min_premium=50_000,
        min_sweeps=0,
        sweep_bypass_premium=0.0,
        deep_otm_multiplier=1.5,
        dte_premium_tiers=dict(_DEFAULT_DTE_PREMIUM_TIERS),
    )
    defaults.update(kwargs)
    return RepetitionAccumulator(**defaults)


# ---------------------------------------------------------------------------
# Finding 1 — otm_band param removed
# ---------------------------------------------------------------------------

class TestOtmBandParamRemoved:

    def test_constructor_does_not_accept_otm_band(self):
        import inspect
        sig = inspect.signature(RepetitionAccumulator.__init__)
        assert "otm_band" not in sig.parameters


# ---------------------------------------------------------------------------
# Finding 2 — unknown-ticker default tier is T1 (strict)
# ---------------------------------------------------------------------------

class TestUnknownTierDefaultIsT1Strict:

    def test_unknown_ticker_defaults_to_t1_floor(self):
        acc = make_accumulator(tier_map={})
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        e = MagicMock()
        e.dte = 20
        ep.events = [e]
        assert acc._get_episode_min_premium(ep) == 500_000

    def test_known_t2_ticker_still_uses_t2_floor(self):
        acc = make_accumulator(tier_map={"TSLA": 2})
        ep = RepetitionEpisode(ticker="TSLA", contract_type="CALL")
        e = MagicMock()
        e.dte = 20
        ep.events = [e]
        assert acc._get_episode_min_premium(ep) == 100_000


# ---------------------------------------------------------------------------
# Finding 7 — _DictEventWrapper is a module-level class
# ---------------------------------------------------------------------------

class TestDictEventWrapperModuleLevel:

    def test_dict_event_wrapper_is_module_level_class(self):
        import signals.repetition_accumulator as mod
        assert hasattr(mod, "_DictEventWrapper")
        assert isinstance(mod._DictEventWrapper, type)

    def test_dict_event_wrapper_attrs(self):
        d = {
            "premium": 123.0,
            "trade_type": "SWEEP",
            "dte": 15,
            "underlying_price": 150.0,
            "order_side": "BUY",
            "contract_type": "CALL",
        }
        w = _DictEventWrapper(d)
        assert w.premium == 123.0
        assert w.trade_type == "SWEEP"
        assert w.dte == 15
        assert w.underlying_price == 150.0
        assert w.order_side == "BUY"
        assert w.contract_type == "CALL"


# ---------------------------------------------------------------------------
# RepetitionEpisode unit tests
# ---------------------------------------------------------------------------

class TestRepetitionEpisodeProperties:

    def test_trade_count_empty(self):
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        assert ep.trade_count == 0

    def test_total_premium_sums_events(self):
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        e1 = MagicMock()
        e1.premium = 100_000.0
        e2 = MagicMock()
        e2.premium = 200_000.0
        ep.events = [e1, e2]
        assert ep.total_premium == 300_000.0

    def test_total_premium_missing_attr_defaults_to_zero(self):
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        e = object()
        ep.events = [e]
        assert ep.total_premium == 0.0

    def test_is_accelerating_false_less_than_3_events(self):
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        e = MagicMock()
        e.timestamp = _ts()
        ep.events = [e, e]
        assert ep.is_accelerating is False

    def test_is_accelerating_true_within_60s(self):
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        events = []
        for i in range(3):
            e = MagicMock()
            e.timestamp = _ts(i * 10)
            events.append(e)
        ep.events = events
        assert ep.is_accelerating is True

    def test_is_accelerating_false_span_exceeds_60s(self):
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        events = []
        for i in range(3):
            e = MagicMock()
            e.timestamp = _ts(i * 35)
            events.append(e)
        ep.events = events
        assert ep.is_accelerating is False

    def test_is_accelerating_exception_returns_false(self):
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        events = [MagicMock(), MagicMock(), MagicMock()]
        for e in events:
            e.timestamp = "not_a_datetime"
        ep.events = events
        assert ep.is_accelerating is False

    def test_summary_str(self):
        ep = RepetitionEpisode(
            ticker="AAPL", contract_type="CALL", strike=150.0, expiry="2026-06-20"
        )
        s = ep.summary_str()
        assert "AAPL" in s
        assert "CALL" in s


# ---------------------------------------------------------------------------
# dominant_direction tests
# ---------------------------------------------------------------------------

class TestDominantDirection:

    def _make_ev(self, order_side, contract_type, premium):
        e = MagicMock()
        e.order_side    = order_side
        e.contract_type = contract_type
        e.premium       = premium
        return e

    def test_buy_call_dominant_is_repeat_buy(self):
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        ep.events = [self._make_ev("BUY", "CALL", 500_000.0)]
        assert ep.dominant_direction == "REPEAT_BUY"

    def test_sell_put_dominant_is_repeat_buy(self):
        ep = RepetitionEpisode(ticker="SPY", contract_type="PUT")
        ep.events = [
            self._make_ev("SELL", "PUT", 1_800_000.0),
            self._make_ev("UNKNOWN", "PUT", 5_000.0),
        ]
        assert ep.dominant_direction == "REPEAT_BUY"

    def test_buy_put_dominant_is_repeat_sell(self):
        ep = RepetitionEpisode(ticker="AAPL", contract_type="PUT")
        ep.events = [self._make_ev("BUY", "PUT", 600_000.0)]
        assert ep.dominant_direction == "REPEAT_SELL"

    def test_sell_call_dominant_is_repeat_sell(self):
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        ep.events = [self._make_ev("SELL", "CALL", 400_000.0)]
        assert ep.dominant_direction == "REPEAT_SELL"

    def test_tie_resolves_to_repeat_buy(self):
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        ep.events = [
            self._make_ev("BUY", "CALL", 100_000.0),
            self._make_ev("BUY", "PUT", 100_000.0),
        ]
        assert ep.dominant_direction == "REPEAT_BUY"

    def test_unknown_order_side_falls_back_to_contract_type(self):
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        ep.events = [self._make_ev("UNKNOWN", "CALL", 200_000.0)]
        assert ep.dominant_direction == "REPEAT_BUY"

    def test_empty_events_defaults_to_repeat_buy(self):
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        ep.events = []
        assert ep.dominant_direction == "REPEAT_BUY"

    def test_premium_weighted_mixed_episode(self):
        ep = RepetitionEpisode(ticker="SPY", contract_type="PUT")
        ep.events = [
            self._make_ev("SELL", "PUT", 1_800_000.0),
            self._make_ev("BUY",  "PUT",   200_000.0),
        ]
        assert ep.dominant_direction == "REPEAT_BUY"


# ---------------------------------------------------------------------------
# OTM classification tests
# ---------------------------------------------------------------------------

class TestOTMClassification:

    def test_atm_exactly_at_2pct(self):
        result = RepetitionAccumulator._classify_otm(153.0, 150.0)
        assert result == "ATM"

    def test_atm_below_2pct(self):
        result = RepetitionAccumulator._classify_otm(151.0, 150.0)
        assert result == "ATM"

    def test_atm_exactly_at_strike(self):
        result = RepetitionAccumulator._classify_otm(150.0, 150.0)
        assert result == "ATM"

    def test_standard_otm_just_above_2pct(self):
        result = RepetitionAccumulator._classify_otm(153.016, 150.0)
        assert result == "STANDARD_OTM"

    def test_standard_otm_at_12pct(self):
        result = RepetitionAccumulator._classify_otm(168.0, 150.0)
        assert result == "STANDARD_OTM"

    def test_deep_otm_just_above_12pct(self):
        result = RepetitionAccumulator._classify_otm(168.02, 150.0)
        assert result == "DEEP_OTM"

    def test_deep_otm_far_out(self):
        result = RepetitionAccumulator._classify_otm(200.0, 150.0)
        assert result == "DEEP_OTM"

    def test_underlying_price_zero_returns_unknown(self):
        result = RepetitionAccumulator._classify_otm(150.0, 0.0)
        assert result == "UNKNOWN"

    def test_underlying_price_negative_returns_unknown(self):
        result = RepetitionAccumulator._classify_otm(150.0, -1.0)
        assert result == "UNKNOWN"

    def test_put_below_strike_atm(self):
        result = RepetitionAccumulator._classify_otm(148.0, 150.0)
        assert result == "ATM"


# ---------------------------------------------------------------------------
# _get_episode_min_premium tests
# ---------------------------------------------------------------------------

class TestGetEpisodeMinPremium:

    def _ep(self, ticker, dte):
        ep = RepetitionEpisode(ticker=ticker, contract_type="CALL")
        e = MagicMock()
        e.dte = dte
        ep.events = [e]
        return ep

    def test_empty_dte_tiers_returns_min_premium(self):
        acc = RepetitionAccumulator(min_premium=75_000, dte_premium_tiers={})
        ep = self._ep("AAPL", 15)
        assert acc._get_episode_min_premium(ep) == 75_000

    def test_t1_dte_0_to_7(self):
        acc = make_accumulator(tier_map={"AAPL": 1})
        ep = self._ep("AAPL", 5)
        assert acc._get_episode_min_premium(ep) == 50_000

    def test_t1_dte_8_to_30(self):
        acc = make_accumulator(tier_map={"AAPL": 1})
        ep = self._ep("AAPL", 20)
        assert acc._get_episode_min_premium(ep) == 500_000

    def test_t1_dte_31_to_90(self):
        acc = make_accumulator(tier_map={"AAPL": 1})
        ep = self._ep("AAPL", 60)
        assert acc._get_episode_min_premium(ep) == 1_000_000

    def test_t1_dte_91_plus(self):
        acc = make_accumulator(tier_map={"AAPL": 1})
        ep = self._ep("AAPL", 120)
        assert acc._get_episode_min_premium(ep) == 2_000_000

    def test_t2_dte_0_to_7(self):
        acc = make_accumulator(tier_map={"TSLA": 2})
        ep = self._ep("TSLA", 5)
        assert acc._get_episode_min_premium(ep) == 25_000

    def test_t2_dte_8_to_30(self):
        acc = make_accumulator(tier_map={"TSLA": 2})
        ep = self._ep("TSLA", 20)
        assert acc._get_episode_min_premium(ep) == 100_000

    def test_t2_dte_31_to_90(self):
        acc = make_accumulator(tier_map={"TSLA": 2})
        ep = self._ep("TSLA", 60)
        assert acc._get_episode_min_premium(ep) == 500_000

    def test_t2_dte_91_plus(self):
        acc = make_accumulator(tier_map={"TSLA": 2})
        ep = self._ep("TSLA", 120)
        assert acc._get_episode_min_premium(ep) == 1_000_000

    def test_t3_uses_t2_t3_column(self):
        acc = make_accumulator(tier_map={"MSTR": 3})
        ep = self._ep("MSTR", 20)
        assert acc._get_episode_min_premium(ep) == 100_000

    def test_unknown_tier_defaults_to_t1_strict(self):
        acc = make_accumulator(tier_map={})
        ep = self._ep("UNKNOWN", 20)
        assert acc._get_episode_min_premium(ep) == 500_000

    def test_dte_at_exact_bucket_boundary_7(self):
        acc = make_accumulator(tier_map={"AAPL": 1})
        ep = self._ep("AAPL", 7)
        assert acc._get_episode_min_premium(ep) == 50_000

    def test_dte_at_exact_bucket_boundary_30(self):
        acc = make_accumulator(tier_map={"AAPL": 1})
        ep = self._ep("AAPL", 30)
        assert acc._get_episode_min_premium(ep) == 500_000

    def test_dte_at_exact_bucket_boundary_90(self):
        acc = make_accumulator(tier_map={"AAPL": 1})
        ep = self._ep("AAPL", 90)
        assert acc._get_episode_min_premium(ep) == 1_000_000

    def test_dte_exceeds_all_explicit_keys_uses_highest(self):
        custom_tiers = {10: (100_000, 50_000), 30: (500_000, 200_000)}
        acc = RepetitionAccumulator(
            dte_premium_tiers=custom_tiers,
            tier_map={"AAPL": 1},
        )
        ep = self._ep("AAPL", 9999)
        assert acc._get_episode_min_premium(ep) == 500_000

    def test_empty_episode_events_uses_dte_zero(self):
        acc = make_accumulator(tier_map={"AAPL": 1})
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        assert acc._get_episode_min_premium(ep) == 50_000


# ---------------------------------------------------------------------------
# S4-POST-2 (#46) — _max_dte_key=None guard
# ---------------------------------------------------------------------------

class TestMaxDteKeyNoneGuard:

    def test_max_dte_key_is_none_when_tiers_empty(self):
        acc = RepetitionAccumulator(dte_premium_tiers={}, min_premium=99_000)
        assert acc._max_dte_key is None

    def test_call_with_none_max_dte_key_returns_min_premium_no_exception(self):
        acc = RepetitionAccumulator(dte_premium_tiers={}, min_premium=77_000)
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        e = MagicMock()
        e.dte = 9999
        ep.events = [e]
        assert acc._get_episode_min_premium(ep) == 77_000

    def test_ingest_tick_with_empty_tiers_uses_min_premium_no_exception(self):
        acc = RepetitionAccumulator(
            min_trades=1,
            min_premium=50_000,
            dte_premium_tiers={},
        )
        ev = make_event(premium=100_000.0, dte=9999, underlying_price=0.0)
        result = run(acc.ingest_tick(ev))
        assert result is not None


# ---------------------------------------------------------------------------
# S4-POST-1 (#45) — deep_otm_multiplier=1.0 reject-then-pass pair
# ---------------------------------------------------------------------------

class TestDeepOtmMultiplierEqualsOne:

    _TICKER         = "TSLA"
    _TIER_MAP       = {"TSLA": 2}
    _DTE            = 5
    _STANDARD_FLOOR = 25_000
    _STRIKE         = 180.0
    _UNDERLYING     = 150.0
    _PREMIUM        = 30_000.0

    def test_deep_otm_rejected_at_1_5x_multiplier(self):
        acc = RepetitionAccumulator(
            min_trades=1, min_sweeps=0, min_premium=1,
            dte_premium_tiers=dict(_DEFAULT_DTE_PREMIUM_TIERS),
            deep_otm_multiplier=1.5,
            tier_map=self._TIER_MAP,
        )
        ev = make_event(
            ticker=self._TICKER, premium=self._PREMIUM, dte=self._DTE,
            strike=self._STRIKE, underlying_price=self._UNDERLYING,
        )
        assert run(acc.ingest_tick(ev)) is None

    def test_deep_otm_passes_at_1_0x_multiplier(self):
        acc = RepetitionAccumulator(
            min_trades=1, min_sweeps=0, min_premium=1,
            dte_premium_tiers=dict(_DEFAULT_DTE_PREMIUM_TIERS),
            deep_otm_multiplier=1.0,
            tier_map=self._TIER_MAP,
        )
        ev = make_event(
            ticker=self._TICKER, premium=self._PREMIUM, dte=self._DTE,
            strike=self._STRIKE, underlying_price=self._UNDERLYING,
        )
        assert run(acc.ingest_tick(ev)) is not None


# ---------------------------------------------------------------------------
# Sweep bypass tests
# ---------------------------------------------------------------------------

class TestSweepBypass:

    def _ep_with_events(self, n_events, trade_type="SWEEP", premium_each=600_000.0):
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        for _ in range(n_events):
            e = MagicMock()
            e.trade_type = trade_type
            e.premium    = premium_each
            ep.events.append(e)
        return ep

    def test_bypass_disabled_when_bypass_premium_zero(self):
        acc = make_accumulator(sweep_bypass_premium=0.0)
        ep = self._ep_with_events(1, trade_type="SWEEP", premium_each=1_000_000.0)
        assert acc._is_single_whale_sweep(ep) is False

    def test_bypass_fires_single_sweep_above_threshold(self):
        acc = make_accumulator(sweep_bypass_premium=500_000.0)
        ep = self._ep_with_events(1, trade_type="SWEEP", premium_each=600_000.0)
        assert acc._is_single_whale_sweep(ep) is True

    def test_bypass_negative_two_events(self):
        acc = make_accumulator(sweep_bypass_premium=500_000.0)
        ep = self._ep_with_events(2, trade_type="SWEEP", premium_each=600_000.0)
        assert acc._is_single_whale_sweep(ep) is False

    def test_bypass_negative_wrong_trade_type(self):
        acc = make_accumulator(sweep_bypass_premium=500_000.0)
        ep = self._ep_with_events(1, trade_type="BLOCK", premium_each=600_000.0)
        assert acc._is_single_whale_sweep(ep) is False

    def test_bypass_negative_premium_below_threshold(self):
        acc = make_accumulator(sweep_bypass_premium=500_000.0)
        ep = self._ep_with_events(1, trade_type="SWEEP", premium_each=400_000.0)
        assert acc._is_single_whale_sweep(ep) is False

    def test_bypass_negative_premium_exactly_at_threshold(self):
        acc = make_accumulator(sweep_bypass_premium=500_000.0)
        ep = self._ep_with_events(1, trade_type="SWEEP", premium_each=500_000.0)
        assert acc._is_single_whale_sweep(ep) is True

    def test_bypass_case_insensitive_trade_type(self):
        acc = make_accumulator(sweep_bypass_premium=500_000.0)
        ep = self._ep_with_events(1, trade_type="sweep", premium_each=600_000.0)
        assert acc._is_single_whale_sweep(ep) is True


# ---------------------------------------------------------------------------
# ingest_tick integration tests
# ---------------------------------------------------------------------------

class TestIngestTick:

    def test_single_event_does_not_qualify_min_trades(self):
        acc = make_accumulator(min_trades=3, min_sweeps=0)
        ev = make_event(premium=500_000.0, dte=15, underlying_price=150.0)
        assert run(acc.ingest_tick(ev)) is None

    def test_three_events_meets_min_trades(self):
        acc = make_accumulator(min_trades=3, min_sweeps=0,
                               dte_premium_tiers={}, min_premium=50_000)
        ev = make_event(premium=100_000.0, dte=15, underlying_price=150.0)
        run(acc.ingest_tick(ev))
        run(acc.ingest_tick(ev))
        assert run(acc.ingest_tick(ev)) is not None

    def test_premium_below_dte_floor_rejected(self):
        acc = make_accumulator(min_trades=1, min_sweeps=0, tier_map={"AAPL": 1})
        ev = make_event(ticker="AAPL", premium=200_000.0, dte=20, underlying_price=150.0)
        assert run(acc.ingest_tick(ev)) is None

    def test_premium_above_dte_floor_passes(self):
        acc = make_accumulator(min_trades=1, min_sweeps=0, tier_map={"AAPL": 1})
        ev = make_event(ticker="AAPL", premium=600_000.0, dte=20, underlying_price=150.0)
        assert run(acc.ingest_tick(ev)) is not None

    def test_t2_lower_floor_passes_where_t1_would_reject(self):
        acc = make_accumulator(min_trades=1, min_sweeps=0, tier_map={"TSLA": 2})
        ev = make_event(ticker="TSLA", premium=200_000.0, dte=20, underlying_price=150.0)
        assert run(acc.ingest_tick(ev)) is not None

    def test_leaps_91_plus_dte_uses_highest_bucket(self):
        acc = make_accumulator(min_trades=1, min_sweeps=0, tier_map={"TSLA": 2})
        ev = make_event(ticker="TSLA", premium=1_100_000.0, dte=120, underlying_price=150.0)
        assert run(acc.ingest_tick(ev)) is not None

    def test_leaps_91_plus_dte_below_floor_rejected(self):
        acc = make_accumulator(min_trades=1, min_sweeps=0, tier_map={"TSLA": 2})
        ev = make_event(ticker="TSLA", premium=500_000.0, dte=120, underlying_price=150.0)
        assert run(acc.ingest_tick(ev)) is None

    def test_deep_otm_below_multiplied_floor_rejected(self):
        acc = make_accumulator(min_trades=1, min_sweeps=0,
                               deep_otm_multiplier=1.5, tier_map={"TSLA": 2})
        ev = make_event(ticker="TSLA", premium=30_000.0, dte=5,
                        strike=180.0, underlying_price=150.0)
        assert run(acc.ingest_tick(ev)) is None

    def test_deep_otm_above_multiplied_floor_passes(self):
        acc = make_accumulator(min_trades=1, min_sweeps=0,
                               deep_otm_multiplier=1.5, tier_map={"TSLA": 2})
        ev = make_event(ticker="TSLA", premium=40_000.0, dte=5,
                        strike=180.0, underlying_price=150.0)
        assert run(acc.ingest_tick(ev)) is not None

    def test_atm_uses_standard_floor_no_multiplier(self):
        acc = make_accumulator(min_trades=1, min_sweeps=0,
                               deep_otm_multiplier=1.5, tier_map={"AAPL": 1})
        ev = make_event(ticker="AAPL", premium=55_000.0, dte=5,
                        strike=151.5, underlying_price=150.0)
        assert run(acc.ingest_tick(ev)) is not None

    def test_underlying_price_zero_uses_standard_floor_no_otm(self):
        acc = make_accumulator(min_trades=1, min_sweeps=0,
                               deep_otm_multiplier=1.5, tier_map={"AAPL": 1})
        ev = make_event(ticker="AAPL", premium=55_000.0, dte=5,
                        strike=200.0, underlying_price=0.0)
        assert run(acc.ingest_tick(ev)) is not None

    def test_deep_otm_multiplier_1_no_extra_floor(self):
        acc = make_accumulator(min_trades=1, min_sweeps=0,
                               deep_otm_multiplier=1.0, tier_map={"TSLA": 2})
        ev = make_event(ticker="TSLA", premium=26_000.0, dte=5,
                        strike=180.0, underlying_price=150.0)
        assert run(acc.ingest_tick(ev)) is not None

    def test_min_sweeps_gate_rejects_when_not_enough_sweeps(self):
        acc = RepetitionAccumulator(
            min_trades=1, min_sweeps=2, sweep_bypass_premium=0.0,
            dte_premium_tiers={}, min_premium=50_000,
        )
        ev_sweep = make_event(
            ticker="AAPL", contract_type="CALL", strike=150.0, expiry="2026-06-20",
            premium=100_000.0, dte=15, underlying_price=150.0, trade_type="SWEEP"
        )
        run(acc.ingest_tick(ev_sweep))
        ev_block = make_event(
            ticker="AAPL", contract_type="CALL", strike=150.0, expiry="2026-06-20",
            premium=100_000.0, dte=15, underlying_price=150.0, trade_type="BLOCK"
        )
        assert run(acc.ingest_tick(ev_block)) is None

    def test_min_sweeps_gate_passes_when_enough_sweeps(self):
        acc = make_accumulator(min_trades=1, min_sweeps=2,
                               sweep_bypass_premium=0.0,
                               dte_premium_tiers={}, min_premium=50_000)
        for _ in range(2):
            ev = make_event(premium=100_000.0, dte=15,
                            underlying_price=150.0, trade_type="SWEEP")
            result = run(acc.ingest_tick(ev))
        assert result is not None

    def test_min_sweeps_zero_disabled(self):
        acc = make_accumulator(min_trades=1, min_sweeps=0,
                               dte_premium_tiers={}, min_premium=50_000)
        ev = make_event(premium=100_000.0, dte=15,
                        underlying_price=150.0, trade_type="BLOCK")
        assert run(acc.ingest_tick(ev)) is not None

    def test_sweep_bypass_fires_bypasses_min_sweeps(self):
        acc = make_accumulator(min_trades=1, min_sweeps=2,
                               sweep_bypass_premium=500_000.0,
                               dte_premium_tiers={}, min_premium=50_000)
        ev = make_event(premium=600_000.0, dte=15,
                        underlying_price=150.0, trade_type="SWEEP")
        result = run(acc.ingest_tick(ev))
        assert result is not None
        assert result.trade_count == 1

    def test_sweep_bypass_negative_two_events_must_meet_min_sweeps(self):
        acc = make_accumulator(min_trades=1, min_sweeps=2,
                               sweep_bypass_premium=500_000.0,
                               dte_premium_tiers={}, min_premium=50_000)
        ev = make_event(premium=600_000.0, dte=15,
                        underlying_price=150.0, trade_type="SWEEP")
        run(acc.ingest_tick(ev))
        result = run(acc.ingest_tick(ev))
        assert result is not None
        assert result.trade_count == 2

    def test_window_pruning_removes_old_events(self):
        acc = RepetitionAccumulator(
            window_minutes=10, min_trades=3, min_premium=50_000, dte_premium_tiers={},
        )
        old_ts = _ts(-700)
        new_ts = _ts(0)
        old_ev = make_event(premium=100_000.0, dte=15, underlying_price=150.0, timestamp=old_ts)
        new_ev = make_event(premium=100_000.0, dte=15, underlying_price=150.0, timestamp=new_ts)
        run(acc.ingest_tick(old_ev))
        run(acc.ingest_tick(old_ev))
        assert run(acc.ingest_tick(new_ev)) is None

    def test_dict_event_compat(self):
        acc = RepetitionAccumulator(min_trades=1, min_premium=50_000, dte_premium_tiers={})
        ev_dict = {
            "ticker": "AAPL", "contract_type": "CALL", "strike": 150.0,
            "expiry": "2026-06-20", "premium": 100_000.0, "trade_type": "SWEEP",
            "dte": 15, "underlying_price": 150.0, "order_side": "BUY",
            "timestamp": _ts(),
        }
        assert run(acc.ingest_tick(ev_dict)) is not None


# ---------------------------------------------------------------------------
# get_signal cooldown tests
# ---------------------------------------------------------------------------

class TestGetSignal:

    def _ep(self):
        return RepetitionEpisode(ticker="AAPL", contract_type="CALL")

    def test_none_ep_returns_none(self):
        acc = make_accumulator(signal_cooldown=5)
        assert run(acc.get_signal(_ts(), None)) is None

    def test_zero_cooldown_always_returns_ep(self):
        acc = make_accumulator(signal_cooldown=0)
        ep = self._ep()
        assert run(acc.get_signal(_ts(), ep)) is ep

    def test_first_signal_sets_last_signal_at(self):
        acc = make_accumulator(signal_cooldown=5)
        ep = self._ep()
        ts = _ts()
        run(acc.get_signal(ts, ep))
        assert ep.last_signal_at == ts

    def test_within_cooldown_suppressed(self):
        acc = make_accumulator(signal_cooldown=5)
        ep = self._ep()
        run(acc.get_signal(_ts(0), ep))
        assert run(acc.get_signal(_ts(60), ep)) is None

    def test_after_cooldown_passes(self):
        acc = make_accumulator(signal_cooldown=5)
        ep = self._ep()
        run(acc.get_signal(_ts(0), ep))
        assert run(acc.get_signal(_ts(310), ep)) is ep


# ---------------------------------------------------------------------------
# ingest shim backward-compat
# ---------------------------------------------------------------------------

class TestIngestShim:

    def test_ingest_gate1_fail_returns_none(self):
        acc = make_accumulator(min_trades=5, dte_premium_tiers={}, min_premium=50_000)
        ev = make_event(premium=100_000.0)
        assert run(acc.ingest(ev)) is None

    def test_ingest_passes_both_gates(self):
        acc = RepetitionAccumulator(
            min_trades=1, min_premium=50_000, signal_cooldown=0, dte_premium_tiers={},
        )
        ev = make_event(premium=100_000.0)
        assert run(acc.ingest(ev)) is not None

    def test_ingest_ts_int_conversion(self):
        """Timestamp as unix int is handled in ingest shim."""
        acc = RepetitionAccumulator(
            min_trades=1, min_premium=50_000, signal_cooldown=0, dte_premium_tiers={},
        )
        ev = make_event(premium=100_000.0)
        ev.timestamp = int(time.time())
        assert run(acc.ingest(ev)) is not None


# ---------------------------------------------------------------------------
# Alert level tests
# ---------------------------------------------------------------------------

class TestAlertLevel:

    def _ep_with_premium(self, total_premium: float, accelerating: bool = False) -> RepetitionEpisode:
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        n = 3 if accelerating else 1
        each = total_premium / n
        for i in range(n):
            e = MagicMock()
            e.premium = each
            e.timestamp = _ts(i * 10 if accelerating else 0)
            ep.events.append(e)
        return ep

    def test_watch_below_250k(self):
        assert make_accumulator().get_alert_level(self._ep_with_premium(100_000.0)) == "WATCH"

    def test_watch_at_249k(self):
        assert make_accumulator().get_alert_level(self._ep_with_premium(249_999.0)) == "WATCH"

    def test_alert_at_250k(self):
        assert make_accumulator().get_alert_level(self._ep_with_premium(250_000.0)) == "ALERT"

    def test_alert_between_250k_and_1m(self):
        assert make_accumulator().get_alert_level(self._ep_with_premium(500_000.0)) == "ALERT"

    def test_strong_signal_at_1m(self):
        assert make_accumulator().get_alert_level(self._ep_with_premium(1_000_000.0)) == "STRONG_SIGNAL"

    def test_strong_signal_between_1m_and_2m(self):
        assert make_accumulator().get_alert_level(self._ep_with_premium(1_500_000.0)) == "STRONG_SIGNAL"

    def test_conviction_at_2m(self):
        assert make_accumulator().get_alert_level(self._ep_with_premium(2_000_000.0)) == "CONVICTION"

    def test_conviction_above_2m(self):
        assert make_accumulator().get_alert_level(self._ep_with_premium(5_000_000.0)) == "CONVICTION"

    def test_conviction_accelerating_above_500k(self):
        acc = make_accumulator()
        ep = self._ep_with_premium(600_000.0, accelerating=True)
        assert ep.is_accelerating is True
        assert acc.get_alert_level(ep) == "CONVICTION"

    def test_conviction_accelerating_at_exactly_500k(self):
        acc = make_accumulator()
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        each = 500_000.0 / 3
        for i in range(3):
            e = MagicMock()
            e.premium   = each
            e.timestamp = _ts(i * 10)
            ep.events.append(e)
        assert ep.is_accelerating is True
        assert abs(ep.total_premium - 500_000.0) < 0.01
        assert acc.get_alert_level(ep) == "CONVICTION"

    def test_conviction_accelerating_below_threshold_is_alert(self):
        acc = make_accumulator()
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        for i in range(3):
            e = MagicMock()
            e.premium   = 100_000.0
            e.timestamp = _ts(i * 10)
            ep.events.append(e)
        assert ep.is_accelerating is True
        assert ep.total_premium == 300_000.0
        assert acc.get_alert_level(ep) == "ALERT"


# ---------------------------------------------------------------------------
# set_tier_map test
# ---------------------------------------------------------------------------

class TestSetTierMap:

    def test_set_tier_map_updates_internal_map(self):
        acc = make_accumulator(tier_map={})
        acc.set_tier_map({"AAPL": 1, "TSLA": 2})
        assert acc._tier_map == {"AAPL": 1, "TSLA": 2}

    def test_tier_map_used_in_premium_floor_after_set(self):
        acc = make_accumulator(tier_map={})
        acc.set_tier_map({"AAPL": 1})
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        e = MagicMock()
        e.dte = 20
        ep.events = [e]
        assert acc._get_episode_min_premium(ep) == 500_000


# ---------------------------------------------------------------------------
# cleanup_expired test
# ---------------------------------------------------------------------------

class TestCleanupExpired:

    def test_expired_episodes_are_removed(self):
        acc = RepetitionAccumulator(window_minutes=10, min_trades=1,
                                    min_premium=50_000, dte_premium_tiers={})
        old_ts = _ts(-700)
        ev = make_event(premium=100_000.0, timestamp=old_ts)
        run(acc.ingest_tick(ev))
        assert len(acc._episodes) == 1
        key = list(acc._episodes.keys())[0]
        acc._episodes[key].last_seen = old_ts
        removed = run(acc.cleanup_expired())
        assert removed == 1
        assert len(acc._episodes) == 0

    def test_active_episodes_not_removed(self):
        acc = RepetitionAccumulator(window_minutes=10, min_trades=1,
                                    min_premium=50_000, dte_premium_tiers={})
        run(acc.ingest_tick(make_event(premium=100_000.0, timestamp=_ts())))
        assert run(acc.cleanup_expired()) == 0
        assert len(acc._episodes) == 1


# ---------------------------------------------------------------------------
# _DEFAULT_DTE_PREMIUM_TIERS constant sanity check
# ---------------------------------------------------------------------------

class TestDefaultDtePremiumTiers:

    def test_all_expected_keys_present(self):
        assert set(_DEFAULT_DTE_PREMIUM_TIERS.keys()) == {7, 30, 90, 9999}

    def test_t1_t2_t3_floors_are_tuples_of_two(self):
        for key, val in _DEFAULT_DTE_PREMIUM_TIERS.items():
            assert isinstance(val, tuple) and len(val) == 2

    def test_t1_always_higher_than_t2_t3(self):
        for key, (t1, t2t3) in _DEFAULT_DTE_PREMIUM_TIERS.items():
            assert t1 >= t2t3

    def test_floors_increase_with_dte(self):
        sorted_keys = sorted(_DEFAULT_DTE_PREMIUM_TIERS)
        prev_t1 = prev_t2t3 = 0
        for k in sorted_keys:
            t1, t2t3 = _DEFAULT_DTE_PREMIUM_TIERS[k]
            assert t1 >= prev_t1 and t2t3 >= prev_t2t3
            prev_t1, prev_t2t3 = t1, t2t3


# ---------------------------------------------------------------------------
# Coverage gaps — lines 128-129, 294, 341→344, 471, 504-505, 577
# ---------------------------------------------------------------------------

class TestCoverageGaps:
    """
    Targets the 7 remaining uncovered statements/branches in
    signals/repetition_accumulator.py after the S4-POST-1/2 push.

    L128-129  _DictEventWrapper.timestamp fallback (missing key)
    L294      log.debug in DTE overflow path (T2 custom-tiers overflow)
    L341→344  standard floor reject, else-branch (ATM/STANDARD_OTM below floor)
    L471      get_signal int-timestamp coercion
    L504-505  ingest() shim int-timestamp coercion
    L577      cleanup_expired ep.last_seen=None guard
    """

    # ------------------------------------------------------------------
    # L128-129 — _DictEventWrapper timestamp missing-key fallback
    # ------------------------------------------------------------------

    def test_dict_event_wrapper_missing_timestamp_uses_now(self):
        """
        L128: self.timestamp = d.get("timestamp") or datetime.now(timezone.utc)

        When the dict has no 'timestamp' key, d.get("timestamp") returns None,
        the `or` short-circuits to datetime.now(timezone.utc) (L129).
        Both the None-result branch of get() and the datetime.now() call
        are on lines 128-129 and are only reached when the key is absent.
        """
        d = {
            "premium": 50_000.0,
            "trade_type": "SWEEP",
            "dte": 5,
            "underlying_price": 150.0,
            "order_side": "BUY",
            "contract_type": "CALL",
            # 'timestamp' intentionally omitted
        }
        before = datetime.now(timezone.utc)
        w = _DictEventWrapper(d)
        after = datetime.now(timezone.utc)
        assert isinstance(w.timestamp, datetime)
        assert w.timestamp.tzinfo is not None, "fallback must be timezone-aware"
        assert before <= w.timestamp <= after, (
            "fallback timestamp must be approximately now()"
        )

    def test_dict_event_wrapper_none_timestamp_uses_now(self):
        """
        L128-129: d.get("timestamp") returns None explicitly.
        The `or` branch fires, executing datetime.now(timezone.utc).
        """
        d = {
            "premium": 50_000.0,
            "trade_type": "SWEEP",
            "dte": 5,
            "underlying_price": 150.0,
            "order_side": "BUY",
            "contract_type": "CALL",
            "timestamp": None,  # falsy -> or branch fires
        }
        w = _DictEventWrapper(d)
        assert isinstance(w.timestamp, datetime)
        assert w.timestamp.tzinfo is not None

    # ------------------------------------------------------------------
    # L294 — log.debug in DTE overflow path, T2 ticker
    # ------------------------------------------------------------------

    def test_dte_overflow_log_debug_t2_ticker(self):
        """
        L294: log.debug(...) fires when DTE exceeds all explicit tier keys.

        The existing test_dte_exceeds_all_explicit_keys_uses_highest uses a
        T1 ticker (col=0). L294 is reached via any overflow but the debug
        call itself was previously uncovered because coverage measures
        statement execution, not branching.

        This test uses a T2 ticker (col=1) to hit L294 on the T2/T3 path
        and asserts the correct T2 floor is returned from the highest bucket.
        Custom tiers: max key=30. DTE=9999 triggers overflow.
        T2 floor at key=30: $200_000.
        """
        custom_tiers = {10: (100_000, 50_000), 30: (500_000, 200_000)}
        acc = RepetitionAccumulator(
            dte_premium_tiers=custom_tiers,
            tier_map={"TSLA": 2},
        )
        ep = RepetitionEpisode(ticker="TSLA", contract_type="CALL")
        e = MagicMock()
        e.dte = 9999
        ep.events = [e]
        result = acc._get_episode_min_premium(ep)
        assert result == 200_000, (
            "T2 ticker overflow path must return T2/T3 column of highest bucket"
        )

    # ------------------------------------------------------------------
    # L341→344 — standard floor reject (else-branch, non-DEEP_OTM)
    # ------------------------------------------------------------------

    def test_standard_otm_below_floor_rejected_via_else_branch(self):
        """
        L341→344: the else-branch of
          `if self.deep_otm_multiplier > 1.0 and otm_band == 'DEEP_OTM':`
        executes `if ep.total_premium < effective_min_prem: return None`.

        This branch is taken when the contract is ATM or STANDARD_OTM
        (not DEEP_OTM) and the premium is below the standard DTE floor.
        All previous tests that rejected via the floor either hit the DEEP_OTM
        path or passed a T1 ticker with a large DTE bucket floor.

        Setup: T1/AAPL, DTE=5 (floor=$50K), STANDARD_OTM at 5% OTM,
        premium=$30K < $50K -> return None via else-branch (L341→344).
        """
        acc = RepetitionAccumulator(
            min_trades=1,
            min_sweeps=0,
            min_premium=1,
            dte_premium_tiers=dict(_DEFAULT_DTE_PREMIUM_TIERS),
            deep_otm_multiplier=1.5,
            tier_map={"AAPL": 1},
        )
        # strike=157.5, underlying=150 -> 5% OTM -> STANDARD_OTM (not DEEP_OTM)
        # T1, DTE=5 -> floor=$50K; premium=$30K < $50K -> rejected via else
        ev = make_event(
            ticker="AAPL",
            premium=30_000.0,
            dte=5,
            strike=157.5,
            underlying_price=150.0,
        )
        assert run(acc.ingest_tick(ev)) is None, (
            "STANDARD_OTM contract below standard floor must be rejected via else-branch"
        )

    def test_atm_below_floor_rejected_via_else_branch(self):
        """
        L341→344: ATM contract (strike==underlying) below DTE floor.
        multiplier > 1.0 but otm_band == 'ATM' so DEEP_OTM branch is skipped.
        Else-branch fires: premium < effective_min_prem -> return None.

        T1/AAPL, DTE=5 (floor=$50K), ATM (0% OTM), premium=$20K -> None.
        """
        acc = RepetitionAccumulator(
            min_trades=1,
            min_sweeps=0,
            min_premium=1,
            dte_premium_tiers=dict(_DEFAULT_DTE_PREMIUM_TIERS),
            deep_otm_multiplier=1.5,
            tier_map={"AAPL": 1},
        )
        ev = make_event(
            ticker="AAPL",
            premium=20_000.0,
            dte=5,
            strike=150.0,  # ATM
            underlying_price=150.0,
        )
        assert run(acc.ingest_tick(ev)) is None, (
            "ATM contract below standard floor must be rejected via else-branch"
        )

    # ------------------------------------------------------------------
    # L471 — get_signal int-timestamp coercion
    # ------------------------------------------------------------------

    def test_get_signal_int_timestamp_coercion(self):
        """
        L471: `ev_ts = datetime.fromtimestamp(ev_ts, tz=timezone.utc)`

        get_signal() accepts `ts` as a datetime but internally coerces
        int/float via `if isinstance(ev_ts, (int, float))` on L470.
        This branch is never exercised by the existing TestGetSignal tests
        because they all pass _ts() (a datetime object).

        Pass ts as a unix int directly; the coercion must produce a valid
        datetime and the signal must be returned (first call, no cooldown
        applied when last_signal_at is None).
        """
        acc = make_accumulator(signal_cooldown=5)
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        unix_ts = int(time.time())
        result = run(acc.get_signal(unix_ts, ep))
        assert result is ep, (
            "get_signal must accept int ts, coerce it, and return ep on first call"
        )
        assert isinstance(ep.last_signal_at, datetime), (
            "last_signal_at must be a datetime after coercion"
        )

    def test_get_signal_float_timestamp_coercion(self):
        """
        L471: same guard, float variant. time.time() returns a float;
        the isinstance check covers both int and float.
        """
        acc = make_accumulator(signal_cooldown=0)
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        result = run(acc.get_signal(time.time(), ep))
        assert result is ep

    # ------------------------------------------------------------------
    # L504-505 — ingest() shim int-timestamp coercion
    # ------------------------------------------------------------------

    def test_ingest_shim_int_timestamp_coercion(self):
        """
        L504-505 in ingest():
          if isinstance(ev_ts, (int, float)):
              ev_ts = datetime.fromtimestamp(ev_ts, tz=timezone.utc)

        The existing test_ingest_ts_int_conversion in TestIngestShim sets
        ev.timestamp on a MagicMock and runs ingest(), but it exercises the
        ingest_tick() coercion path (L~360), NOT the identical guard in
        the ingest() shim itself (L504-505). Those two guards are on
        separate lines and coverage tracks them independently.

        This test constructs the episode manually so ingest_tick() returns
        immediately (Gate-1 passes) and then calls get_signal with the
        int-coercion path still live in ingest().
        """
        acc = RepetitionAccumulator(
            min_trades=1,
            min_premium=50_000,
            signal_cooldown=0,
            dte_premium_tiers={},
        )
        ev = make_event(premium=100_000.0)
        ev.timestamp = int(time.time())  # int -> triggers L504 isinstance guard
        result = run(acc.ingest(ev))
        assert result is not None, (
            "ingest() with int timestamp must coerce and return episode"
        )

    def test_ingest_shim_float_timestamp_coercion(self):
        """
        L504-505: float variant. Ensures the isinstance branch is exercised
        with a float (time.time()) rather than an int.
        """
        acc = RepetitionAccumulator(
            min_trades=1,
            min_premium=50_000,
            signal_cooldown=0,
            dte_premium_tiers={},
        )
        ev = make_event(premium=100_000.0)
        ev.timestamp = time.time()  # float
        result = run(acc.ingest(ev))
        assert result is not None

    # ------------------------------------------------------------------
    # L577 — cleanup_expired ep.last_seen=None guard
    # ------------------------------------------------------------------

    def test_cleanup_expired_last_seen_none_not_expired(self):
        """
        L577: `if ep.last_seen and (now - ep.last_seen) > self.window`

        The `ep.last_seen and` short-circuit must evaluate to False when
        last_seen is None, keeping the episode alive (not expiring it).

        The existing cleanup tests always set last_seen to a real timestamp
        (either via ingest_tick which assigns ep.last_seen, or manually via
        acc._episodes[key].last_seen = old_ts). Neither path exercises the
        last_seen=None branch because a freshly-ingested episode always has
        last_seen set before cleanup runs.

        This test injects an episode directly with last_seen=None and asserts
        cleanup_expired returns 0 (not expired) and the episode remains.
        """
        acc = RepetitionAccumulator(
            window_minutes=10, min_trades=1, min_premium=50_000, dte_premium_tiers={},
        )
        # Inject a synthetic episode with last_seen=None directly
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        assert ep.last_seen is None  # dataclass default
        acc._episodes["AAPL|CALL|0.00|"] = ep

        removed = run(acc.cleanup_expired())
        assert removed == 0, (
            "Episode with last_seen=None must not be expired — "
            "the `ep.last_seen and` short-circuit must guard the subtraction"
        )
        assert "AAPL|CALL|0.00|" in acc._episodes
