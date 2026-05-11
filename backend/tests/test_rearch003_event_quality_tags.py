"""
REARCH-003: Flow Event Quality Tagging — test suite

Covers:
  E1  classify_bid_ask — ASK side at 98% threshold
  E2  classify_bid_ask — BID side at 102% threshold
  E3  classify_bid_ask — exact ask → ASK
  E4  classify_bid_ask — exact bid → BID
  E5  classify_bid_ask — mid price → MID
  E6  classify_bid_ask — zero ask guard → MID
  E7  classify_bid_ask — crossed market guard → MID
  E8  compute_vol_oi_signal — high ratio (>= 0.5) → True
  E9  compute_vol_oi_signal — low ratio (< 0.5) → False
  E10 compute_vol_oi_signal — None inputs → None
  E11 persist_flow_event integration — all quality tag fields present in buffered row
  E12 REARCH-010 regression guard — purged columns absent from row dict
"""
import asyncio
import os
from typing import Optional
from unittest.mock import patch

import pytest

# Ensure Supabase env vars are unset so persist_flow_event short-circuits
# at _is_configured() and we can inspect the buffer directly.
os.environ.pop("SUPABASE_URL", None)
os.environ.pop("SUPABASE_SERVICE_ROLE_KEY", None)
os.environ.pop("SUPABASE_SERVICE_KEY", None)

import services.flow_store as fs  # noqa: E402  (import after env teardown)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ev_dict(
    fill_price: float = 5.10,
    bid: float = 4.80,
    ask: float = 5.20,
    underlying_price: float = 150.0,
    premium: float = 510.0,
    open_interest: int = 200,
) -> dict:
    """Minimal ev_dict that passes the field set expected by persist_flow_event."""
    return {
        "ticker":           "AAPL",
        "contract_type":    "CALL",
        "strike":           150.0,
        "expiry":           "2026-06-20",
        "dte":              40,
        "fill_price":       fill_price,
        "bid":              bid,
        "ask":              ask,
        "size":             10,
        "premium":          premium,
        "trade_type":       "BTO",
        "bid_ask_class":    "MID",   # legacy field — should be overwritten by REARCH-003
        "is_aggressive":    False,
        "sentiment":        "BULLISH",
        "exchange_count":   1,
        "fill_count":       1,
        "open_interest":    open_interest,
        "iv":               0.35,
        "underlying_price": underlying_price,
        "occ_symbol":       "AAPL260620C00150000",
        "is_synthetic_quote": False,
    }


def _run_persist(ev_dict: dict, vol: Optional[int], oi: Optional[int]) -> dict:
    """
    Drive persist_flow_event() synchronously and return the last buffered row.

    Uses asyncio.run() (Python 3.10+ compatible) instead of the deprecated
    asyncio.get_event_loop().run_until_complete() which raises RuntimeError
    in Python 3.12 when there is no current event loop in the main thread.

    Patches:
      - chain_store.get_contract_vol_oi → returns (vol, oi)
      - _is_configured()                → returns True  (skips env var check)
    """
    fs._flow_event_buffer.clear()

    async def _run():
        with patch("services.flow_store._is_configured", return_value=True), \
             patch(
                 "services.flow_store.get_contract_vol_oi",
                 return_value=(vol, oi),
                 create=True,
             ):
            # Patch the lazy import inside persist_flow_event
            import services.chain_store as cs
            with patch.object(cs, "get_contract_vol_oi", return_value=(vol, oi)):
                await fs.persist_flow_event(ev_dict)

    asyncio.run(_run())
    assert fs._flow_event_buffer, "persist_flow_event did not append to buffer"
    return fs._flow_event_buffer[-1]


# ---------------------------------------------------------------------------
# TestClassifyBidAsk
# ---------------------------------------------------------------------------

