"""
test_tier_engine_c3.py

100% coverage of the C-3 fix in tier_engine:

  C-3: assign_tiers() now accepts require_oi parameter.
       - require_oi=False (default): OI gate skipped. T1/T2 reachable
         on volume+price alone. Stable pre-build tier assignment.
       - require_oi=True: OI gate enforced. Used post-build when OI is
         populated from chain data.

Tests:
  1.  require_oi_false_skips_oi_gate         — zero OI → T1 if vol+price qualify
  2.  require_oi_true_enforces_oi_gate       — zero OI → T3 even if vol+price qualify
  3.  require_oi_default_is_false            — default assign_tiers() skips OI gate
  4.  t1_reachable_without_oi               — high vol+price, oi=0, require_oi=False → T1
  5.  t2_reachable_without_oi               — medium vol+price, oi=0, require_oi=False → T2
  6.  require_oi_true_t1_with_sufficient_oi  — vol+price+oi all qualify → T1
  7.  require_oi_true_t2_with_sufficient_oi  — medium vol+price+oi → T2
  8.  zero_oi_stable_across_two_calls        — two assign_tiers calls, same result
  9.  classify_require_oi_false_direct       — _classify direct unit test, require_oi=False
  10. classify_require_oi_true_direct        — _classify direct unit test, require_oi=True
"""
import asyncio
import pytest
from unittest.mock import patch

from services.tier_engine import _classify, assign_tiers, _DEFAULT_THRESHOLDS


class _Q:
    """Minimal SymbolQuote stand-in."""
    def __init__(self, symbol, last_price, volume, average_volume, open_interest=0):
        self.symbol         = symbol
        self.last_price     = last_price
        self.volume         = volume
        self.average_volume = average_volume
        self.open_interest  = open_interest


# High enough to qualify T1 on vol+price
_T1_VOL   = 25_000_000
_T1_PRICE = 150.0
_T1_OI    = 2_000  # above t1_min_oi=1000

# Medium — qualifies T2 on vol+price
_T2_VOL   = 3_000_000
_T2_PRICE = 50.0
_T2_OI    = 600   # above t2_min_oi=500

_THRESH = dict(_DEFAULT_THRESHOLDS)


# ---------------------------------------------------------------------------
# _classify unit tests (synchronous)
# ---------------------------------------------------------------------------

def test_classify_require_oi_false_direct():
    q = _Q("AAPL", last_price=_T1_PRICE, volume=_T1_VOL, average_volume=_T1_VOL, open_interest=0)
    tier = _classify(q, _THRESH, require_oi=False)
    assert tier == 1


def test_classify_require_oi_true_direct():
    q = _Q("AAPL", last_price=_T1_PRICE, volume=_T1_VOL, average_volume=_T1_VOL, open_interest=0)
    tier = _classify(q, _THRESH, require_oi=True)
    assert tier == 3  # zero OI fails t1_min_oi gate


# ---------------------------------------------------------------------------
# assign_tiers async tests
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def test_require_oi_false_skips_oi_gate():
    q = _Q("AAPL", last_price=_T1_PRICE, volume=_T1_VOL, average_volume=_T1_VOL, open_interest=0)
    with patch("services.tier_engine._fetch_thresholds", return_value=_THRESH):
        result = _run(assign_tiers([q], thresholds=_THRESH, require_oi=False))
    assert result["AAPL"] == 1


def test_require_oi_true_enforces_oi_gate():
    q = _Q("AAPL", last_price=_T1_PRICE, volume=_T1_VOL, average_volume=_T1_VOL, open_interest=0)
    with patch("services.tier_engine._fetch_thresholds", return_value=_THRESH):
        result = _run(assign_tiers([q], thresholds=_THRESH, require_oi=True))
    assert result["AAPL"] == 3


def test_require_oi_default_is_false():
    """Default call (no require_oi) must skip OI gate — same as require_oi=False."""
    q = _Q("MSFT", last_price=_T1_PRICE, volume=_T1_VOL, average_volume=_T1_VOL, open_interest=0)
    with patch("services.tier_engine._fetch_thresholds", return_value=_THRESH):
        result = _run(assign_tiers([q], thresholds=_THRESH))
    assert result["MSFT"] == 1


def test_t1_reachable_without_oi():
    q = _Q("SPY", last_price=_T1_PRICE, volume=_T1_VOL, average_volume=_T1_VOL, open_interest=0)
    result = _run(assign_tiers([q], thresholds=_THRESH, require_oi=False))
    assert result["SPY"] == 1


def test_t2_reachable_without_oi():
    q = _Q("QQQ", last_price=_T2_PRICE, volume=_T2_VOL, average_volume=_T2_VOL, open_interest=0)
    result = _run(assign_tiers([q], thresholds=_THRESH, require_oi=False))
    assert result["QQQ"] == 2


def test_require_oi_true_t1_with_sufficient_oi():
    q = _Q("AAPL", last_price=_T1_PRICE, volume=_T1_VOL, average_volume=_T1_VOL, open_interest=_T1_OI)
    result = _run(assign_tiers([q], thresholds=_THRESH, require_oi=True))
    assert result["AAPL"] == 1


def test_require_oi_true_t2_with_sufficient_oi():
    q = _Q("QQQ", last_price=_T2_PRICE, volume=_T2_VOL, average_volume=_T2_VOL, open_interest=_T2_OI)
    result = _run(assign_tiers([q], thresholds=_THRESH, require_oi=True))
    assert result["QQQ"] == 2


def test_zero_oi_stable_across_two_calls():
    """Two consecutive calls with require_oi=False must return identical tier maps."""
    quotes = [
        _Q("AAPL", last_price=_T1_PRICE, volume=_T1_VOL, average_volume=_T1_VOL, open_interest=0),
        _Q("MSFT", last_price=_T2_PRICE, volume=_T2_VOL, average_volume=_T2_VOL, open_interest=0),
    ]
    r1 = _run(assign_tiers(quotes, thresholds=_THRESH, require_oi=False))
    r2 = _run(assign_tiers(quotes, thresholds=_THRESH, require_oi=False))
    assert r1 == r2
    assert r1["AAPL"] == 1
    assert r1["MSFT"] == 2
