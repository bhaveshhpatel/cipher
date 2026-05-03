"""
QA boundary tests for ING-002 — $10,000 minimum premium floor gate.

Spec (STORY-STEPS_ING.md § ING-002 AC):
  AC-1  fill * size * 100 < 10_000  →  parse_tradier_trade returns "below_premium"
  AC-2  fill * size * 100 == 10_000 →  at-floor trade passes (returns OptionsFlowEvent)
  AC-3  fill * size * 100 > 10_000  →  above-floor trade passes (returns OptionsFlowEvent)
  AC-4  size == 0 gate still fires BEFORE floor gate (returns None, not "below_premium")
  AC-5  _stats["below_min_premium"] is incremented on each filtered trade
  AC-6  get_stats() exposes below_min_premium key (visible in /health/stream)

Test IDs: P-01 … P-06
"""
import importlib
from datetime import date, timedelta
from unittest.mock import patch

import pytest

import parsers.options_flow_parser as _parser_module
from parsers.options_flow_parser import OptionsFlowEvent, parse_tradier_trade


# ── helpers ─────────────────────────────────────────────────────────────────

def _payload(
    symbol="AAPL  260117C00180000",
    last=1.00,
    bid=0.90,
    ask=1.10,
    size=10,
    exch="C",
    timestamp=1700000000000,
):
    """Minimal Tradier timesale payload, kept small so we control premium."""
    return {
        "symbol":    symbol,
        "last":      last,
        "bid":       bid,
        "ask":       ask,
        "size":      size,
        "exch":      exch,
        "timestamp": timestamp,
    }


def _reset_stats():
    """Reset module-level _stats so counter tests start from 0."""
    _parser_module._stats["below_min_premium"] = 0


# ── P-01  Below floor → "below_premium" ─────────────────────────────────────

def test_P01_below_floor_returns_below_premium_sentinel():
    """
    last=1.00, size=5  →  premium = 1.00 * 5 * 100 = $500  (<$10,000)
    Expected return value: the string literal "below_premium"
    """
    raw = _payload(last=1.00, size=5)
    result = parse_tradier_trade(raw)
    assert result == "below_premium", (
        f"Expected 'below_premium' for $500 premium, got {result!r}"
    )


# ── P-02  At exact floor → passes ───────────────────────────────────────────

def test_P02_at_floor_exact_passes():
    """
    last=1.00, size=100  →  premium = 1.00 * 100 * 100 = $10,000  (== floor)
    At-floor trades must NOT be filtered.
    """
    raw = _payload(last=1.00, size=100)
    result = parse_tradier_trade(raw)
    assert isinstance(result, OptionsFlowEvent), (
        f"At-floor $10,000 trade should pass gate, got {result!r}"
    )
    assert result.premium == pytest.approx(10_000.0)


# ── P-03  Above floor → passes ───────────────────────────────────────────────

def test_P03_above_floor_passes():
    """
    last=2.50, size=100  →  premium = 2.50 * 100 * 100 = $25,000  (>$10,000)
    """
    raw = _payload(last=2.50, size=100)
    result = parse_tradier_trade(raw)
    assert isinstance(result, OptionsFlowEvent), (
        f"Above-floor $25,000 trade should pass gate, got {result!r}"
    )
    assert result.premium == pytest.approx(25_000.0)


# ── P-04  size=0 gate fires before floor gate → None (not "below_premium") ──

def test_P04_size_zero_still_returns_none_not_below_premium():
    """
    SA-Q3 ordering: size==0 guard must be checked first.
    A zero-size payload should return None, never "below_premium".
    """
    raw = _payload(last=999.0, size=0)
    result = parse_tradier_trade(raw)
    assert result is None, (
        f"size=0 must return None (not 'below_premium'), got {result!r}"
    )


# ── P-05  Counter increments on each filtered trade ─────────────────────────

def test_P05_below_premium_counter_increments():
    """
    Each call that hits the floor gate must increment _stats["below_min_premium"].
    We reset the counter, fire three below-floor trades, then assert count==3.
    """
    _reset_stats()

    for _ in range(3):
        result = parse_tradier_trade(_payload(last=0.50, size=5))  # $250 each
        assert result == "below_premium"

    assert _parser_module._stats["below_min_premium"] == 3, (
        f"Expected counter=3, got {_parser_module._stats['below_min_premium']}"
    )


# ── P-06  get_stats() exposes key ────────────────────────────────────────────

def test_P06_get_stats_exposes_below_min_premium_key():
    """
    get_stats() must return a dict that contains the 'below_min_premium' key
    so the /health/stream endpoint can surface it without modification.
    """
    from parsers.options_flow_parser import get_stats
    stats = get_stats()
    assert "below_min_premium" in stats, (
        f"get_stats() missing 'below_min_premium' key. Keys present: {list(stats.keys())}"
    )
    assert isinstance(stats["below_min_premium"], int)