class TestClassifyBidAsk:
    """Unit tests for classify_bid_ask() pure function."""

    def test_e1_ask_side_98pct_threshold(self):
        """fill == ask * 0.98 → ASK  (boundary — should be ASK, not MID)."""
        ask = 5.20
        fill = round(ask * 0.98, 6)   # 5.096
        cls = fs.classify_bid_ask(fill, bid=4.80, ask=ask)
        assert cls == "ASK", f"Expected ASK at 98% of ask, got {cls!r} (fill={fill}, ask={ask})"

    def test_e2_bid_side_102pct_threshold(self):
        """fill == bid * 1.02 → BID  (boundary — should be BID, not MID)."""
        bid = 4.80
        fill = round(bid * 1.02, 6)   # 4.896
        cls = fs.classify_bid_ask(fill, bid=bid, ask=5.20)
        assert cls == "BID", f"Expected BID at 102% of bid, got {cls!r} (fill={fill}, bid={bid})"

    def test_e3_exact_ask_is_ask_side(self):
        """fill exactly at ask → ASK."""
        assert fs.classify_bid_ask(5.20, bid=4.80, ask=5.20) == "ASK"

    def test_e4_exact_bid_is_bid_side(self):
        """fill exactly at bid → BID."""
        assert fs.classify_bid_ask(4.80, bid=4.80, ask=5.20) == "BID"

    def test_e5_midpoint_is_mid(self):
        """fill at exact midpoint → MID."""
        assert fs.classify_bid_ask(5.00, bid=4.80, ask=5.20) == "MID"

    def test_e6_zero_ask_guard(self):
        """ask == 0 → MID (synthetic/bad quote guard)."""
        assert fs.classify_bid_ask(0.0, bid=0.0, ask=0.0) == "MID"

    def test_e7_crossed_market_guard(self):
        """bid > ask → MID (crossed market guard)."""
        assert fs.classify_bid_ask(5.00, bid=5.50, ask=5.00) == "MID"

    def test_above_ask_still_ask(self):
        """fill above ask → ASK (aggressive buyer)."""
        assert fs.classify_bid_ask(5.50, bid=4.80, ask=5.20) == "ASK"

    def test_below_bid_still_bid(self):
        """fill below bid → BID (aggressive seller)."""
        assert fs.classify_bid_ask(4.50, bid=4.80, ask=5.20) == "BID"


# ---------------------------------------------------------------------------
# TestComputeVolOiSignal
# ---------------------------------------------------------------------------

class TestComputeVolOiSignal:
    """Unit tests for compute_vol_oi_signal() pure function."""

    def test_e8_high_ratio_returns_true(self):
        """vol/OI >= 0.5 → True."""
        assert fs.compute_vol_oi_signal(vol=500, oi=1000) is True

    def test_e9_low_ratio_returns_false(self):
        """vol/OI < 0.5 → False."""
        assert fs.compute_vol_oi_signal(vol=300, oi=1000) is False

    def test_e10_vol_none_returns_none(self):
        """vol=None → None."""
        assert fs.compute_vol_oi_signal(vol=None, oi=1000) is None

    def test_e10_oi_none_returns_none(self):
        """oi=None → None."""
        assert fs.compute_vol_oi_signal(vol=500, oi=None) is None

    def test_e10_both_none_returns_none(self):
        """vol=None, oi=None → None."""
        assert fs.compute_vol_oi_signal(vol=None, oi=None) is None

    def test_e10_zero_oi_returns_none(self):
        """oi=0 → None (div-by-zero guard)."""
        assert fs.compute_vol_oi_signal(vol=500, oi=0) is None

    def test_exactly_at_threshold(self):
        """vol/OI == 0.5 exactly → True (boundary inclusive)."""
        assert fs.compute_vol_oi_signal(vol=500, oi=1000) is True


# ---------------------------------------------------------------------------
# TestPersistFlowEventTagIntegration
# ---------------------------------------------------------------------------

