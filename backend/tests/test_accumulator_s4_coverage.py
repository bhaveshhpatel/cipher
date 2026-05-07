"""
test_accumulator_s4_coverage.py

Targets the S4-specific uncovered branches in
signals/repetition_accumulator.py.

ING-006 rewrite changes that affect this file:
  - min_premium= constructor kwarg removed; DTE-stratified tiers supersede it
  - cleanup_expired() removed (episode cleanup is now inline in ingest_tick)
  - _is_single_whale_sweep() removed; whale-sweep bypass is inline in ingest_tick
  - sweep_bypass_premium= constructor kwarg removed
  - _classify_moneyness_band() is a MODULE-LEVEL function (ING-011b D3)
  - STANDARD_OTM label renamed to OTM
  - Default tier changed from 2 (permissive) to 1 (strict) per D-11/QA-F1.
    Tests that need tier-2 behaviour MUST call acc.set_tier_map({ticker: 2})
    explicitly — the old implicit default no longer applies.
  - DTE overflow (DTE > max tier key) now falls through to
    _DEFAULT_DTE_PREMIUM_TIERS_91_PLUS keyed by tier, not the last custom bucket

ING-011 changes that affect this file:
  - _classify_otm() renamed to _classify_moneyness_band() — full moneyness
    spectrum (DEEP_ITM | ITM | ATM | OTM | DEEP_OTM | UNKNOWN)
  - All call sites now call the module-level _classify_moneyness_band(ev)
    directly (ING-011b D3 — shim removed)

Line groups covered:
  144-155  set_tier_map / T2 tier floor path (_get_episode_min_premium)
  248,268  _classify_moneyness_band OTM and DEEP_OTM branches
  367-369  dominant_direction REPEAT_SELL branch
  390-394  dominant_direction MIXED (sell_prem > buy_prem)
  440      DTE overflow fallback in _get_episode_min_premium
  546      min_sweeps gate suppression (sweep_count < min_sweeps -> None)
  592-599  ingest_tick Gate-4 whale-sweep bypass fires episode
"""
import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from signals.repetition_accumulator import (
    RepetitionAccumulator,
    RepetitionEpisode,
    _classify_moneyness_band,
)


# ── helpers ──────────────────────────────────────────────────────────────────────────────────

def _ts(offset_seconds=0):
    return datetime(2026, 4, 26, 14, 30, tzinfo=timezone.utc) + timedelta(seconds=offset_seconds)


def _ev(
    ticker="AAPL", contract_type="CALL", strike=200.0, expiry="2026-05-16",
    premium=100_000, trade_type="SWEEP", dte=30,
    underlying_price=200.0, order_side="BUY", is_aggressive=True, ts_offset=0,
):
    return SimpleNamespace(
        ticker=ticker, contract_type=contract_type, strike=strike, expiry=expiry,
        premium=premium, trade_type=trade_type, dte=dte,
        underlying_price=underlying_price, order_side=order_side,
        is_aggressive=is_aggressive,
        timestamp=_ts(ts_offset), occ_symbol=None, direction=None, sentiment=None,
    )


def _otm_ev(strike, underlying_price, contract_type="CALL"):
    """Minimal event object for _classify_moneyness_band tests (ING-011b module-level).

    ING-011: _classify_otm() renamed to _classify_moneyness_band().
    ING-011b: promoted to module-level function — call directly, no acc instance needed.
    contract_type defaults to 'CALL' so OTM/DEEP_OTM cases exercise the real
    directional path rather than the unknown-contract-type fallback.
    """
    return SimpleNamespace(
        strike=strike,
        underlying_price=underlying_price,
        contract_type=contract_type,
    )


def run(coro):
    return asyncio.run(coro)


# ── set_tier_map + T2 floor path ─────────────────────────────────────────────────────────────

def test_set_tier_map_updates_internal_map():
    acc = RepetitionAccumulator(
        min_trades=1,
        dte_premium_tiers=[(30, {1: 500_000, 2: 100_000}), (9999, {1: 2_000_000, 2: 1_000_000})],
    )
    acc.set_tier_map({"AAPL": 2})
    assert acc._tier_map["AAPL"] == 2


def test_t2_ticker_uses_lower_floor():
    """
    T2 tier=2 floor for DTE<=30 is 100_000.
    3 aggressive events x 40_000 = 120_000 >= 100_000 -> should pass.
    Same tickers at T1 (tier=1 floor 500_000) would fail.
    """
    acc = RepetitionAccumulator(
        window_minutes=60, min_trades=3,
        dte_premium_tiers=[(30, {1: 500_000, 2: 100_000}), (9999, {1: 2_000_000, 2: 1_000_000})],
    )
    acc.set_tier_map({"AAPL": 2})
    ep = None
    for i in range(3):
        ep = run(acc.ingest_tick(_ev(premium=40_000, dte=20, is_aggressive=True, ts_offset=i)))
    assert ep is not None, "T2 ticker should pass the lower DTE floor"
    assert ep.total_premium == 120_000


