"""
test_rearch003_event_quality_tags.py

REARCH-003: Event quality tag columns on flow_events.

Test matrix (QA deliberation 2026-05-10):
  E-1:  fill at ask          -> is_ask_side=True,  bid_ask_class='ASK'
  E-2:  fill at bid          -> is_ask_side=False, bid_ask_class='BID'
  E-3:  fill at mid          -> is_ask_side=False, bid_ask_class='MID'
  E-4:  vol/OI >= 0.5        -> vol_oi_signal='HIGH'
  E-5:  vol/OI < 0.5         -> vol_oi_signal='NORMAL'
  E-6:  OI = 0               -> vol_oi_signal='UNKNOWN'
  E-7:  chain_store miss      -> vol_oi_signal='UNKNOWN', event NOT dropped
  E-8:  underlying_price=0   -> normalized_premium=None
  E-9:  underlying_price=None-> normalized_premium=None
  E-10: valid inputs          -> correct normalized_premium ratio
  E-11: all four tag fields present in buffered row dict
  E-12: REARCH-010 regression guard (dropped columns absent from row)

All tests are purely unit-level -- no Supabase connectivity required.
The _flow_event_buffer is patched to capture the row dict without issuing
any HTTP calls.
"""
import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers --- import directly for unit tests (no Supabase env needed)
# ---------------------------------------------------------------------------
import importlib
import services.flow_store as fs


# ---------------------------------------------------------------------------
# Unit tests for pure helpers
# ---------------------------------------------------------------------------

class TestClassifyBidAsk:
    """E-1, E-2, E-3"""

    def test_e1_ask_side(self):
        cls, is_ask = fs._classify_bid_ask(fill_price=5.00, bid=4.80, ask=5.00)
        assert cls == "ASK"
        assert is_ask is True

    def test_e1_ask_side_98pct_threshold(self):
        """fill at exactly 98% of ask should still be ASK / is_ask_side=True"""
        ask = 5.00
        fill = ask * 0.98
        cls, is_ask = fs._classify_bid_ask(fill_price=fill, bid=4.70, ask=ask)
        assert cls == "ASK"
        assert is_ask is True

    def test_e2_bid_side(self):
        cls, is_ask = fs._classify_bid_ask(fill_price=4.80, bid=4.80, ask=5.00)
        assert cls == "BID"
        assert is_ask is False

    def test_e2_bid_side_102pct_threshold(self):
        """fill at exactly 102% of bid should still be BID"""
        bid = 4.80
        fill = bid * 1.02
        cls, is_ask = fs._classify_bid_ask(fill_price=fill, bid=bid, ask=5.20)
        assert cls == "BID"
        assert is_ask is False

    def test_e3_mid(self):
        cls, is_ask = fs._classify_bid_ask(fill_price=4.90, bid=4.80, ask=5.00)
        assert cls == "MID"
        assert is_ask is False

    def test_ask_none_returns_mid(self):
        cls, is_ask = fs._classify_bid_ask(fill_price=5.00, bid=4.80, ask=None)
        assert cls == "MID"
        assert is_ask is False

    def test_ask_zero_returns_mid(self):
        cls, is_ask = fs._classify_bid_ask(fill_price=5.00, bid=4.80, ask=0.0)
        assert cls == "MID"
        assert is_ask is False

    def test_fill_none_treated_as_zero(self):
        """None fill_price -> 0.0 -> BID side (below bid * 1.02)"""
        cls, is_ask = fs._classify_bid_ask(fill_price=None, bid=4.80, ask=5.00)
        assert cls == "BID"
        assert is_ask is False


