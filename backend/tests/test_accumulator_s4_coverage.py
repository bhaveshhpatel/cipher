"""
test_accumulator_s4_coverage.py

Targets the S4-specific uncovered branches in
signals/repetition_accumulator.py (was at 75%).

Missing line groups addressed:
  126-127  cleanup_expired removes a stale episode
  144-155  set_tier_map / T2 tier floor path (_get_episode_min_premium col=1)
  248,268  _classify_otm STANDARD_OTM and DEEP_OTM branches
  310-335  _is_single_whale_sweep: all four guard branches
  367-369  dominant_direction REPEAT_SELL branch
  390-394  dominant_direction MIXED (sell_prem > buy_prem)
  440      DTE overflow debug-log fallback in _get_episode_min_premium
  473-474  _get_episode_min_premium T2 col path returned
  479-481  _get_episode_min_premium overflow return for T2
  546      min_sweeps gate suppression (sweep_count < min_sweeps -> None)
  592-599  ingest_tick Gate-4 whale-sweep bypass fires episode
"""
import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from signals.repetition_accumulator import (
    RepetitionAccumulator,
    RepetitionEpisode,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _ts(offset_seconds=0):
    return datetime(2026, 4, 26, 14, 30, tzinfo=timezone.utc) + timedelta(seconds=offset_seconds)


def _ev(
    ticker="AAPL", contract_type="CALL", strike=200.0, expiry="2026-05-16",
    premium=100_000, trade_type="SWEEP", dte=30,
    underlying_price=200.0, order_side="BUY", ts_offset=0,
):
    return SimpleNamespace(
        ticker=ticker, contract_type=contract_type, strike=strike, expiry=expiry,
        premium=premium, trade_type=trade_type, dte=dte,
        underlying_price=underlying_price, order_side=order_side,
        timestamp=_ts(ts_offset), occ_symbol=None, direction=None, sentiment=None,
    )


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── cleanup_expired (lines 126-127) ───────────────────────────────────────────

def test_cleanup_expired_removes_stale_episode():
    acc = RepetitionAccumulator(window_minutes=1, min_trades=1, min_premium=1)
    stale_ts = datetime.now(timezone.utc) - timedelta(minutes=10)
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
    ep.last_seen = stale_ts
    acc._episodes["AAPL|CALL|0.00|"] = ep
    removed = run(acc.cleanup_expired())
    assert removed == 1
    assert "AAPL|CALL|0.00|" not in acc._episodes


def test_cleanup_expired_keeps_fresh_episode():
    acc = RepetitionAccumulator(window_minutes=60, min_trades=1, min_premium=1)
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
    ep.last_seen = datetime.now(timezone.utc)
    acc._episodes["AAPL|CALL|0.00|"] = ep
    removed = run(acc.cleanup_expired())
    assert removed == 0


# ── set_tier_map + T2 floor path (lines 144-155, 473-474) ─────────────────────

def test_set_tier_map_updates_internal_map():
    acc = RepetitionAccumulator(
        min_trades=1, min_premium=1,
        dte_premium_tiers={30: (500_000, 100_000), 9999: (2_000_000, 1_000_000)},
    )
    acc.set_tier_map({"AAPL": 2})
    assert acc._tier_map["AAPL"] == 2


def test_t2_ticker_uses_lower_floor():
    """
    T2 col=1 floor for DTE<=30 is 100_000.
    3 events x 40_000 = 120_000 >= 100_000 -> should pass.
    Same tickers at T1 (col=0 floor 500_000) would fail.
    """
    acc = RepetitionAccumulator(
        window_minutes=60, min_trades=3, min_premium=1,
        dte_premium_tiers={30: (500_000, 100_000), 9999: (2_000_000, 1_000_000)},
    )
    acc.set_tier_map({"AAPL": 2})
    for i in range(3):
        ep = run(acc.ingest_tick(_ev(premium=40_000, dte=20, ts_offset=i)))
    assert ep is not None, "T2 ticker should pass the lower DTE floor"
    assert ep.total_premium == 120_000


def test_t1_ticker_blocked_by_higher_floor():
    """Same 3x40k premium at T1 col=0 floor=500_000 -> blocked."""
    acc = RepetitionAccumulator(
        window_minutes=60, min_trades=3, min_premium=1,
        dte_premium_tiers={30: (500_000, 100_000), 9999: (2_000_000, 1_000_000)},
    )
    # No set_tier_map -> defaults to T1
    ep = None
    for i in range(3):
        ep = run(acc.ingest_tick(_ev(premium=40_000, dte=20, ts_offset=i)))
    assert ep is None, "T1 ticker should be blocked by the higher DTE floor"


# ── _classify_otm branches (lines 248, 268) ───────────────────────────────────

def test_classify_otm_atm():
    result = RepetitionAccumulator._classify_otm(200.0, 200.0)
    assert result == "ATM"


def test_classify_otm_standard_otm():
    # strike=220, underlying=200 -> pct=0.10 -> STANDARD_OTM
    result = RepetitionAccumulator._classify_otm(220.0, 200.0)
    assert result == "STANDARD_OTM"


def test_classify_otm_deep_otm():
    # strike=240, underlying=200 -> pct=0.20 -> DEEP_OTM
    result = RepetitionAccumulator._classify_otm(240.0, 200.0)
    assert result == "DEEP_OTM"


def test_classify_otm_unknown_when_zero_price():
    result = RepetitionAccumulator._classify_otm(200.0, 0.0)
    assert result == "UNKNOWN"


def test_deep_otm_multiplier_blocks_low_premium():
    """
    DEEP_OTM: floor * 1.5 = 150_000. 3 x 40_000 = 120_000 < 150_000 -> None.
    Strike 240 on underlying 200 -> 20% OTM -> DEEP_OTM.
    """
    acc = RepetitionAccumulator(
        window_minutes=60, min_trades=3, min_premium=100_000,
        deep_otm_multiplier=1.5,
    )
    ep = None
    for i in range(3):
        ep = run(acc.ingest_tick(_ev(
            strike=240.0, underlying_price=200.0,
            premium=40_000, ts_offset=i,
        )))
    assert ep is None


def test_deep_otm_multiplier_passes_high_premium():
    """
    DEEP_OTM: floor * 1.5 = 150_000. 3 x 60_000 = 180_000 >= 150_000 -> passes.
    """
    acc = RepetitionAccumulator(
        window_minutes=60, min_trades=3, min_premium=100_000,
        deep_otm_multiplier=1.5,
    )
    ep = None
    for i in range(3):
        ep = run(acc.ingest_tick(_ev(
            strike=240.0, underlying_price=200.0,
            premium=60_000, ts_offset=i,
        )))
    assert ep is not None


# ── _is_single_whale_sweep (lines 310-335) ────────────────────────────────────

def test_whale_sweep_disabled_when_bypass_zero():
    acc = RepetitionAccumulator(sweep_bypass_premium=0)
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
    ep.events = [SimpleNamespace(premium=1_000_000, trade_type="SWEEP", dte=30,
                                 underlying_price=200.0, order_side="BUY",
                                 timestamp=_ts())]
    assert acc._is_single_whale_sweep(ep) is False


def test_whale_sweep_false_when_multiple_events():
    acc = RepetitionAccumulator(sweep_bypass_premium=500_000)
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
    ev = SimpleNamespace(premium=600_000, trade_type="SWEEP", dte=30,
                         underlying_price=200.0, order_side="BUY", timestamp=_ts())
    ep.events = [ev, ev]  # len == 2, not 1
    assert acc._is_single_whale_sweep(ep) is False


def test_whale_sweep_false_when_not_sweep_type():
    acc = RepetitionAccumulator(sweep_bypass_premium=500_000)
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
    ep.events = [SimpleNamespace(premium=1_000_000, trade_type="BLOCK", dte=30,
                                 underlying_price=200.0, order_side="BUY",
                                 timestamp=_ts())]
    assert acc._is_single_whale_sweep(ep) is False


def test_whale_sweep_false_when_premium_below_bypass():
    acc = RepetitionAccumulator(sweep_bypass_premium=500_000)
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
    ep.events = [SimpleNamespace(premium=400_000, trade_type="SWEEP", dte=30,
                                 underlying_price=200.0, order_side="BUY",
                                 timestamp=_ts())]
    assert acc._is_single_whale_sweep(ep) is False


def test_whale_sweep_true_when_all_conditions_met():
    acc = RepetitionAccumulator(sweep_bypass_premium=500_000)
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
    ep.events = [SimpleNamespace(premium=600_000, trade_type="SWEEP", dte=30,
                                 underlying_price=200.0, order_side="BUY",
                                 timestamp=_ts())]
    assert acc._is_single_whale_sweep(ep) is True


# ── dominant_direction REPEAT_SELL + mixed (lines 367-369, 390-394) ───────────

def test_dominant_direction_repeat_sell_buy_put():
    ep = RepetitionEpisode(ticker="SPY", contract_type="PUT")
    ep.events = [
        SimpleNamespace(premium=300_000, order_side="BUY", contract_type="PUT",
                        trade_type="SWEEP", dte=30, underlying_price=500.0,
                        timestamp=_ts()),
    ]
    assert ep.dominant_direction == "REPEAT_SELL"


def test_dominant_direction_repeat_sell_sell_call():
    ep = RepetitionEpisode(ticker="SPY", contract_type="CALL")
    ep.events = [
        SimpleNamespace(premium=300_000, order_side="SELL", contract_type="CALL",
                        trade_type="SWEEP", dte=30, underlying_price=500.0,
                        timestamp=_ts()),
    ]
    assert ep.dominant_direction == "REPEAT_SELL"


def test_dominant_direction_mixed_sell_wins():
    """sell_prem > buy_prem -> REPEAT_SELL."""
    ep = RepetitionEpisode(ticker="TSLA", contract_type="CALL")
    ep.events = [
        SimpleNamespace(premium=400_000, order_side="BUY", contract_type="CALL",
                        trade_type="SWEEP", dte=30, underlying_price=300.0,
                        timestamp=_ts()),
        SimpleNamespace(premium=600_000, order_side="SELL", contract_type="CALL",
                        trade_type="SWEEP", dte=30, underlying_price=300.0,
                        timestamp=_ts(1)),
    ]
    assert ep.dominant_direction == "REPEAT_SELL"


# ── DTE overflow / custom tiers fallback (lines 440, 473-474, 479-481) ────────

def test_get_episode_min_premium_dte_overflow_t1():
    """
    Custom tiers with max key=30. DTE=60 exceeds all keys -> debug log path
    fires and returns tiers[30][col=0].
    """
    custom_tiers = {30: (500_000, 100_000)}
    acc = RepetitionAccumulator(
        min_trades=1, min_premium=1,
        dte_premium_tiers=custom_tiers,
    )
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
    ev = SimpleNamespace(premium=100_000, dte=60, underlying_price=200.0,
                         order_side="BUY", trade_type="SWEEP", timestamp=_ts())
    ep.events = [ev]
    floor = acc._get_episode_min_premium(ep)
    assert floor == 500_000  # col=0 (T1), key=30 (only key, overflow fallback)


def test_get_episode_min_premium_dte_overflow_t2():
    """
    Custom tiers with max key=30. DTE=60 exceeds all keys ->
    overflow return tiers[30][col=1] for T2 ticker.
    """
    custom_tiers = {30: (500_000, 100_000)}
    acc = RepetitionAccumulator(
        min_trades=1, min_premium=1,
        dte_premium_tiers=custom_tiers,
    )
    acc.set_tier_map({"AAPL": 2})
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
    ev = SimpleNamespace(premium=100_000, dte=60, underlying_price=200.0,
                         order_side="BUY", trade_type="SWEEP", timestamp=_ts())
    ep.events = [ev]
    floor = acc._get_episode_min_premium(ep)
    assert floor == 100_000  # col=1 (T2), overflow fallback


# ── min_sweeps gate suppression (line 546) ────────────────────────────────────

def test_min_sweeps_gate_suppresses_non_sweep():
    """
    min_sweeps=2 with only BLOCK events -> sweep_count=0 < 2 -> None.
    """
    acc = RepetitionAccumulator(
        window_minutes=60, min_trades=3, min_premium=50_000,
        min_sweeps=2,
    )
    ep = None
    for i in range(3):
        ep = run(acc.ingest_tick(_ev(
            premium=30_000, trade_type="BLOCK", ts_offset=i,
        )))
    assert ep is None


def test_min_sweeps_gate_passes_when_enough_sweeps():
    acc = RepetitionAccumulator(
        window_minutes=60, min_trades=3, min_premium=50_000,
        min_sweeps=2,
    )
    ep = None
    for i in range(3):
        ep = run(acc.ingest_tick(_ev(
            premium=30_000, trade_type="SWEEP", ts_offset=i,
        )))
    assert ep is not None


# ── whale sweep bypass fires episode (lines 592-599) ─────────────────────────

def test_whale_sweep_bypass_fires_on_single_large_sweep():
    """
    min_trades=3, min_sweeps=2, sweep_bypass_premium=500_000.
    Single SWEEP event with premium=600_000 -> Gate-1 (min_trades=3) still
    blocks since only 1 event in the episode. Bypass only skips min_sweeps.
    """
    acc = RepetitionAccumulator(
        window_minutes=60, min_trades=3, min_premium=50_000,
        min_sweeps=2, sweep_bypass_premium=500_000,
    )
    ep = run(acc.ingest_tick(_ev(
        premium=600_000, trade_type="SWEEP", underlying_price=200.0,
    )))
    assert ep is None  # Gate-1 (min_trades) still blocks single event


def test_whale_sweep_bypass_skips_sweep_count_check():
    """
    min_trades=1, min_sweeps=2, sweep_bypass_premium=500_000.
    Single SWEEP at 600_000 >= bypass -> _is_single_whale_sweep=True -> skips
    sweep count check -> episode fires.
    """
    acc = RepetitionAccumulator(
        window_minutes=60, min_trades=1, min_premium=50_000,
        min_sweeps=2, sweep_bypass_premium=500_000,
    )
    ep = run(acc.ingest_tick(_ev(
        premium=600_000, trade_type="SWEEP", underlying_price=200.0,
    )))
    assert ep is not None, "Whale sweep bypass should allow single qualifying SWEEP"