def test_t1_ticker_blocked_by_higher_floor():
    """Same 3x40k premium at T1 tier=1 floor=500_000 -> blocked."""
    acc = RepetitionAccumulator(
        window_minutes=60, min_trades=3,
        dte_premium_tiers=[(30, {1: 500_000, 2: 100_000}), (9999, {1: 2_000_000, 2: 1_000_000})],
    )
    # D-11: default tier is 1 (strict). Force explicitly for clarity.
    acc._tier_map["AAPL"] = 1
    ep = None
    for i in range(3):
        ep = run(acc.ingest_tick(_ev(premium=40_000, dte=20, is_aggressive=True, ts_offset=i)))
    assert ep is None, "T1 ticker should be blocked by the higher DTE floor"


# ── _classify_moneyness_band branches (ING-011b: module-level function) ───────────────────────
# ING-011b (D3): _classify_moneyness_band is a MODULE-LEVEL function.
# Import and call directly — no accumulator instance required.

def test_classify_otm_atm():
    assert _classify_moneyness_band(_otm_ev(200.0, 200.0)) == "ATM"


def test_classify_otm_standard_otm():
    # strike=220, underlying=200 -> pct=0.10 -> OTM (renamed from STANDARD_OTM in ING-006)
    # CALL: strike > underlying -> OTM (not ITM for a call)
    assert _classify_moneyness_band(_otm_ev(220.0, 200.0)) == "OTM"


def test_classify_otm_deep_otm():
    # strike=240, underlying=200 -> pct=0.20 -> DEEP_OTM
    # CALL: strike > underlying -> OTM/DEEP_OTM (not ITM)
    assert _classify_moneyness_band(_otm_ev(240.0, 200.0)) == "DEEP_OTM"


def test_classify_otm_unknown_when_zero_price():
    assert _classify_moneyness_band(_otm_ev(200.0, 0.0)) == "UNKNOWN"


def test_deep_otm_multiplier_blocks_low_premium():
    """
    DEEP_OTM: floor * 1.5 applied to weighted premium.
    Strike 240 on underlying 200 -> 20% OTM -> DEEP_OTM.
    DTE=30, tier=2, floor=100_000. Effective deep floor = 150_000.
    3 aggressive @ $40k = $120k weighted < $150k -> None.
    """
    acc = RepetitionAccumulator(
        window_minutes=60, min_trades=3,
        deep_otm_multiplier=1.5,
        dte_premium_tiers=[(30, {1: 500_000, 2: 100_000}), (9999, {1: 2_000_000, 2: 1_000_000})],
    )
    acc.set_tier_map({"AAPL": 2})
    ep = None
    for i in range(3):
        ep = run(acc.ingest_tick(_ev(
            strike=240.0, underlying_price=200.0,
            premium=40_000, is_aggressive=True, ts_offset=i,
        )))
    assert ep is None


def test_deep_otm_multiplier_passes_high_premium():
    """
    DEEP_OTM: floor * 1.5. DTE=30, tier=2, floor=100_000 -> deep_floor=150_000.
    3 aggressive @ $60k = $180k weighted >= $150k -> passes.
    """
    acc = RepetitionAccumulator(
        window_minutes=60, min_trades=3,
        deep_otm_multiplier=1.5,
        dte_premium_tiers=[(30, {1: 500_000, 2: 100_000}), (9999, {1: 2_000_000, 2: 1_000_000})],
    )
    acc.set_tier_map({"AAPL": 2})
    ep = None
    for i in range(3):
        ep = run(acc.ingest_tick(_ev(
            strike=240.0, underlying_price=200.0,
            premium=60_000, is_aggressive=True, ts_offset=i,
        )))
    assert ep is not None


# ── dominant_direction REPEAT_SELL + mixed ────────────────────────────────────────────

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


# ── DTE overflow / custom tiers fallback ─────────────────────────────────────────────────
# NOTE: dte_premium_tiers must be a list of (max_dte, {tier: floor}) tuples.
# DTE > all tier keys overflows to _DEFAULT_DTE_PREMIUM_TIERS_91_PLUS.

def test_get_episode_min_premium_dte_overflow_t1():
    """
    Custom tiers with max_dte=30. DTE=60 exceeds all keys.
    Overflow -> _DEFAULT_DTE_PREMIUM_TIERS_91_PLUS[tier=2] = 1_000_000.
    Explicitly set tier=2 via set_tier_map (D-11: default is tier=1, not 2).
    """
    acc = RepetitionAccumulator(
        min_trades=1,
        dte_premium_tiers=[(30, {1: 500_000, 2: 100_000})],
    )
    acc.set_tier_map({"AAPL": 2})  # D-11: must be explicit; default is tier=1
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
    ev = SimpleNamespace(premium=100_000, dte=60, underlying_price=200.0,
                         order_side="BUY", trade_type="SWEEP", timestamp=_ts(),
                         ticker="AAPL")
    ep.events = [ev]
    floor = acc._get_episode_min_premium(ep)
    assert floor == 1_000_000