class TestComputeVolOiSignal:
    """E-4, E-5, E-6"""

    def test_e4_high(self):
        assert fs._compute_vol_oi_signal(volume=1000, oi=2000) == "HIGH"  # 0.5 >= 0.5

    def test_e4_high_above_threshold(self):
        assert fs._compute_vol_oi_signal(volume=800, oi=1000) == "HIGH"  # 0.8

    def test_e5_normal(self):
        assert fs._compute_vol_oi_signal(volume=400, oi=1000) == "NORMAL"  # 0.4

    def test_e6_oi_zero(self):
        assert fs._compute_vol_oi_signal(volume=500, oi=0) == "UNKNOWN"

    def test_volume_none(self):
        assert fs._compute_vol_oi_signal(volume=None, oi=1000) == "UNKNOWN"

    def test_oi_none(self):
        assert fs._compute_vol_oi_signal(volume=500, oi=None) == "UNKNOWN"

    def test_both_none(self):
        assert fs._compute_vol_oi_signal(volume=None, oi=None) == "UNKNOWN"

    def test_custom_threshold(self):
        """Caller can pass a custom threshold without patching the module."""
        assert fs._compute_vol_oi_signal(volume=300, oi=1000, threshold=0.25) == "HIGH"
        assert fs._compute_vol_oi_signal(volume=200, oi=1000, threshold=0.25) == "NORMAL"


class TestComputeNormalizedPremium:
    """E-8, E-9, E-10"""

    def test_e8_underlying_zero(self):
        assert fs._compute_normalized_premium(premium=500.0, underlying_price=0.0) is None

    def test_e9_underlying_none(self):
        assert fs._compute_normalized_premium(premium=500.0, underlying_price=None) is None

    def test_e10_valid(self):
        result = fs._compute_normalized_premium(premium=500.0, underlying_price=100.0)
        assert result == 5.0

    def test_e10_rounds_to_4dp(self):
        result = fs._compute_normalized_premium(premium=1.0, underlying_price=3.0)
        assert result == round(1.0 / 3.0, 4)

    def test_premium_none(self):
        assert fs._compute_normalized_premium(premium=None, underlying_price=100.0) is None


# ---------------------------------------------------------------------------
# Integration tests: persist_flow_event() row dict assembly
# ---------------------------------------------------------------------------

def _make_ev_dict(
    fill_price=5.00,
    bid=4.80,
    ask=5.00,
    premium=500.0,
    underlying_price=100.0,
    ticker="AAPL",
    occ_symbol="AAPL260117C00250000",
    **kwargs,
) -> dict:
    base = {
        "ticker":           ticker,
        "occ_symbol":       occ_symbol,
        "contract_type":    "CALL",
        "strike":           250.0,
        "expiry":           "2026-01-17",
        "dte":              5,
        "fill_price":       fill_price,
        "bid":              bid,
        "ask":              ask,
        "size":             10,
        "premium":          premium,
        "underlying_price": underlying_price,
        "trade_type":       "BTO",
        "bid_ask_class":    "ASK",   # incoming field -- will be overwritten by helper
        "is_aggressive":    True,
        "sentiment":        "BULLISH",
        "exchange_count":   1,
        "fill_count":       1,
        "open_interest":    5000,
        "iv":               0.35,
        "is_synthetic_quote": False,
    }
    base.update(kwargs)
    return base


