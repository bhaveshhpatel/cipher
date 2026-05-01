"""
test_apex_s0_swarm_cleanup.py

Acceptance tests for Apex S0 — Swarm Cleanup.

Coverage requirements (S0 spec):
  AC-S0-1  build_composite_async does NOT exist on composite_signal_engine module.
  AC-S0-2  run_ensemble import does NOT exist as a module-level name.
  AC-S0-3  CompositeSignal has NO swarm_* fields.
  AC-S0-4  build_composite still returns a valid CompositeSignal synchronously.
  AC-S0-5  ensemble_runner.run_ensemble raises NotImplementedError when awaited.
  AC-S0-6  Importing ensemble_runner emits DeprecationWarning.
  AC-S0-7  volume_weighted_premium_factor returns 0.5 when OI == 0.
  AC-S0-8  volume_weighted_premium_factor clamps at 1.0.
  AC-S0-9  compute_flow_score clamps at 1.0 for extreme premium.
  AC-S0-10 build_composite recommendation logic: BUY / SELL / HOLD paths.
"""
from __future__ import annotations
import asyncio
import dataclasses
import importlib
import inspect
import sys
import pytest
import warnings

import signals.composite_signal_engine as cse


# ---------------------------------------------------------------------------
# Minimal stubs
# ---------------------------------------------------------------------------

class _Event:
    def __init__(self, **kw):
        self.ticker         = kw.get("ticker", "AAPL")
        self.premium        = kw.get("premium", 500_000)
        self.open_interest  = kw.get("open_interest", 1_000)
        self.sentiment      = kw.get("sentiment", "BULLISH")
        self.dte            = kw.get("dte", 30)
        self.influence_tier = kw.get("influence_tier", "WHALE")


class _Episode:
    def __init__(self, events, total_premium=1_000_000, trade_count=5,
                 is_accelerating=False, ticker="AAPL", contract_type="CALL"):
        self.events          = events
        self.total_premium   = total_premium
        self.trade_count     = trade_count
        self.is_accelerating = is_accelerating
        self.ticker          = ticker
        self.contract_type   = contract_type


class _Accumulator:
    pass


def _make_ep(sentiment="BULLISH", premium=500_000, oi=1_000,
             total_premium=1_000_000, trade_count=5, accelerating=False):
    ev = _Event(premium=premium, open_interest=oi, sentiment=sentiment)
    return _Episode([ev], total_premium=total_premium,
                    trade_count=trade_count, is_accelerating=accelerating)


# ---------------------------------------------------------------------------
# AC-S0-1  build_composite_async must not exist
# ---------------------------------------------------------------------------

def test_build_composite_async_removed():
    assert not hasattr(cse, "build_composite_async"), (
        "build_composite_async still present — S0 cleanup incomplete"
    )


# ---------------------------------------------------------------------------
# AC-S0-2  run_ensemble must not be a module-level name
# ---------------------------------------------------------------------------

def test_run_ensemble_import_removed():
    assert not hasattr(cse, "run_ensemble"), (
        "run_ensemble still imported at module level — S0 cleanup incomplete"
    )


# ---------------------------------------------------------------------------
# AC-S0-3  CompositeSignal must have no swarm_* fields
# ---------------------------------------------------------------------------

def test_composite_signal_no_swarm_fields():
    swarm_fields = [
        f.name for f in dataclasses.fields(cse.CompositeSignal)
        if f.name.startswith("swarm_")
    ]
    assert swarm_fields == [], (
        f"CompositeSignal still has swarm fields: {swarm_fields}"
    )


# ---------------------------------------------------------------------------
# AC-S0-4  build_composite returns a valid CompositeSignal synchronously
# ---------------------------------------------------------------------------

def test_build_composite_returns_composite_signal():
    ep  = _make_ep()
    sig = cse.build_composite(ep, _Accumulator())
    assert isinstance(sig, cse.CompositeSignal)
    assert sig.ticker == "AAPL"
    assert 0.0 <= sig.composite_score <= 1.0
    assert sig.recommendation in {"BUY", "SELL", "HOLD"}


def test_build_composite_is_not_a_coroutine():
    ep     = _make_ep()
    result = cse.build_composite(ep, _Accumulator())
    assert not inspect.iscoroutine(result), (
        "build_composite returned a coroutine — async leak detected"
    )


# ---------------------------------------------------------------------------
# AC-S0-5  ensemble_runner.run_ensemble raises NotImplementedError
# ---------------------------------------------------------------------------

def test_ensemble_runner_run_ensemble_raises():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import simulation.ensemble_runner as er
    with pytest.raises(NotImplementedError):
        asyncio.run(er.run_ensemble())


# ---------------------------------------------------------------------------
# AC-S0-6  Importing ensemble_runner emits DeprecationWarning
# ---------------------------------------------------------------------------

def test_ensemble_runner_import_warns():
    sys.modules.pop("simulation.ensemble_runner", None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.import_module("simulation.ensemble_runner")
    categories = [str(w.category) for w in caught]
    assert any("DeprecationWarning" in c for c in categories), (
        "ensemble_runner import did not emit DeprecationWarning"
    )


# ---------------------------------------------------------------------------
# AC-S0-7  vwpf returns 0.5 when OI == 0
# ---------------------------------------------------------------------------

def test_vwpf_zero_oi_returns_half():
    ep = _make_ep(oi=0)
    assert cse.volume_weighted_premium_factor(ep) == 0.5


def test_vwpf_no_events_returns_half():
    ep = _Episode([])
    assert cse.volume_weighted_premium_factor(ep) == 0.5


# ---------------------------------------------------------------------------
# AC-S0-8  vwpf clamps at 1.0
# ---------------------------------------------------------------------------

def test_vwpf_clamps_at_one():
    ep = _make_ep(premium=10_000_000, oi=1)
    assert cse.volume_weighted_premium_factor(ep) == 1.0


# ---------------------------------------------------------------------------
# AC-S0-9  compute_flow_score clamps at 1.0
# ---------------------------------------------------------------------------

def test_compute_flow_score_clamps_at_one():
    ep = _make_ep(total_premium=999_000_000, trade_count=1_000, accelerating=True)
    score = cse.compute_flow_score(ep)
    assert score == 1.0


# ---------------------------------------------------------------------------
# AC-S0-10 build_composite recommendation paths
# ---------------------------------------------------------------------------

def test_build_composite_buy_path(monkeypatch):
    monkeypatch.setattr(
        "signals.backtest_validator.get_backtest_score",
        lambda *a, **kw: 1.0
    )
    ep  = _make_ep(sentiment="BULLISH", total_premium=10_000_000, trade_count=20)
    sig = cse.build_composite(ep, _Accumulator())
    assert sig.recommendation == "BUY"


def test_build_composite_sell_path(monkeypatch):
    monkeypatch.setattr(
        "signals.backtest_validator.get_backtest_score",
        lambda *a, **kw: 1.0
    )
    ep  = _make_ep(sentiment="BEARISH", total_premium=10_000_000, trade_count=20)
    sig = cse.build_composite(ep, _Accumulator())
    assert sig.recommendation == "SELL"


def test_build_composite_hold_path(monkeypatch):
    monkeypatch.setattr(
        "signals.backtest_validator.get_backtest_score",
        lambda *a, **kw: 0.0
    )
    ep  = _make_ep(sentiment="BULLISH", total_premium=0, trade_count=0)
    sig = cse.build_composite(ep, _Accumulator())
    assert sig.recommendation == "HOLD"
