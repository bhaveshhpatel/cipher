"""
Tests for services/classifier.py

Covers (merged from test_classifier_coverage.py as of 2026-04-27):
 - classify() is callable and importable
 - All known trade_type / contract_type / sentiment combos return a non-empty string
 - Case-insensitive inputs
 - High-premium CALL SWEEP with bullish sentiment returns GOLDEN_SWEEP
 - Zero-premium edge case does not crash
 - None / non-numeric premium treated as 0.0, no crash
 - Unknown/arbitrary inputs do not raise; return UNUSUAL_CALL, UNUSUAL_PUT, or FLOW
 - Return value is always a plain str (never None)
 - DARK_POOL block fallthrough when direction mismatch
 - Exact threshold boundary values for every tier
 - PUT_SWEEP path
"""
import pytest
from services.classifier import classify


# ---------------------------------------------------------------------------
# Smoke
# ---------------------------------------------------------------------------

def test_classify_is_callable():
    assert callable(classify)


@pytest.mark.parametrize("trade_type,premium,contract_type,sentiment", [
    ("sweep",   250_000.0, "CALL",  "bullish"),
    ("sweep",    50_000.0, "PUT",   "bearish"),
    ("block",   500_000.0, "CALL",  "bullish"),
    ("block",   100_000.0, "PUT",   "bearish"),
    ("split",    25_000.0, "CALL",  "neutral"),
    ("SWEEP",   250_000.0, "call",  "BULLISH"),   # case-insensitive
    ("BLOCK",   500_000.0, "put",   "BEARISH"),
])
def test_classify_returns_nonempty_string(trade_type, premium, contract_type, sentiment):
    result = classify(trade_type, premium, contract_type, sentiment)
    assert isinstance(result, str)
    assert len(result) > 0


def test_classify_never_returns_none():
    assert classify("block", 300_000.0, "PUT", "bearish") is not None


# ---------------------------------------------------------------------------
# Core classification labels
# ---------------------------------------------------------------------------

def test_classify_golden_sweep_exact_threshold():
    assert classify("sweep", 500_000.0, "CALL", "bullish") == "GOLDEN_SWEEP"


def test_classify_golden_sweep_just_below_threshold():
    assert classify("sweep", 499_999.99, "CALL", "bullish") == "CALL_SWEEP"


def test_classify_golden_sweep_high_premium():
    assert classify("sweep", 1_000_000.0, "CALL", "bullish") == "GOLDEN_SWEEP"


def test_classify_whale_block_exact_threshold():
    assert classify("block", 1_000_000.0, "PUT", "bearish") == "WHALE_BLOCK"


def test_classify_dark_pool_bull():
    assert classify("block", 600_000.0, "CALL", "bullish") == "DARK_POOL_BULL"


def test_classify_dark_pool_bear():
    assert classify("block", 500_000.0, "PUT", "bearish") == "DARK_POOL_BEAR"


def test_classify_call_sweep():
    assert classify("sweep", 200_000.0, "CALL", "bullish") == "CALL_SWEEP"


def test_classify_put_sweep_bearish():
    assert classify("sweep", 200_000.0, "PUT", "bearish") == "PUT_SWEEP"


def test_classify_smart_money_exact_threshold():
    # block, exactly 100k, bullish — below DARK_POOL threshold
    assert classify("block", 100_000.0, "CALL", "bullish") == "SMART_MONEY"


# ---------------------------------------------------------------------------
# DARK_POOL fallthrough — block >= 500k but mismatched direction
# ---------------------------------------------------------------------------

def test_classify_dark_pool_block_call_bearish_falls_through():
    result = classify("block", 600_000.0, "CALL", "bearish")
    assert result not in ("DARK_POOL_BULL", "DARK_POOL_BEAR")
    assert isinstance(result, str)


def test_classify_dark_pool_block_put_bullish_falls_through():
    result = classify("block", 600_000.0, "PUT", "bullish")
    assert result not in ("DARK_POOL_BULL", "DARK_POOL_BEAR")
    assert isinstance(result, str)


def test_classify_dark_pool_block_call_neutral_falls_through():
    result = classify("block", 600_000.0, "CALL", "neutral")
    assert result not in ("DARK_POOL_BULL", "DARK_POOL_BEAR")


# ---------------------------------------------------------------------------
# Unusual / fallback paths
# ---------------------------------------------------------------------------

def test_classify_non_sweep_non_block_call_returns_unusual_call():
    assert classify("split", 10_000.0, "CALL", "neutral") == "UNUSUAL_CALL"


def test_classify_non_sweep_non_block_put_returns_unusual_put():
    assert classify("split", 10_000.0, "PUT", "neutral") == "UNUSUAL_PUT"


def test_classify_unknown_trade_type_unknown_contract_returns_flow():
    assert classify("split", 10_000.0, "UNKNOWN", "neutral") == "FLOW"


def test_classify_empty_strings_return_flow():
    assert classify("", 0.0, "", "") == "FLOW"


# ---------------------------------------------------------------------------
# Edge: premium coercion
# ---------------------------------------------------------------------------

def test_classify_zero_premium_does_not_crash():
    result = classify("sweep", 0.0, "CALL", "neutral")
    assert isinstance(result, str)


def test_classify_none_premium_does_not_crash():
    result = classify("sweep", None, "CALL", "bullish")
    assert isinstance(result, str) and len(result) > 0


def test_classify_string_premium_does_not_crash():
    result = classify("block", "not-a-number", "CALL", "bullish")
    assert isinstance(result, str)


def test_classify_none_premium_treated_as_zero():
    # prem=0.0 won’t hit GOLDEN_SWEEP threshold
    assert classify("sweep", None, "CALL", "bullish") != "GOLDEN_SWEEP"
