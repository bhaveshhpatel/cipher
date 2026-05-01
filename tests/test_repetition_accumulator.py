"""
tests/test_repetition_accumulator.py

S4 — Apex L2 Dual-Window Accumulator test suite.

Coverage targets (100% line + branch):
  - DTE-adjusted premium floors (all 4 DTE buckets x T1 and T2/T3)
  - OTM classification: ATM (<=2%), standard OTM (2-12%), deep OTM (>12%)
  - Deep OTM multiplier gate: reject below, pass above
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
  - ingest shim backward-compat
  - cleanup_expired
  - set_tier_map
  - _get_episode_min_premium: empty dte_premium_tiers fallback
  - _get_episode_min_premium: DTE exceeds all explicit keys
  - _get_episode_min_premium: unknown ticker defaults to T1 strict (Finding 2)
  - _is_single_whale_sweep: all False branches
  - Window pruning
  - _DictEventWrapper allocated at module level (not per-tick)

QA scenarios exercised:
  QA-09 (accumulate/no emit), QA-10 (bypass positive), QA-11 (bypass negative),
  QA-17 (deep OTM pass), QA-18 (missing underlying fallback),
  QA-21 (WATCH), QA-22 (ALERT), QA-23 (STRONG_SIGNAL), QA-24 (CONVICTION)
"""
import asyncio
import sys
import os
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
    # asyncio.run() preferred over deprecated get_event_loop().run_until_complete()
    # (Finding 6 — deprecated in Python 3.10+)
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
    """
    Build a minimal mock OptionsFlowEvent-compatible object.
    All attributes accessed by the accumulator are present.
    """
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
    """
    Create an accumulator with S4 defaults suitable for unit tests.
    Overrides via kwargs.
    """
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
        """
        Finding 1 (panel deliberation May 1 2026): `otm_band` was stored but
        never consumed by _classify_otm. Removed from constructor to prevent
        callers from believing the bands are configurable when they are not.
        """
        import inspect
        sig = inspect.signature(RepetitionAccumulator.__init__)
        assert "otm_band" not in sig.parameters, (
            "otm_band should have been removed — it was a dead stored attribute"
        )


# ---------------------------------------------------------------------------
# Finding 2 — unknown-ticker default tier is T1 (strict)
# ---------------------------------------------------------------------------

