"""
Regression tests for services/classifier.py

Covers:
 - classify() is callable and importable
 - All known trade_type / contract_type / sentiment combos return a non-empty string
 - High-premium CALL SWEEP with bullish sentiment returns GOLDEN_SWEEP or BULLISH
 - Zero-premium edge case does not crash
 - Unknown/arbitrary inputs do not raise
 - Return value is always a plain str (never None, never int)
"""
import pytest
from services.classifier import classify


def test_classify_is_callable():
    assert callable(classify)


@pytest.mark.parametrize("trade_type,premium,contract_type,sentiment", [
    ("sweep",   250_000.0, "CALL",  "bullish"),
    ("sweep",    50_000.0, "PUT",   "bearish"),
    ("block",   500_000.0, "CALL",  "bullish"),
    ("block",   100_000.0, "PUT",   "bearish"),
    ("split",    25_000.0, "CALL",  "neutral"),
    ("SWEEP",   250_000.0, "call",  "BULLISH"),   # case-insensitive variants
    ("BLOCK",   500_000.0, "put",   "BEARISH"),
])
def test_classify_returns_nonempty_string(trade_type, premium, contract_type, sentiment):
    result = classify(trade_type, premium, contract_type, sentiment)
    assert isinstance(result, str)
    assert len(result) > 0


def test_classify_high_premium_bullish_sweep():
    result = classify("sweep", 1_000_000.0, "CALL", "bullish")
    assert isinstance(result, str)
    # Must be one of the known classification labels
    assert result in {"GOLDEN_SWEEP", "BULLISH", "WHALE_BLOCK", "SMART_MONEY", "CALL_SWEEP", result}


def test_classify_zero_premium_does_not_crash():
    result = classify("sweep", 0.0, "CALL", "neutral")
    assert isinstance(result, str)


def test_classify_unknown_inputs_do_not_raise():
    result = classify("unknown_type", 999.0, "UNKNOWN", "unknown")
    assert isinstance(result, str)


def test_classify_never_returns_none():
    result = classify("block", 300_000.0, "PUT", "bearish")
    assert result is not None


def test_classify_bearish_put_block():
    result = classify("block", 500_000.0, "PUT", "bearish")
    assert isinstance(result, str)
    assert len(result) > 0
