"""
ING-004: Fallback underlying_price from registry stock_price.

Test matrix (5 cases, D-1 through D-5):
  D-1  underlying_price=0 in tick, registry ready, stock_price=150.0
       -> ev.underlying_price = 150.0; fallback_applied counter +1
  D-2  underlying_price=155.0 in tick, registry ready
       -> registry NOT called for price; ev.underlying_price = 155.0; counter unchanged
  D-3  underlying_price=0, registry not ready (is_ready()=False)
       -> ev.underlying_price = 0.0; counter unchanged
  D-4  underlying_price=0, registry ready, stock_price()=0.0 (unknown ticker)
       -> ev.underlying_price = 0.0; counter unchanged (guard if sp > 0 blocks)
  D-5  underlying_price=0, registry ready, stock_price=150.0, meta lookup returns None
       -> fallback still fires; ev.underlying_price = 150.0; counter +1
       (confirms fallback is independent of meta)

Deliberation: 2026-05-03 -- SA/PBE/QA all signed off.
Pre-merge panel fix (2026-05-03): expiration_date changed to '2027-01-16' (LEAPS, ~253 DTE).
  These tests call parse_tradier_trade() directly -- the accumulator DTE-tier gate (Gate 6)
  never executes here, so DTE value is irrelevant to all assertions. The change removes
  ambiguity for future readers. No functional impact.
"""
from unittest.mock import MagicMock, patch
import pytest


def _make_raw(
    underlying_price: float = 0.0,
    premium_fill: float = 5.0,   # fill=5.0, size=100 => premium=$50,000 (clears $10k parser floor)
    size: int = 100,
    symbol: str = "AAPL270116C00150000",
) -> dict:
    # expiration_date = 2027-01-16 (~253 DTE from 2026-05-03, LEAPS range).
    # DTE has no effect here: these tests exercise parse_tradier_trade() only.
    # The accumulator DTE-tier gate (Gate 6) is never reached in this test file.
    return {
        "symbol":           symbol,
        "underlying":       "AAPL",
        "last":             premium_fill,
        "bid":              4.90,
        "ask":              5.10,
        "size":             size,
        "underlying_price": underlying_price,
        "option_type":      "C",
        "strike":           150.0,
        "expiration_date":  "2027-01-16",
        "exchange_count":   1,
        "fill_count":       1,
    }


def _make_registry(ready: bool = True, stock_price: float = 150.0, meta=None):
    reg = MagicMock()
    reg.is_ready.return_value = ready
    reg.stock_price.return_value = stock_price
    reg.lookup.return_value = meta
    return reg


# ---------------------------------------------------------------------------
# D-1: tick has underlying_price=0, registry ready, stock_price=150.0
#      -> fallback applied, counter incremented
# ---------------------------------------------------------------------------
def test_d1_fallback_applied_when_tick_zero():
    from parsers import options_flow_parser as mod
    mod._stats["underlying_price_fallback_applied"] = 0

    reg = _make_registry(ready=True, stock_price=150.0, meta=None)
    with patch.object(mod, "get_registry", return_value=reg):
        ev = mod.parse_tradier_trade(_make_raw(underlying_price=0.0))

    assert ev is not None and not isinstance(ev, str)
    assert ev.underlying_price == 150.0
    assert mod._stats["underlying_price_fallback_applied"] == 1


# ---------------------------------------------------------------------------
# D-2: tick has underlying_price=155.0 -> registry NOT called for stock price
#      counter must stay unchanged
# ---------------------------------------------------------------------------
def test_d2_no_fallback_when_tick_has_price():
    from parsers import options_flow_parser as mod
    mod._stats["underlying_price_fallback_applied"] = 0

    reg = _make_registry(ready=True, stock_price=150.0, meta=None)
    with patch.object(mod, "get_registry", return_value=reg):
        ev = mod.parse_tradier_trade(_make_raw(underlying_price=155.0))

    assert ev is not None and not isinstance(ev, str)
    assert ev.underlying_price == 155.0
    reg.stock_price.assert_not_called()
    assert mod._stats["underlying_price_fallback_applied"] == 0


# ---------------------------------------------------------------------------
# D-3: registry not ready -> underlying_price stays 0.0, counter unchanged
# ---------------------------------------------------------------------------
def test_d3_no_fallback_when_registry_not_ready():
    from parsers import options_flow_parser as mod
    mod._stats["underlying_price_fallback_applied"] = 0

    reg = _make_registry(ready=False, stock_price=150.0, meta=None)
    with patch.object(mod, "get_registry", return_value=reg):
        ev = mod.parse_tradier_trade(_make_raw(underlying_price=0.0))

    assert ev is not None and not isinstance(ev, str)
    assert ev.underlying_price == 0.0
    assert mod._stats["underlying_price_fallback_applied"] == 0


# ---------------------------------------------------------------------------
# D-4: registry ready but stock_price=0.0 (unknown ticker)
#      -> guard "if sp > 0" blocks mutation; counter unchanged
# ---------------------------------------------------------------------------
def test_d4_no_fallback_when_stock_price_zero():
    from parsers import options_flow_parser as mod
    mod._stats["underlying_price_fallback_applied"] = 0

    reg = _make_registry(ready=True, stock_price=0.0, meta=None)
    with patch.object(mod, "get_registry", return_value=reg):
        ev = mod.parse_tradier_trade(_make_raw(underlying_price=0.0))

    assert ev is not None and not isinstance(ev, str)
    assert ev.underlying_price == 0.0
    assert mod._stats["underlying_price_fallback_applied"] == 0


# ---------------------------------------------------------------------------
# D-5: registry ready, meta=None (lookup miss), but stock_price=150.0
#      -> fallback fires independently of meta; counter +1
# ---------------------------------------------------------------------------
def test_d5_fallback_independent_of_meta_lookup():
    from parsers import options_flow_parser as mod
    mod._stats["underlying_price_fallback_applied"] = 0

    reg = _make_registry(ready=True, stock_price=150.0, meta=None)
    with patch.object(mod, "get_registry", return_value=reg):
        ev = mod.parse_tradier_trade(_make_raw(underlying_price=0.0))

    assert ev is not None and not isinstance(ev, str)
    assert ev.underlying_price == 150.0
    assert mod._stats["underlying_price_fallback_applied"] == 1
    # meta was None but fallback still fired
    reg.lookup.assert_called_once()
    reg.stock_price.assert_called_once_with("AAPL")