@pytest.fixture(autouse=True)
def patch_supabase_env(monkeypatch):
    """Ensure _is_configured() returns True without real credentials."""
    monkeypatch.setenv("SUPABASE_URL", "https://fake.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "fake-service-role-key")
    # Reload module-level env vars picked up at import time
    import importlib
    importlib.reload(fs)
    yield


def _run_persist(ev_dict: dict, vol: int = None, oi: int = None):
    """
    Run persist_flow_event() and return the row dict that was appended
    to _flow_event_buffer. Chain-store vol/OI is injected via mock.
    """
    captured_rows = []

    original_append = list.append

    def capture_append(self, item):
        captured_rows.append(item)
        original_append(self, item)

    vol_oi_return = (vol, oi)

    with patch("services.flow_store._flow_event_buffer", []) as buf, \
         patch("services.flow_store.get_contract_vol_oi", return_value=vol_oi_return, create=True):
        # patch chain_store import inside the function
        with patch.dict("sys.modules", {
            "services.chain_store": MagicMock(get_contract_vol_oi=lambda _: vol_oi_return)
        }):
            # patch _insert_rows_with_retry so we never hit HTTP
            with patch("services.flow_store._insert_rows_with_retry", new=AsyncMock(return_value=True)):
                # force buffer to capture without triggering early flush
                fs._flow_event_buffer = []
                asyncio.get_event_loop().run_until_complete(fs.persist_flow_event(ev_dict))
                rows = list(fs._flow_event_buffer)
                fs._flow_event_buffer = []
    return rows[0] if rows else None


class TestPersistFlowEventTagIntegration:
    """E-7, E-11, E-12"""

    def test_e11_all_tag_fields_present(self):
        """Row dict must contain all four REARCH-003 tag fields."""
        row = _run_persist(_make_ev_dict(), vol=500, oi=1000)
        assert row is not None
        assert "is_ask_side"        in row
        assert "bid_ask_class"      in row
        assert "vol_oi_signal"      in row
        assert "normalized_premium" in row

    def test_e11_ask_side_true_in_row(self):
        row = _run_persist(_make_ev_dict(fill_price=5.00, bid=4.80, ask=5.00), vol=500, oi=1000)
        assert row["is_ask_side"] is True
        assert row["bid_ask_class"] == "ASK"

    def test_e11_bid_side_in_row(self):
        row = _run_persist(_make_ev_dict(fill_price=4.80, bid=4.80, ask=5.20), vol=500, oi=1000)
        assert row["is_ask_side"] is False
        assert row["bid_ask_class"] == "BID"

    def test_e11_vol_oi_high_in_row(self):
        row = _run_persist(_make_ev_dict(), vol=600, oi=1000)  # 0.6 >= 0.5
        assert row["vol_oi_signal"] == "HIGH"

    def test_e11_vol_oi_normal_in_row(self):
        row = _run_persist(_make_ev_dict(), vol=300, oi=1000)  # 0.3 < 0.5
        assert row["vol_oi_signal"] == "NORMAL"

    def test_e7_chain_store_miss_unknown(self):
        """Cache miss (None, None) must produce UNKNOWN, not drop the event."""
        row = _run_persist(_make_ev_dict(), vol=None, oi=None)
        assert row is not None, "Event must not be dropped on cache miss"
        assert row["vol_oi_signal"] == "UNKNOWN"

    def test_e8_underlying_zero_normalized_none(self):
        row = _run_persist(_make_ev_dict(underlying_price=0.0), vol=500, oi=1000)
        assert row["normalized_premium"] is None

    def test_e9_underlying_none_normalized_none(self):
        ev = _make_ev_dict()
        ev["underlying_price"] = None
        row = _run_persist(ev, vol=500, oi=1000)
        assert row["normalized_premium"] is None

    def test_e10_normalized_premium_value(self):
        row = _run_persist(_make_ev_dict(premium=500.0, underlying_price=100.0), vol=500, oi=1000)
        assert row["normalized_premium"] == 5.0

    def test_e12_rearch010_regression_guard(self):
        """REARCH-010 dropped columns must NEVER appear in the row dict."""
        row = _run_persist(_make_ev_dict(), vol=500, oi=1000)
        dropped = {"is_golden_sweep", "influence_tier", "conviction_score"}
        assert set(row.keys()).isdisjoint(dropped), (
            f"REARCH-010 regression: dropped columns found in row: "
            f"{set(row.keys()) & dropped}"
        )

    def test_bid_ask_class_consistent_with_is_ask_side(self):
        """
        bid_ask_class and is_ask_side must always be derived from the same
        _classify_bid_ask() call -- they cannot disagree.
        """
        # ASK case
        row = _run_persist(_make_ev_dict(fill_price=5.00, bid=4.80, ask=5.00), vol=500, oi=1000)
        assert (row["bid_ask_class"] == "ASK") == row["is_ask_side"]

        # BID case
        row = _run_persist(_make_ev_dict(fill_price=4.80, bid=4.80, ask=5.20), vol=500, oi=1000)
        assert row["bid_ask_class"] == "BID"
        assert row["is_ask_side"] is False

        # MID case
        row = _run_persist(_make_ev_dict(fill_price=5.00, bid=4.80, ask=5.20), vol=500, oi=1000)
        assert row["bid_ask_class"] == "MID"
        assert row["is_ask_side"] is False
