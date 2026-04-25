"""
Unit tests for parsers/bid_ask_classifier.py and parsers/trade_type_detector.py

Covers:
  classify_bid_ask
  1.  fill > ask  → ABOVE_ASK
  2.  fill == ask → AT_ASK
  3.  fill == bid → AT_BID
  4.  fill < bid  → BELOW_BID
  5.  fill between bid and ask → MID
  6.  Crossed market (bid > ask) → MID (safe fallback)
  7.  All zeros → MID fallback
  8.  Values at exact midpoint → MID

  is_aggressive
  9.  ABOVE_ASK → True
  10. AT_ASK    → True
  11. MID       → False
  12. AT_BID    → False
  13. BELOW_BID → False
  14. Unknown value → False

  detect_trade_type
  15. exchange_count >= 3           → SWEEP
  16. fill_count >= 3               → SPLIT
  17. premium >= 500_000, size >= 50 → BLOCK
  18. fallback                      → SINGLE
  19. SWEEP takes precedence over BLOCK
  20. fill_count >= 3 with exchange_count=1 → SPLIT not SWEEP

  is_golden_sweep
  21. SWEEP + premium >= 1_000_000 + aggressive → True
  22. SWEEP + premium < 1_000_000              → False
  23. BLOCK + premium >= 1_000_000 + aggressive → False
  24. SWEEP + premium >= 1_000_000 + not aggressive → False
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parsers.bid_ask_classifier import classify_bid_ask, is_aggressive
from parsers.trade_type_detector import detect_trade_type, is_golden_sweep


# ============================================================
# classify_bid_ask
# ============================================================

# 1
def test_classify_above_ask():
    assert classify_bid_ask(3.70, 3.40, 3.60) == "ABOVE_ASK"


# 2
def test_classify_at_ask():
    assert classify_bid_ask(3.60, 3.40, 3.60) == "AT_ASK"


# 3
def test_classify_at_bid():
    assert classify_bid_ask(3.40, 3.40, 3.60) == "AT_BID"


# 4
def test_classify_below_bid():
    assert classify_bid_ask(3.20, 3.40, 3.60) == "BELOW_BID"


# 5
def test_classify_mid():
    assert classify_bid_ask(3.50, 3.40, 3.60) == "MID"


# 6
def test_classify_crossed_market_mid_fallback():
    # bid > ask → crossed market, safe fallback to MID
    result = classify_bid_ask(3.50, 3.70, 3.50)
    assert result == "MID"


# 7
def test_classify_all_zeros_mid_fallback():
    assert classify_bid_ask(0.0, 0.0, 0.0) == "MID"


# 8
def test_classify_exact_midpoint():
    assert classify_bid_ask(3.50, 3.40, 3.60) == "MID"


# ============================================================
# is_aggressive
# ============================================================

# 9
def test_is_aggressive_above_ask():
    assert is_aggressive("ABOVE_ASK") is True


# 10
def test_is_aggressive_at_ask():
    assert is_aggressive("AT_ASK") is True


# 11
def test_is_aggressive_mid_false():
    assert is_aggressive("MID") is False


# 12
def test_is_aggressive_at_bid_false():
    assert is_aggressive("AT_BID") is False


# 13
def test_is_aggressive_below_bid_false():
    assert is_aggressive("BELOW_BID") is False


# 14
def test_is_aggressive_unknown_false():
    assert is_aggressive("GARBAGE") is False


# ============================================================
# detect_trade_type
# ============================================================

# 15
def test_detect_sweep_exchange_count():
    assert detect_trade_type(size=10, premium=50_000, exchange_count=3, fill_count=1) == "SWEEP"


# 16
def test_detect_split_fill_count():
    assert detect_trade_type(size=10, premium=50_000, exchange_count=1, fill_count=3) == "SPLIT"


# 17
def test_detect_block():
    assert detect_trade_type(size=50, premium=500_000, exchange_count=1, fill_count=1) == "BLOCK"


# 18
def test_detect_single_fallback():
    assert detect_trade_type(size=5, premium=1_000, exchange_count=1, fill_count=1) == "SINGLE"


# 19
def test_detect_sweep_over_block():
    # exchange_count=3 qualifies as SWEEP even if premium/size also qualify as BLOCK
    assert detect_trade_type(size=100, premium=1_000_000, exchange_count=3, fill_count=1) == "SWEEP"


# 20
def test_detect_split_not_sweep_single_exchange():
    result = detect_trade_type(size=10, premium=50_000, exchange_count=1, fill_count=3)
    assert result == "SPLIT"
    assert result != "SWEEP"


# ============================================================
# is_golden_sweep
# ============================================================

# 21
def test_is_golden_sweep_true():
    assert is_golden_sweep("SWEEP", 1_500_000, True) is True


# 22
def test_is_golden_sweep_false_low_premium():
    assert is_golden_sweep("SWEEP", 500_000, True) is False


# 23
def test_is_golden_sweep_false_wrong_type():
    assert is_golden_sweep("BLOCK", 1_500_000, True) is False


# 24
def test_is_golden_sweep_false_not_aggressive():
    assert is_golden_sweep("SWEEP", 1_500_000, False) is False