class TestUnknownTierDefaultIsT1Strict:
    """
    Panel deliberation May 1 2026 — Finding 2:
    Unknown tickers must default to T1 (strict) floor, not T2/T3 (lenient).
    A ticker absent from the registry should not get easier qualification
    than a known large-cap. set_tier_map() assigns correct tiers once ready.
    """

    def test_unknown_ticker_defaults_to_t1_floor(self):
        acc = make_accumulator(tier_map={})  # AAPL not in map
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        e = MagicMock()
        e.dte = 20  # 8-30 bucket: T1=$500K, T2/T3=$100K
        ep.events = [e]
        # Should return T1 strict floor ($500K), not T2/T3 lenient ($100K)
        assert acc._get_episode_min_premium(ep) == 500_000, (
            "Unknown ticker must use T1 (strict) floor — Finding 2 deliberation"
        )

    def test_known_t2_ticker_still_uses_t2_floor(self):
        """Sanity: explicitly T2 tickers still get T2/T3 floor after the fix."""
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
        """
        Finding 7: _DictEventWrapper must be defined at module level,
        not inside ingest_tick, so it is not re-created on every hot-path call.
        """
        import signals.repetition_accumulator as mod
        assert hasattr(mod, "_DictEventWrapper"), (
            "_DictEventWrapper should be a module-level class (Finding 7)"
        )
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
        e = object()  # no .premium attribute
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
            e.timestamp = _ts(i * 10)  # 0s, 10s, 20s
            events.append(e)
        ep.events = events
        assert ep.is_accelerating is True

    def test_is_accelerating_false_span_exceeds_60s(self):
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        events = []
        for i in range(3):
            e = MagicMock()
            e.timestamp = _ts(i * 35)  # 0s, 35s, 70s
            events.append(e)
        ep.events = events
        assert ep.is_accelerating is False

    def test_is_accelerating_exception_returns_false(self):
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        events = [MagicMock(), MagicMock(), MagicMock()]
        for e in events:
            e.timestamp = "not_a_datetime"  # will throw on comparison
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
    """
    Premium-weighted direction across episode events.
    Verifies the S2 deliberation invariant:
      SELL + PUT -> REPEAT_BUY even when mixed with neutral events.
    """

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
        """Core S2 invariant: SELL PUT campaign resolves to REPEAT_BUY."""
        ep = RepetitionEpisode(ticker="SPY", contract_type="PUT")
        ep.events = [
            self._make_ev("SELL", "PUT", 1_800_000.0),
            self._make_ev("UNKNOWN", "PUT", 5_000.0),  # weak last tick
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
        """Equal buy and sell premium -> REPEAT_BUY (>= comparison)."""
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        ep.events = [
            self._make_ev("BUY", "CALL", 100_000.0),
            self._make_ev("BUY", "PUT", 100_000.0),  # REPEAT_SELL
        ]
        assert ep.dominant_direction == "REPEAT_BUY"

    def test_unknown_order_side_falls_back_to_contract_type(self):
        """UNKNOWN order_side: CALL -> REPEAT_BUY via fallback convention."""
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        ep.events = [self._make_ev("UNKNOWN", "CALL", 200_000.0)]
        assert ep.dominant_direction == "REPEAT_BUY"

    def test_empty_events_defaults_to_repeat_buy(self):
        """No events: buy_prem == sell_prem == 0.0 -> REPEAT_BUY."""
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        ep.events = []
        assert ep.dominant_direction == "REPEAT_BUY"

    def test_premium_weighted_mixed_episode(self):
        """$1.8M SELL PUT + $200K BUY PUT -> SELL PUT wins -> REPEAT_BUY."""
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
        """At exactly 2% OTM -> ATM band (Issue 6: <= 0.02)."""
        result = RepetitionAccumulator._classify_otm(153.0, 150.0)
        assert result == "ATM"

    def test_atm_below_2pct(self):
        result = RepetitionAccumulator._classify_otm(151.0, 150.0)
        assert result == "ATM"

    def test_atm_exactly_at_strike(self):
        result = RepetitionAccumulator._classify_otm(150.0, 150.0)
        assert result == "ATM"

    def test_standard_otm_just_above_2pct(self):
        """2.01% OTM -> STANDARD_OTM (not ATM)."""
        result = RepetitionAccumulator._classify_otm(153.016, 150.0)
        assert result == "STANDARD_OTM"

    def test_standard_otm_at_12pct(self):
        """Exactly 12% OTM -> STANDARD_OTM (not DEEP_OTM)."""
        result = RepetitionAccumulator._classify_otm(168.0, 150.0)
        assert result == "STANDARD_OTM"

    def test_deep_otm_just_above_12pct(self):
        """12.01% OTM -> DEEP_OTM."""
        result = RepetitionAccumulator._classify_otm(168.02, 150.0)
        assert result == "DEEP_OTM"

    def test_deep_otm_far_out(self):
        result = RepetitionAccumulator._classify_otm(200.0, 150.0)
        assert result == "DEEP_OTM"

    def test_underlying_price_zero_returns_unknown(self):
        """No underlying price -> UNKNOWN, no OTM classification attempted."""
        result = RepetitionAccumulator._classify_otm(150.0, 0.0)
        assert result == "UNKNOWN"

    def test_underlying_price_negative_returns_unknown(self):
        result = RepetitionAccumulator._classify_otm(150.0, -1.0)
        assert result == "UNKNOWN"

    def test_put_below_strike_atm(self):
        """PUT strike below underlying: abs() makes it ATM within 2%."""
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

    def test_unknown_tier_defaults_to_t1_strict(
        self,
    ):
        """
        Finding 2 (panel deliberation May 1 2026):
        Ticker not in tier_map -> defaults to tier 1 (T1 strict floor).
        Prevents low-float noise from qualifying more easily than known large-caps
        during registry warmup.
        """
        acc = make_accumulator(tier_map={})  # UNKNOWN not in map
        ep = self._ep("UNKNOWN", 20)  # 8-30 bucket
        # T1 strict floor = $500K, T2/T3 lenient floor = $100K
        assert acc._get_episode_min_premium(ep) == 500_000

    def test_dte_at_exact_bucket_boundary_7(self):
        """DTE==7 should match the 7-bucket (inclusive upper bound)."""
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
        """
        DTE > 9999 (impossible in practice but tests the overflow branch).
        The accumulator uses the highest-key bucket.
        """
        custom_tiers = {10: (100_000, 50_000), 30: (500_000, 200_000)}
        acc = RepetitionAccumulator(
            dte_premium_tiers=custom_tiers,
            tier_map={"AAPL": 1},
        )
        ep = self._ep("AAPL", 9999)  # exceeds 30 (max key)
        assert acc._get_episode_min_premium(ep) == 500_000

    def test_empty_episode_events_uses_dte_zero(self):
        """No events in episode -> dte defaults to 0 -> matches 7-bucket."""
        acc = make_accumulator(tier_map={"AAPL": 1})
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        # ep.events is empty
        assert acc._get_episode_min_premium(ep) == 50_000


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
        """QA-10: len==1, SWEEP, premium >= bypass threshold."""
        acc = make_accumulator(sweep_bypass_premium=500_000.0)
        ep = self._ep_with_events(1, trade_type="SWEEP", premium_each=600_000.0)
        assert acc._is_single_whale_sweep(ep) is True

    def test_bypass_negative_two_events(self):
        """QA-11: len==2 -> bypass does NOT fire regardless of premium."""
        acc = make_accumulator(sweep_bypass_premium=500_000.0)
        ep = self._ep_with_events(2, trade_type="SWEEP", premium_each=600_000.0)
        assert acc._is_single_whale_sweep(ep) is False

    def test_bypass_negative_wrong_trade_type(self):
        """len==1 but trade_type is BLOCK -> bypass does not fire."""
        acc = make_accumulator(sweep_bypass_premium=500_000.0)
        ep = self._ep_with_events(1, trade_type="BLOCK", premium_each=600_000.0)
        assert acc._is_single_whale_sweep(ep) is False

    def test_bypass_negative_premium_below_threshold(self):
        """len==1, SWEEP, but premium < threshold."""
        acc = make_accumulator(sweep_bypass_premium=500_000.0)
        ep = self._ep_with_events(1, trade_type="SWEEP", premium_each=400_000.0)
        assert acc._is_single_whale_sweep(ep) is False

    def test_bypass_negative_premium_exactly_at_threshold(self):
        """premium == threshold -> passes (>= comparison)."""
        acc = make_accumulator(sweep_bypass_premium=500_000.0)
        ep = self._ep_with_events(1, trade_type="SWEEP", premium_each=500_000.0)
        assert acc._is_single_whale_sweep(ep) is True

    def test_bypass_case_insensitive_trade_type(self):
        """trade_type 'sweep' (lowercase) also triggers bypass."""
        acc = make_accumulator(sweep_bypass_premium=500_000.0)
        ep = self._ep_with_events(1, trade_type="sweep", premium_each=600_000.0)
        assert acc._is_single_whale_sweep(ep) is True


# ---------------------------------------------------------------------------
# ingest_tick integration tests — Gate-1 full pipeline
# ---------------------------------------------------------------------------

class TestIngestTick:

    # ── min_trades gate ────────────────────────────────────────────────

    def test_single_event_does_not_qualify_min_trades(self):
        """QA-09: accumulates but does not emit (min_trades=3)."""
        acc = make_accumulator(min_trades=3, min_sweeps=0)
        ev = make_event(premium=500_000.0, dte=15, underlying_price=150.0)
        result = run(acc.ingest_tick(ev))
        assert result is None

    def test_three_events_meets_min_trades(self):
        acc = make_accumulator(min_trades=3, min_sweeps=0,
                               dte_premium_tiers={}, min_premium=50_000)
        ev = make_event(premium=100_000.0, dte=15, underlying_price=150.0)
        run(acc.ingest_tick(ev))
        run(acc.ingest_tick(ev))
        result = run(acc.ingest_tick(ev))
        assert result is not None

    # ── DTE-adjusted floor gate ────────────────────────────────────────

    def test_premium_below_dte_floor_rejected(self):
        """
        T1 ticker, 20 DTE bucket (floor=$500K), episode premium $200K -> rejected.
        """
        acc = make_accumulator(
            min_trades=1,
            min_sweeps=0,
            tier_map={"AAPL": 1},
        )
        ev = make_event(ticker="AAPL", premium=200_000.0, dte=20,
                        underlying_price=150.0)
        result = run(acc.ingest_tick(ev))
        assert result is None

    def test_premium_above_dte_floor_passes(self):
        """T1 ticker, 20 DTE (floor=$500K), episode premium $600K -> passes."""
        acc = make_accumulator(
            min_trades=1,
            min_sweeps=0,
            tier_map={"AAPL": 1},
        )
        ev = make_event(ticker="AAPL", premium=600_000.0, dte=20,
                        underlying_price=150.0)
        result = run(acc.ingest_tick(ev))
        assert result is not None

    def test_t2_lower_floor_passes_where_t1_would_reject(self):
        """T2 ticker, 20 DTE (T2/T3 floor=$100K), episode premium $200K -> passes."""
        acc = make_accumulator(
            min_trades=1,
            min_sweeps=0,
            tier_map={"TSLA": 2},
        )
        ev = make_event(ticker="TSLA", premium=200_000.0, dte=20,
                        underlying_price=150.0)
        result = run(acc.ingest_tick(ev))
        assert result is not None

    def test_leaps_91_plus_dte_uses_highest_bucket(self):
        """
        T2 ticker, 120 DTE (91+ bucket, T2/T3 floor=$1M).
        Premium $1.1M -> passes. LEAPS are not blindly excluded.
        """
        acc = make_accumulator(
            min_trades=1,
            min_sweeps=0,
            tier_map={"TSLA": 2},
        )
        ev = make_event(ticker="TSLA", premium=1_100_000.0, dte=120,
                        underlying_price=150.0)
        result = run(acc.ingest_tick(ev))
        assert result is not None

    def test_leaps_91_plus_dte_below_floor_rejected(self):
        """T2, 120 DTE (floor=$1M), episode premium $500K -> rejected."""
        acc = make_accumulator(
            min_trades=1,
            min_sweeps=0,
            tier_map={"TSLA": 2},
        )
        ev = make_event(ticker="TSLA", premium=500_000.0, dte=120,
                        underlying_price=150.0)
        result = run(acc.ingest_tick(ev))
        assert result is None

    # ── Deep OTM multiplier gate ───────────────────────────────────────

    def test_deep_otm_below_multiplied_floor_rejected(self):
        """
        QA-17 reject path:
        T2, 5 DTE (floor=$25K), deep OTM 20%, multiplied floor=$37.5K.
        Episode premium $30K -> below multiplied floor -> rejected.
        """
        acc = make_accumulator(
            min_trades=1,
            min_sweeps=0,
            deep_otm_multiplier=1.5,
            tier_map={"TSLA": 2},
        )
        # 180 / 150 = 1.20 -> 20% OTM -> DEEP_OTM
        ev = make_event(ticker="TSLA", premium=30_000.0, dte=5,
                        strike=180.0, underlying_price=150.0)
        result = run(acc.ingest_tick(ev))
        assert result is None

    def test_deep_otm_above_multiplied_floor_passes(self):
        """
        QA-17 pass path:
        T2, 5 DTE (floor=$25K), deep OTM 20%, multiplied floor=$37.5K.
        Episode premium $40K -> above multiplied floor -> passes.
        """
        acc = make_accumulator(
            min_trades=1,
            min_sweeps=0,
            deep_otm_multiplier=1.5,
            tier_map={"TSLA": 2},
        )
        ev = make_event(ticker="TSLA", premium=40_000.0, dte=5,
                        strike=180.0, underlying_price=150.0)
        result = run(acc.ingest_tick(ev))
        assert result is not None

    def test_atm_uses_standard_floor_no_multiplier(self):
        """
        ATM contract (<=2% OTM) uses standard DTE floor, no multiplier applied.
        T1, 5 DTE (floor=$50K), ATM at 1% OTM.
        Episode premium $55K -> passes at standard floor.
        """
        acc = make_accumulator(
            min_trades=1,
            min_sweeps=0,
            deep_otm_multiplier=1.5,
            tier_map={"AAPL": 1},
        )
        # 151.5 / 150 = 1% OTM -> ATM band
        ev = make_event(ticker="AAPL", premium=55_000.0, dte=5,
                        strike=151.5, underlying_price=150.0)
        result = run(acc.ingest_tick(ev))
        assert result is not None

    def test_underlying_price_zero_uses_standard_floor_no_otm(self):
        """
        QA-18: underlying_price==0 -> UNKNOWN OTM band -> standard floor only.
        T1, 5 DTE (floor=$50K), premium $55K -> passes without OTM check.
        """
        acc = make_accumulator(
            min_trades=1,
            min_sweeps=0,
            deep_otm_multiplier=1.5,
            tier_map={"AAPL": 1},
        )
        ev = make_event(ticker="AAPL", premium=55_000.0, dte=5,
                        strike=200.0, underlying_price=0.0)  # deep OTM if price known
        result = run(acc.ingest_tick(ev))
        assert result is not None

    def test_deep_otm_multiplier_1_no_extra_floor(self):
        """deep_otm_multiplier=1.0 -> multiplier effectively disabled; standard floor only."""
        acc = make_accumulator(
            min_trades=1,
            min_sweeps=0,
            deep_otm_multiplier=1.0,
            tier_map={"TSLA": 2},
        )
        ev = make_event(ticker="TSLA", premium=26_000.0, dte=5,
                        strike=180.0, underlying_price=150.0)
        result = run(acc.ingest_tick(ev))
        assert result is not None

    # ── min_sweeps gate ────────────────────────────────────────────────

    def test_min_sweeps_gate_rejects_when_not_enough_sweeps(self):
        """
        Finding 4 (panel deliberation May 1 2026) — isolated sweep-count gate test:
        min_sweeps=2, min_trades=1, min_premium disabled.
        Ingest exactly 1 SWEEP + 1 BLOCK = 2 events, only 1 SWEEP.
        The ONLY failing gate is sweep_count (1) < min_sweeps (2).
        Result must be None specifically because sweep gate fires.
        """
        acc = RepetitionAccumulator(
            min_trades=1,
            min_sweeps=2,
            sweep_bypass_premium=0.0,
            dte_premium_tiers={},
            min_premium=50_000,
        )
        # Event 1: SWEEP, premium=$100K — passes min_trades(1), min_premium(50K)
        ev_sweep = make_event(
            ticker="AAPL", contract_type="CALL", strike=150.0, expiry="2026-06-20",
            premium=100_000.0, dte=15, underlying_price=150.0, trade_type="SWEEP"
        )
        run(acc.ingest_tick(ev_sweep))

        # Event 2: BLOCK, same contract key — still only 1 SWEEP in episode
        ev_block = make_event(
            ticker="AAPL", contract_type="CALL", strike=150.0, expiry="2026-06-20",
            premium=100_000.0, dte=15, underlying_price=150.0, trade_type="BLOCK"
        )
        result = run(acc.ingest_tick(ev_block))

        # trade_count=2 >= min_trades(1) ✓
        # total_premium=$200K >= min_premium($50K) ✓
        # sweep_count=1 < min_sweeps(2) -> None  <- this is the only failing gate
        assert result is None, (
            "sweep_count=1 should fail min_sweeps=2; no other gate should cause None here"
        )

    def test_min_sweeps_gate_passes_when_enough_sweeps(self):
        """min_sweeps=2, episode has 2 SWEEP events -> passes."""
        acc = make_accumulator(
            min_trades=1,
            min_sweeps=2,
            sweep_bypass_premium=0.0,
            dte_premium_tiers={},
            min_premium=50_000,
        )
        for _ in range(2):
            ev = make_event(premium=100_000.0, dte=15,
                            underlying_price=150.0, trade_type="SWEEP")
            result = run(acc.ingest_tick(ev))
        assert result is not None

    def test_min_sweeps_zero_disabled(self):
        """min_sweeps=0 -> sweep gate is skipped entirely."""
        acc = make_accumulator(
            min_trades=1,
            min_sweeps=0,
            dte_premium_tiers={},
            min_premium=50_000,
        )
        ev = make_event(premium=100_000.0, dte=15,
                        underlying_price=150.0, trade_type="BLOCK")
        result = run(acc.ingest_tick(ev))
        assert result is not None

    def test_sweep_bypass_fires_bypasses_min_sweeps(self):
        """
        QA-10: len==1, SWEEP, premium >= bypass_premium ($500K).
        Even though min_sweeps=2, bypass fires -> qualifies.
        """
        acc = make_accumulator(
            min_trades=1,
            min_sweeps=2,
            sweep_bypass_premium=500_000.0,
            dte_premium_tiers={},
            min_premium=50_000,
        )
        ev = make_event(premium=600_000.0, dte=15,
                        underlying_price=150.0, trade_type="SWEEP")
        result = run(acc.ingest_tick(ev))
        assert result is not None
        assert result.trade_count == 1

    def test_sweep_bypass_negative_two_events_must_meet_min_sweeps(self):
        """
        QA-11: Two events in episode -> bypass does not fire.
        min_sweeps=2, both events are SWEEP -> still qualifies via min_sweeps.
        This test uses 2 SWEEP events so it qualifies to isolate from
        the rejection case (tested separately in test_min_sweeps_gate_rejects).
        """
        acc = make_accumulator(
            min_trades=1,
            min_sweeps=2,
            sweep_bypass_premium=500_000.0,
            dte_premium_tiers={},
            min_premium=50_000,
        )
        ev = make_event(premium=600_000.0, dte=15,
                        underlying_price=150.0, trade_type="SWEEP")
        run(acc.ingest_tick(ev))  # event 1
        result = run(acc.ingest_tick(ev))  # event 2 -> bypass can't fire
        # len==2 -> bypass does not fire; but min_sweeps=2 is met -> should pass
        assert result is not None
        assert result.trade_count == 2

    # ── Window pruning ────────────────────────────────────────────────

    def test_window_pruning_removes_old_events(self):
        """Events older than the window are pruned before gate evaluation."""
        acc = RepetitionAccumulator(
            window_minutes=10,
            min_trades=3,
            min_premium=50_000,
            dte_premium_tiers={},
        )
        old_ts = _ts(-700)  # 700s ago, outside 10-min window
        new_ts = _ts(0)

        old_ev = make_event(premium=100_000.0, dte=15,
                            underlying_price=150.0, timestamp=old_ts)
        new_ev = make_event(premium=100_000.0, dte=15,
                            underlying_price=150.0, timestamp=new_ts)

        run(acc.ingest_tick(old_ev))
        run(acc.ingest_tick(old_ev))
        # Two old events; now add one new event — old ones pruned
        result = run(acc.ingest_tick(new_ev))
        # Only 1 recent event -> min_trades=3 not met
        assert result is None

    # ── Dict event compat ────────────────────────────────────────────

    def test_dict_event_compat(self):
        """Dict events are wrapped and handled correctly."""
        acc = RepetitionAccumulator(
            min_trades=1,
            min_premium=50_000,
            dte_premium_tiers={},
        )
        ev_dict = {
            "ticker": "AAPL",
            "contract_type": "CALL",
            "strike": 150.0,
            "expiry": "2026-06-20",
            "premium": 100_000.0,
            "trade_type": "SWEEP",
            "dte": 15,
            "underlying_price": 150.0,
            "order_side": "BUY",
            "timestamp": _ts(),
        }
        result = run(acc.ingest_tick(ev_dict))
        assert result is not None


# ---------------------------------------------------------------------------
# get_signal cooldown tests
# ---------------------------------------------------------------------------

class TestGetSignal:

    def _ep(self):
        return RepetitionEpisode(ticker="AAPL", contract_type="CALL")

    def test_none_ep_returns_none(self):
        acc = make_accumulator(signal_cooldown=5)
        result = run(acc.get_signal(_ts(), None))
        assert result is None

    def test_zero_cooldown_always_returns_ep(self):
        acc = make_accumulator(signal_cooldown=0)
        ep = self._ep()
        result = run(acc.get_signal(_ts(), ep))
        assert result is ep

    def test_first_signal_sets_last_signal_at(self):
        acc = make_accumulator(signal_cooldown=5)
        ep = self._ep()
        ts = _ts()
        run(acc.get_signal(ts, ep))
        assert ep.last_signal_at == ts

    def test_within_cooldown_suppressed(self):
        acc = make_accumulator(signal_cooldown=5)  # 5 minutes
        ep = self._ep()
        ts1 = _ts(0)
        run(acc.get_signal(ts1, ep))
        ts2 = _ts(60)  # 60 seconds later, within 5-min cooldown
        result = run(acc.get_signal(ts2, ep))
        assert result is None

    def test_after_cooldown_passes(self):
        acc = make_accumulator(signal_cooldown=5)  # 5 minutes
        ep = self._ep()
        ts1 = _ts(0)
        run(acc.get_signal(ts1, ep))
        ts2 = _ts(310)  # 310 seconds = just over 5 minutes
        result = run(acc.get_signal(ts2, ep))
        assert result is ep


# ---------------------------------------------------------------------------
# ingest shim backward-compat
# ---------------------------------------------------------------------------

class TestIngestShim:

    def test_ingest_gate1_fail_returns_none(self):
        acc = make_accumulator(min_trades=5, dte_premium_tiers={}, min_premium=50_000)
        ev = make_event(premium=100_000.0)
        result = run(acc.ingest(ev))
        assert result is None

    def test_ingest_passes_both_gates(self):
        acc = RepetitionAccumulator(
            min_trades=1,
            min_premium=50_000,
            signal_cooldown=0,
            dte_premium_tiers={},
        )
        ev = make_event(premium=100_000.0)
        result = run(acc.ingest(ev))
        assert result is not None

    def test_ingest_ts_int_conversion(self):
        """Timestamp as unix int is handled in ingest shim."""
        import time
        acc = RepetitionAccumulator(
            min_trades=1,
            min_premium=50_000,
            signal_cooldown=0,
            dte_premium_tiers={},
        )
        ev = make_event(premium=100_000.0)
        ev.timestamp = int(time.time())
        result = run(acc.ingest(ev))
        assert result is not None


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

    def test_watch_below_100k(self):
        """QA-21: premium < $100K -> WATCH."""
        acc = make_accumulator()
        ep = self._ep_with_premium(50_000.0)
        assert acc.get_alert_level(ep) == "WATCH"

    def test_alert_at_100k(self):
        """QA-22: premium == $100K -> ALERT (boundary)."""
        acc = make_accumulator()
        ep = self._ep_with_premium(100_000.0)
        assert acc.get_alert_level(ep) == "ALERT"

    def test_alert_between_100k_and_500k(self):
        acc = make_accumulator()
        ep = self._ep_with_premium(250_000.0)
        assert acc.get_alert_level(ep) == "ALERT"

    def test_strong_signal_at_500k(self):
        """QA-23: premium == $500K -> STRONG_SIGNAL (non-accelerating)."""
        acc = make_accumulator()
        ep = self._ep_with_premium(500_000.0)
        assert acc.get_alert_level(ep) == "STRONG_SIGNAL"

    def test_strong_signal_between_500k_and_2m(self):
        acc = make_accumulator()
        ep = self._ep_with_premium(1_000_000.0)
        assert acc.get_alert_level(ep) == "STRONG_SIGNAL"

    def test_conviction_at_2m(self):
        """QA-24: premium >= $2M -> CONVICTION."""
        acc = make_accumulator()
        ep = self._ep_with_premium(2_000_000.0)
        assert acc.get_alert_level(ep) == "CONVICTION"

    def test_conviction_above_2m(self):
        acc = make_accumulator()
        ep = self._ep_with_premium(5_000_000.0)
        assert acc.get_alert_level(ep) == "CONVICTION"

    def test_conviction_accelerating_above_500k(self):
        """
        is_accelerating=True AND premium >= $500K -> CONVICTION
        (even below $2M).
        """
        acc = make_accumulator()
        ep = self._ep_with_premium(600_000.0, accelerating=True)
        assert ep.is_accelerating is True
        assert acc.get_alert_level(ep) == "CONVICTION"

    def test_conviction_accelerating_at_exactly_500k(self):
        """
        Finding 3 (panel deliberation May 1 2026):
        is_accelerating=True AND premium == exactly $500K -> CONVICTION.

        At premium=$500K:
          - non-accelerating -> STRONG_SIGNAL  (prem >= 500K, not accelerating)
          - accelerating     -> CONVICTION     (is_accelerating AND prem >= 500K)

        These are different outcomes at the same premium. This test pins the
        boundary so any accidental reordering of the first two gate checks
        is caught immediately.
        """
        acc = make_accumulator()
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        # 3 events each at $500K/3 = $166,666.67, spaced 10s apart -> is_accelerating=True
        # total_premium = exactly $500K
        each = 500_000.0 / 3
        for i in range(3):
            e = MagicMock()
            e.premium   = each
            e.timestamp = _ts(i * 10)  # 0s, 10s, 20s -> span=20s <= 60s
            ep.events.append(e)
        assert ep.is_accelerating is True
        assert abs(ep.total_premium - 500_000.0) < 0.01
        # Must be CONVICTION, not STRONG_SIGNAL
        assert acc.get_alert_level(ep) == "CONVICTION"

    def test_conviction_accelerating_threshold_is_500k(self):
        """
        is_accelerating=True but premium < $500K -> NOT CONVICTION.
        Must fall through to STRONG_SIGNAL check.
        """
        acc = make_accumulator()
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        for i in range(3):
            e = MagicMock()
            e.premium   = 100_000.0  # total = 300K
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
        assert acc._tier_map == {}
        acc.set_tier_map({"AAPL": 1, "TSLA": 2})
        assert acc._tier_map == {"AAPL": 1, "TSLA": 2}

    def test_tier_map_used_in_premium_floor_after_set(self):
        """After set_tier_map, floor lookup uses the new map."""
        acc = make_accumulator(tier_map={})
        acc.set_tier_map({"AAPL": 1})
        ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
        e = MagicMock()
        e.dte = 20
        ep.events = [e]
        # T1, 20 DTE -> floor = $500K
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
        ev = make_event(premium=100_000.0, timestamp=_ts())
        run(acc.ingest_tick(ev))
        removed = run(acc.cleanup_expired())
        assert removed == 0
        assert len(acc._episodes) == 1


# ---------------------------------------------------------------------------
# _DEFAULT_DTE_PREMIUM_TIERS constant sanity check
# ---------------------------------------------------------------------------

class TestDefaultDtePremiumTiers:

    def test_all_expected_keys_present(self):
        assert set(_DEFAULT_DTE_PREMIUM_TIERS.keys()) == {7, 30, 90, 9999}

    def test_t1_t2_t3_floors_are_tuples_of_two(self):
        for key, val in _DEFAULT_DTE_PREMIUM_TIERS.items():
            assert isinstance(val, tuple)
            assert len(val) == 2

    def test_t1_always_higher_than_t2_t3(self):
        """T1 floor should always be >= T2/T3 floor in every bucket."""
        for key, (t1, t2t3) in _DEFAULT_DTE_PREMIUM_TIERS.items():
            assert t1 >= t2t3, f"T1 floor {t1} < T2/T3 floor {t2t3} in DTE bucket {key}"

    def test_floors_increase_with_dte(self):
        """Both T1 and T2/T3 floors should increase with DTE bucket."""
        sorted_keys = sorted(_DEFAULT_DTE_PREMIUM_TIERS)
        prev_t1 = prev_t2t3 = 0
        for k in sorted_keys:
            t1, t2t3 = _DEFAULT_DTE_PREMIUM_TIERS[k]
            assert t1 >= prev_t1
            assert t2t3 >= prev_t2t3
            prev_t1 = t1
            prev_t2t3 = t2t3