class TestPersistFlowEventTagIntegration:
    """
    Integration tests: call persist_flow_event() with a mocked chain_store
    and verify quality tag fields are written correctly into the buffer row.

    All async calls are driven via asyncio.run() — compatible with Python
    3.10+ and correctly handles the "no current event loop" constraint that
    Python 3.12 enforces when asyncio.get_event_loop() is called outside an
    async context.
    """

    def test_e11_all_tag_fields_present(self):
        """Row must contain all four REARCH-003 quality tag keys."""
        row = _run_persist(_make_ev_dict(), vol=500, oi=1000)
        for field in ("is_ask_side", "bid_ask_class", "vol_oi_signal",
                      "normalized_premium", "normalized_oi"):
            assert field in row, f"Missing REARCH-003 field: {field!r}"

    def test_e11_ask_side_true_in_row(self):
        """fill == ask → is_ask_side=True, bid_ask_class='ASK'."""
        row = _run_persist(_make_ev_dict(fill_price=5.00, bid=4.80, ask=5.00), vol=500, oi=1000)
        assert row["is_ask_side"] is True
        assert row["bid_ask_class"] == "ASK"

    def test_e11_bid_side_in_row(self):
        """fill == bid → is_ask_side=False, bid_ask_class='BID'."""
        row = _run_persist(_make_ev_dict(fill_price=4.80, bid=4.80, ask=5.20), vol=500, oi=1000)
        assert row["is_ask_side"] is False
        assert row["bid_ask_class"] == "BID"

    def test_e11_vol_oi_high_in_row(self):
        """vol/OI >= 0.5 → vol_oi_signal=True."""
        row = _run_persist(_make_ev_dict(), vol=600, oi=1000)  # 0.6 >= 0.5
        assert row["vol_oi_signal"] is True

    def test_e11_vol_oi_normal_in_row(self):
        """vol/OI < 0.5 → vol_oi_signal=False."""
        row = _run_persist(_make_ev_dict(), vol=300, oi=1000)  # 0.3 < 0.5
        assert row["vol_oi_signal"] is False

    def test_e7_chain_store_miss_unknown(self):
        """Cache miss (vol=None, oi=None) → vol_oi_signal=None (never a gate)."""
        row = _run_persist(_make_ev_dict(), vol=None, oi=None)
        assert row["vol_oi_signal"] is None

    def test_e8_underlying_zero_normalized_none(self):
        """underlying_price=0.0 → normalized_premium=None."""
        row = _run_persist(_make_ev_dict(underlying_price=0.0), vol=500, oi=1000)
        assert row["normalized_premium"] is None

    def test_e9_underlying_none_normalized_none(self):
        """underlying_price absent → normalized_premium=None."""
        ev = _make_ev_dict()
        ev.pop("underlying_price", None)
        row = _run_persist(ev, vol=500, oi=1000)
        assert row["normalized_premium"] is None

    def test_e10_normalized_premium_value(self):
        """normalized_premium = premium / underlying_price."""
        row = _run_persist(_make_ev_dict(premium=500.0, underlying_price=100.0), vol=500, oi=1000)
        assert row["normalized_premium"] == pytest.approx(5.0, rel=1e-5)

    def test_e12_rearch010_regression_guard(self):
        """REARCH-010 purged columns must NOT appear in the buffered row dict."""
        row = _run_persist(_make_ev_dict(), vol=500, oi=1000)
        for purged in ("is_golden_sweep", "influence_tier", "conviction_score"):
            assert purged not in row, f"REARCH-010 purged column still present: {purged!r}"

    def test_bid_ask_class_consistent_with_is_ask_side(self):
        """bid_ask_class='ASK' iff is_ask_side=True; 'BID' iff is_ask_side=False."""
        row = _run_persist(_make_ev_dict(fill_price=5.00, bid=4.80, ask=5.00), vol=500, oi=1000)
        assert (row["bid_ask_class"] == "ASK") == row["is_ask_side"]
