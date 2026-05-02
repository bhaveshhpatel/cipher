"""
test_order_side_classifier_coverage.py

Covers parsers/order_side_classifier.py (was at 33%, lines 48-59 all missed).

Lines 48-59 are the _LOOKUP hit + UNKNOWN fallback paths in
order_side_to_direction(). Every branch needs at least one hit.
"""
from parsers.order_side_classifier import order_side_to_direction


# ── _LOOKUP hits ──────────────────────────────────────────────────────────────

def test_buy_call_is_repeat_buy():
    assert order_side_to_direction("BUY", "CALL") == "REPEAT_BUY"


def test_sell_put_is_repeat_buy():
    assert order_side_to_direction("SELL", "PUT") == "REPEAT_BUY"


def test_buy_put_is_repeat_sell():
    assert order_side_to_direction("BUY", "PUT") == "REPEAT_SELL"


def test_sell_call_is_repeat_sell():
    assert order_side_to_direction("SELL", "CALL") == "REPEAT_SELL"


# ── UNKNOWN fallback paths ────────────────────────────────────────────────────

def test_unknown_side_call_returns_repeat_buy():
    assert order_side_to_direction("UNKNOWN", "CALL") == "REPEAT_BUY"


def test_unknown_side_put_returns_repeat_sell():
    assert order_side_to_direction("UNKNOWN", "PUT") == "REPEAT_SELL"


def test_unknown_side_unknown_type_returns_repeat_buy():
    # neither CALL nor PUT -> bullish default
    assert order_side_to_direction("UNKNOWN", "UNKNOWN") == "REPEAT_BUY"


def test_empty_strings_return_repeat_buy():
    # both empty -> side="" ctype="" -> no lookup hit -> not PUT -> REPEAT_BUY
    assert order_side_to_direction("", "") == "REPEAT_BUY"


# ── Case-insensitivity ────────────────────────────────────────────────────────

def test_lowercase_buy_call():
    assert order_side_to_direction("buy", "call") == "REPEAT_BUY"


def test_mixed_case_sell_put():
    assert order_side_to_direction("Sell", "Put") == "REPEAT_BUY"


def test_lowercase_buy_put():
    assert order_side_to_direction("buy", "put") == "REPEAT_SELL"


def test_lowercase_sell_call():
    assert order_side_to_direction("sell", "call") == "REPEAT_SELL"


# ── None-guard (order_side or '' guard) ───────────────────────────────────────

def test_none_order_side_returns_repeat_buy():
    # order_side=None -> (None or '') -> '' strip upper -> '' -> no match -> CALL default
    assert order_side_to_direction(None, "CALL") == "REPEAT_BUY"


def test_none_contract_type_returns_repeat_buy():
    assert order_side_to_direction("BUY", None) == "REPEAT_BUY"
