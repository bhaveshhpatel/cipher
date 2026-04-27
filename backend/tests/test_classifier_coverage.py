"""
P3 coverage tests for services/classifier.py.

Targets uncovered lines:
  - Lines 53-54: premium with None/non-numeric value → prem=0.0, no crash
  - Line 62: DARK_POOL_THRESHOLD block but not CALL+bullish or PUT+bearish → falls through
  - Lines 68-79: unknown trade_type with CALL → UNUSUAL_CALL
                 unknown trade_type with PUT  → UNUSUAL_PUT
                 unknown trade_type, unknown contract → FLOW
  - Exact boundary values: premium == threshold
  - Sweep with PUT and bearish sentiment → PUT_SWEEP
  - Block >= 500k, CALL, neutral (not bullish) → falls through DARK_POOL
"""
from services.classifier import classify


# ---------------------------------------------------------------------------
# Non-numeric premium edge cases (lines 53-54)
# ---------------------------------------------------------------------------

def test_classify_none_premium_does_not_crash():
    result = classify("sweep", None, "CALL", "bullish")
    assert isinstance(result, str) and len(result) > 0


def test_classify_string_premium_does_not_crash():
    result = classify("block", "not-a-number", "CALL", "bullish")
    assert isinstance(result, str)


def test_classify_none_premium_treated_as_zero():
    # With prem=0.0, sweep+CALL+bullish won't hit GOLDEN_SWEEP threshold
    result = classify("sweep", None, "CALL", "bullish")
    assert result != "GOLDEN_SWEEP"


# ---------------------------------------------------------------------------
# DARK_POOL block falls through (line 62) — block >= 500k, wrong direction
# ---------------------------------------------------------------------------

def test_classify_dark_pool_block_call_bearish_falls_through():
    result = classify("block", 600_000.0, "CALL", "bearish")
    assert result not in ("DARK_POOL_BULL", "DARK_POOL_BEAR")
    assert isinstance(result, str)


def test_classify_dark_pool_block_put_bullish_falls_through():
    result = classify("block", 600_000.0, "PUT", "bullish")
    assert result not in ("DARK_POOL_BULL", "DARK_POOL_BEAR")
    assert isinstance(result, str)


def test_classify_dark_pool_block_neutral_falls_through():
    result = classify("block", 600_000.0, "CALL", "neutral")
    assert result not in ("DARK_POOL_BULL", "DARK_POOL_BEAR")


# ---------------------------------------------------------------------------
# Unknown trade_type paths (lines 68-79)
# ---------------------------------------------------------------------------

def test_classify_unknown_trade_type_call_returns_unusual_call():
    assert classify("split", 10_000.0, "CALL", "neutral") == "UNUSUAL_CALL"


def test_classify_unknown_trade_type_put_returns_unusual_put():
    assert classify("split", 10_000.0, "PUT", "neutral") == "UNUSUAL_PUT"


def test_classify_unknown_trade_type_unknown_contract_returns_flow():
    assert classify("split", 10_000.0, "UNKNOWN", "neutral") == "FLOW"


def test_classify_empty_strings_return_flow():
    assert classify("", 0.0, "", "") == "FLOW"


# ---------------------------------------------------------------------------
# Exact boundary values
# ---------------------------------------------------------------------------

def test_classify_golden_sweep_exact_threshold():
    assert classify("sweep", 500_000.0, "CALL", "bullish") == "GOLDEN_SWEEP"


def test_classify_golden_sweep_just_below_threshold():
    assert classify("sweep", 499_999.99, "CALL", "bullish") == "CALL_SWEEP"


def test_classify_whale_block_exact_threshold():
    assert classify("block", 1_000_000.0, "PUT", "bearish") == "WHALE_BLOCK"


def test_classify_smart_money_exact_threshold():
    # block, exactly 100k, bullish, CALL — below DARK_POOL threshold
    assert classify("block", 100_000.0, "CALL", "bullish") == "SMART_MONEY"


def test_classify_put_sweep_bearish():
    assert classify("sweep", 200_000.0, "PUT", "bearish") == "PUT_SWEEP"