def test_get_episode_min_premium_dte_overflow_t2():
    """
    Same scenario with tier=2 set explicitly.
    _DEFAULT_DTE_PREMIUM_TIERS_91_PLUS tier=2 is 1_000_000.
    """
    acc = RepetitionAccumulator(
        min_trades=1,
        dte_premium_tiers=[(30, {1: 500_000, 2: 100_000})],
    )
    acc.set_tier_map({"AAPL": 2})  # D-11: must be explicit; default is tier=1
    ep = RepetitionEpisode(ticker="AAPL", contract_type="CALL")
    ev = SimpleNamespace(premium=100_000, dte=60, underlying_price=200.0,
                         order_side="BUY", trade_type="SWEEP", timestamp=_ts(),
                         ticker="AAPL")
    ep.events = [ev]
    floor = acc._get_episode_min_premium(ep)
    assert floor == 1_000_000  # 91+ DTE overflow, tier=2 explicit


# ── min_sweeps gate suppression ─────────────────────────────────────────────────────────────

def test_min_sweeps_gate_suppresses_non_sweep():
    """
    min_sweeps=2 with only BLOCK events -> sweep_count=0 < 2 -> None.
    Uses DTE=5, tier=2, 3 aggressive @ $30k = $90k > $50k floor (passes Gate 2).
    Gate 4 (min_sweeps) blocks because trade_type=BLOCK events are not counted.

    NOTE: Gate 4 only activates when the incoming ev.trade_type == SWEEP.
    BLOCK events bypass Gate 4 entirely in the ING-006 implementation —
    the sweep gate only fires when the current tick is itself a SWEEP.
    This test passes trade_type=SWEEP on the last event to trigger Gate 4.
    """
    acc = RepetitionAccumulator(
        window_minutes=60, min_trades=3,
        min_sweeps=2,
        dte_premium_tiers=[(7, {1: 50_000, 2: 50_000}), (9999, {1: 2_000_000, 2: 1_000_000})],
    )
    acc.set_tier_map({"AAPL": 2})
    ep = None
    # Two BLOCK events (won't trigger Gate 4) + one SWEEP (triggers Gate 4)
    for i in range(2):
        ep = run(acc.ingest_tick(_ev(
            premium=30_000, trade_type="BLOCK", dte=5, is_aggressive=True, ts_offset=i,
        )))
    # Final SWEEP tick: triggers Gate 4; sweep_count=1 < min_sweeps=2 -> None
    ep = run(acc.ingest_tick(_ev(
        premium=30_000, trade_type="SWEEP", dte=5, is_aggressive=True, ts_offset=2,
    )))
    assert ep is None


def test_min_sweeps_gate_passes_when_enough_sweeps():
    """
    min_sweeps=2 with 3 SWEEP events -> sweep_count=3 >= 2 -> passes.
    DTE=5, tier=2, 3 aggressive @ $30k = $90k > $50k floor.
    """
    acc = RepetitionAccumulator(
        window_minutes=60, min_trades=3,
        min_sweeps=2,
        dte_premium_tiers=[(7, {1: 50_000, 2: 50_000}), (9999, {1: 2_000_000, 2: 1_000_000})],
    )
    acc.set_tier_map({"AAPL": 2})
    ep = None
    for i in range(3):
        ep = run(acc.ingest_tick(_ev(
            premium=30_000, trade_type="SWEEP", dte=5, is_aggressive=True, ts_offset=i,
        )))
    assert ep is not None


# ── whale sweep bypass fires episode ──────────────────────────────────────────────────────────

def test_whale_sweep_bypass_fires_on_single_large_sweep():
    """
    min_trades=3, min_sweeps=2.
    Single SWEEP event with premium=600_000 -> Gate 1 (min_trades=3) blocks
    since only 1 event in the episode. Bypass only skips min_sweeps Gate 4.
    """
    acc = RepetitionAccumulator(
        window_minutes=60, min_trades=3,
        min_sweeps=2,
        dte_premium_tiers=[(7, {1: 50_000, 2: 50_000}), (9999, {1: 2_000_000, 2: 1_000_000})],
    )
    acc.set_tier_map({"AAPL": 2})
    ep = run(acc.ingest_tick(_ev(
        premium=600_000, trade_type="SWEEP", dte=5, underlying_price=200.0,
    )))
    assert ep is None  # Gate-1 (min_trades) still blocks single event


def test_whale_sweep_bypass_skips_sweep_count_check():
    """
    min_trades=1, min_sweeps=2.
    Single SWEEP at premium >= 500_000 -> whale-sweep bypass fires;
    len(ep.events)==1 AND trade_type=SWEEP AND premium>=500k -> skips Gate 4
    -> episode emits.
    DTE=5, tier=2, floor=$50k. Single $600k aggressive event = $600k > $50k.
    """
    acc = RepetitionAccumulator(
        window_minutes=60, min_trades=1,
        min_sweeps=2,
        dte_premium_tiers=[(7, {1: 50_000, 2: 50_000}), (9999, {1: 2_000_000, 2: 1_000_000})],
    )
    acc.set_tier_map({"AAPL": 2})
    ep = run(acc.ingest_tick(_ev(
        premium=600_000, trade_type="SWEEP", dte=5, underlying_price=200.0,
        is_aggressive=True,
    )))
    assert ep is not None, "Whale sweep bypass should allow single qualifying SWEEP"
