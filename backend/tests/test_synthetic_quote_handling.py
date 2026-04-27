"""
Tests for synthetic quote handling (is_synthetic_quote flag).

Covers:
  - Fix 1: bid_ask_class forced to MID when bid=ask=0
  - Fix 1: is_aggressive forced to False when bid=ask=0
  - Fix 2: conviction_score reduced by 40% on synthetic quotes
  - Fix 3: is_synthetic_quote field present and accessible (DB column smoke test)
  - Regression: real quotes with bid/ask still classify normally
"""
from parsers.options_flow_parser import parse_tradier_trade

_BASE = {
    "symbol": "AAPL  260117C00180000",
    "size":   100,
    "last":   3.50,
    "timestamp": 1745000000000,
}


def _real_quote(**overrides):
    return {**_BASE, "bid": 3.40, "ask": 3.60, **overrides}


def _synthetic_quote(**overrides):
    return {**_BASE, "bid": 0, "ask": 0, **overrides}


# ---------------------------------------------------------------------------
# Fix 1 — bid_ask_class forced to MID on synthetic quotes
# ---------------------------------------------------------------------------

def test_synthetic_bid_ask_class_is_mid():
    ev = parse_tradier_trade(_synthetic_quote())
    assert ev is not None
    assert ev.is_synthetic_quote is True
    assert ev.bid_ask_class == "MID"


def test_synthetic_is_not_aggressive():
    ev = parse_tradier_trade(_synthetic_quote())
    assert ev is not None
    assert ev.is_aggressive is False


def test_synthetic_is_not_golden_sweep():
    """Golden sweep requires is_aggressive=True; synthetic trades cannot be golden."""
    ev = parse_tradier_trade(_synthetic_quote(size=500, last=15.0))
    assert ev is not None
    assert ev.is_golden_sweep is False


# ---------------------------------------------------------------------------
# Fix 2 — conviction haircut on synthetic quotes
# ---------------------------------------------------------------------------

def test_synthetic_conviction_is_haircut():
    """Synthetic conviction must be strictly less than real conviction for same trade."""
    real  = parse_tradier_trade(_real_quote())
    synth = parse_tradier_trade(_synthetic_quote())
    assert real is not None and synth is not None
    assert synth.conviction_score < real.conviction_score


def test_synthetic_conviction_haircut_is_40_pct():
    """Synthetic conviction = real_conviction * 0.6, rounded to 3dp."""
    synth = parse_tradier_trade(_synthetic_quote())
    real  = parse_tradier_trade(_real_quote())
    assert synth is not None and real is not None
    expected = round(real.conviction_score * 0.6, 3)
    assert synth.conviction_score == expected


def test_synthetic_high_premium_conviction_still_haircut():
    """Even a large synthetic trade should not escape the haircut."""
    synth = parse_tradier_trade(_synthetic_quote(size=2000, last=3.50))
    real  = parse_tradier_trade(_real_quote(size=2000, last=3.50))
    assert synth is not None and real is not None
    assert synth.conviction_score < real.conviction_score
    assert synth.conviction_score == round(real.conviction_score * 0.6, 3)


# ---------------------------------------------------------------------------
# Fix 3 — is_synthetic_quote flag is set and accessible
# ---------------------------------------------------------------------------

def test_synthetic_flag_true_when_no_quotes():
    ev = parse_tradier_trade(_synthetic_quote())
    assert ev is not None
    assert ev.is_synthetic_quote is True


def test_synthetic_flag_false_when_real_quotes():
    ev = parse_tradier_trade(_real_quote())
    assert ev is not None
    assert ev.is_synthetic_quote is False


def test_synthetic_flag_false_when_only_bid_present():
    """Partial quote (bid present, ask=0): ask<=bid so classify_bid_ask returns MID,
    but is_synthetic_quote should be False (we had real bid data)."""
    ev = parse_tradier_trade({**_BASE, "bid": 3.40, "ask": 0})
    assert ev is not None
    assert ev.is_synthetic_quote is False


# ---------------------------------------------------------------------------
# Regression — real quotes still classify normally
# ---------------------------------------------------------------------------

def test_real_above_ask_still_classified():
    """Fill above ask on real quote must still produce ABOVE_ASK."""
    ev = parse_tradier_trade(_real_quote(last=3.70, bid=3.40, ask=3.60))
    assert ev is not None
    assert ev.bid_ask_class == "ABOVE_ASK"
    assert ev.is_aggressive is True
    assert ev.is_synthetic_quote is False


def test_real_at_bid_still_classified():
    ev = parse_tradier_trade(_real_quote(last=3.40, bid=3.40, ask=3.60))
    assert ev is not None
    assert ev.bid_ask_class == "AT_BID"
    assert ev.is_aggressive is False
    assert ev.is_synthetic_quote is False


def test_real_mid_still_classified():
    ev = parse_tradier_trade(_real_quote(last=3.50, bid=3.40, ask=3.60))
    assert ev is not None
    assert ev.bid_ask_class == "MID"
    assert ev.is_aggressive is False


def test_zero_size_returns_none():
    """Regression: zero-size trades still rejected regardless of quote data."""
    ev = parse_tradier_trade({**_BASE, "size": 0, "bid": 0, "ask": 0})
    assert ev is None
