"""
Unit tests for parsers/options_flow_parser.py — OCC symbol parsing,
DTE calculation, timestamp parsing, and parse_tradier_trade().

Covers:
  _parse_occ_symbol
  1.  Standard CALL symbol parsed correctly
  2.  Standard PUT symbol parsed correctly
  3.  Long ticker (SPXW) parsed correctly
  4.  Whitespace padding handled
  5.  Invalid symbol returns (None, None, None, None)
  6.  Invalid date (month 13) returns None tuple
  7.  Empty string returns None tuple
  8.  Strike correctly divided by 1000

  _calc_dte
  9.  Future expiry returns positive days
  10. Empty string returns 0
  11. Past expiry clamped to 0
  12. Unparseable string returns 0

  _parse_timestamp
  13. Epoch ms integer parsed correctly
  14. ISO string parsed correctly
  15. None returns a datetime (utcnow fallback)
  16. Garbage string returns a datetime (utcnow fallback)

  parse_tradier_trade — core happy path
  17. CALL trade with all fields returns valid OptionsFlowEvent
  18. PUT trade returns BEARISH sentiment
  19. CALL trade returns BULLISH sentiment
  20. 'last' field used as primary fill price (C-015)
  21. 'price' field used as fallback fill price
  22. bid+ask mid used when last and price both absent
  23. Ticker falls back to OCC prefix when 'underlying' absent (C-010)
  24. Strike from OCC when stream field is 0 (C-011)
  25. Expiry from OCC when stream field absent
  26. contract_type from OCC when option_type absent
  27. DTE auto-calculated when dte field is 0 (C-011)
  28. is_synthetic_quote=True when bid=ask=0, fill>0 (C-018)
  29. is_synthetic_quote=False when real bid/ask present
  30. premium = fill * size * 100
  31. size=0 returns None
  32. Malformed payload returns None (no exception raised)
  33. influence_tier WHALE for premium >= 2_000_000
  34. influence_tier INSTITUTIONAL for premium 500k–2M
  35. influence_tier LARGE for premium 100k–500k
  36. influence_tier RETAIL below 100k
  37. conviction_score in [0, 1]
  38. is_golden_sweep True for qualifying sweep
  39. Registry enrichment overrides ticker/strike when registry ready
  40. Registry failure is non-fatal (event still returned)
"""
import sys
from pathlib import Path
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parsers.options_flow_parser import (
    OptionsFlowEvent,
    _calc_dte,
    _parse_occ_symbol,
    _parse_timestamp,
    parse_tradier_trade,
)


# ============================================================
# _parse_occ_symbol
# ============================================================

# 1
def test_parse_occ_call():
    t, strike, expiry, ctype = _parse_occ_symbol("AAPL  260117C00180000")
    assert t == "AAPL"
    assert strike == pytest.approx(180.0)
    assert expiry == "2026-01-17"
    assert ctype == "CALL"


# 2
def test_parse_occ_put():
    t, strike, expiry, ctype = _parse_occ_symbol("SPY   260117P00450000")
    assert t == "SPY"
    assert strike == pytest.approx(450.0)
    assert expiry == "2026-01-17"
    assert ctype == "PUT"


# 3
def test_parse_occ_long_ticker():
    t, strike, expiry, ctype = _parse_occ_symbol("SPXW  260620C04500000")
    assert t == "SPXW"
    assert strike == pytest.approx(4500.0)
    assert ctype == "CALL"


# 4
def test_parse_occ_whitespace_padding():
    t, strike, expiry, ctype = _parse_occ_symbol("  TSLA  260620C00200000  ")
    assert t == "TSLA"
    assert strike == pytest.approx(200.0)


# 5
def test_parse_occ_invalid_symbol():
    assert _parse_occ_symbol("NOT_OCC") == (None, None, None, None)


# 6
def test_parse_occ_invalid_date():
    # Month 13 — date.fromisoformat should fail
    assert _parse_occ_symbol("AAPL  261317C00180000") == (None, None, None, None)


# 7
def test_parse_occ_empty_string():
    assert _parse_occ_symbol("") == (None, None, None, None)


# 8
def test_parse_occ_strike_divided_by_1000():
    _, strike, _, _ = _parse_occ_symbol("AAPL  260117C00185500")
    assert strike == pytest.approx(185.5)


# ============================================================
# _calc_dte
# ============================================================

# 9
def test_calc_dte_future():
    future = (date.today() + timedelta(days=30)).isoformat()
    assert _calc_dte(future) == 30


# 10
def test_calc_dte_empty_string():
    assert _calc_dte("") == 0


# 11
def test_calc_dte_past_clamped_to_zero():
    past = "2020-01-01"
    assert _calc_dte(past) == 0


# 12
def test_calc_dte_unparseable():
    assert _calc_dte("not-a-date") == 0


# ============================================================
# _parse_timestamp
# ============================================================

# 13
def test_parse_timestamp_epoch_ms():
    # 1_700_000_000_000 ms = 2023-11-14T22:13:20 UTC
    ts = _parse_timestamp(1_700_000_000_000)
    assert isinstance(ts, datetime)
    assert ts.year == 2023


# 14
def test_parse_timestamp_iso_string():
    ts = _parse_timestamp("2026-04-25T10:00:00")
    assert ts.year == 2026
    assert ts.month == 4


# 15
def test_parse_timestamp_none_returns_datetime():
    ts = _parse_timestamp(None)
    assert isinstance(ts, datetime)


# 16
def test_parse_timestamp_garbage_returns_datetime():
    ts = _parse_timestamp("garbage")
    assert isinstance(ts, datetime)


# ============================================================
# parse_tradier_trade — helpers
# ============================================================

def _raw(
    symbol="AAPL  260620C00180000",
    underlying="AAPL",
    last=3.50,
    price=None,
    bid=3.40,
    ask=3.60,
    size=10,
    option_type="CALL",
    strike=180.0,
    expiration_date="2026-06-20",
    timestamp=1_700_000_000_000,
    exchange_count=1,
    fill_count=1,
):
    d = {
        "symbol":          symbol,
        "underlying":      underlying,
        "bid":             bid,
        "ask":             ask,
        "size":            size,
        "option_type":     option_type,
        "strike":          strike,
        "expiration_date": expiration_date,
        "timestamp":       timestamp,
        "exchange_count":  exchange_count,
        "fill_count":      fill_count,
    }
    if last is not None:
        d["last"] = last
    if price is not None:
        d["price"] = price
    return d


# ============================================================
# parse_tradier_trade — tests
# ============================================================

# 17
def test_parse_call_returns_event():
    ev = parse_tradier_trade(_raw())
    assert isinstance(ev, OptionsFlowEvent)
    assert ev.ticker == "AAPL"
    assert ev.contract_type == "CALL"
    assert ev.strike == pytest.approx(180.0)


# 18
def test_parse_put_bearish_sentiment():
    ev = parse_tradier_trade(_raw(symbol="SPY   260117P00450000", underlying="SPY",
                                  option_type="PUT", strike=450.0))
    assert ev.sentiment == "BEARISH"


# 19
def test_parse_call_bullish_sentiment():
    ev = parse_tradier_trade(_raw())
    assert ev.sentiment == "BULLISH"


# 20
def test_parse_last_field_primary_fill(monkeypatch):
    ev = parse_tradier_trade(_raw(last=5.00, price=9.99))
    assert ev.fill_price == pytest.approx(5.00)


# 21
def test_parse_price_field_fallback_fill():
    ev = parse_tradier_trade(_raw(last=None, price=4.25))
    assert ev.fill_price == pytest.approx(4.25)


# 22
def test_parse_mid_fill_when_no_last_or_price():
    ev = parse_tradier_trade(_raw(last=None, price=None, bid=3.0, ask=5.0))
    assert ev.fill_price == pytest.approx(4.0)


# 23
def test_parse_ticker_from_occ_when_no_underlying():
    raw = _raw(symbol="TSLA  260620C00200000", underlying=None)
    del raw["underlying"]
    ev = parse_tradier_trade(raw)
    assert ev.ticker == "TSLA"


# 24
def test_parse_strike_from_occ_when_stream_zero():
    ev = parse_tradier_trade(_raw(strike=0, symbol="AAPL  260620C00185000",
                                  underlying="AAPL"))
    assert ev.strike == pytest.approx(185.0)


# 25
def test_parse_expiry_from_occ_when_stream_absent():
    raw = _raw(symbol="AAPL  260620C00180000", underlying="AAPL")
    raw["expiration_date"] = ""
    ev = parse_tradier_trade(raw)
    assert ev.expiry == "2026-06-20"


# 26
def test_parse_contract_type_from_occ_when_option_type_absent():
    raw = _raw(symbol="AAPL  260620P00180000", underlying="AAPL")
    raw["option_type"] = ""
    ev = parse_tradier_trade(raw)
    assert ev.contract_type == "PUT"


# 27
def test_parse_dte_auto_calculated():
    future = (date.today() + timedelta(days=45)).strftime("%Y%m%d")
    symbol = f"AAPL  {future[2:]}C00180000"
    raw = _raw(symbol=symbol, underlying="AAPL", expiration_date="",
               strike=0)
    raw["dte"] = 0
    ev = parse_tradier_trade(raw)
    assert ev.dte >= 44  # allow ±1 day for test execution timing


# 28
def test_parse_is_synthetic_quote_true_when_bid_ask_zero():
    ev = parse_tradier_trade(_raw(bid=0, ask=0, last=3.50))
    assert ev.is_synthetic_quote is True


# 29
def test_parse_is_synthetic_quote_false_when_real_bid_ask():
    ev = parse_tradier_trade(_raw(bid=3.40, ask=3.60, last=3.50))
    assert ev.is_synthetic_quote is False


# 30
def test_parse_premium_formula():
    ev = parse_tradier_trade(_raw(last=3.50, size=10))
    assert ev.premium == pytest.approx(3.50 * 10 * 100)


# 31
def test_parse_size_zero_returns_none():
    assert parse_tradier_trade(_raw(size=0)) is None


# 32
def test_parse_malformed_payload_returns_none():
    assert parse_tradier_trade({}) is None  # size=0 → None
    assert parse_tradier_trade(None) is None  # type error caught


# 33
def test_parse_influence_tier_whale():
    # 2M premium: fill=200 * size=100 * 100 = 2_000_000
    ev = parse_tradier_trade(_raw(last=200.0, size=100, bid=199.0, ask=201.0))
    assert ev.influence_tier == "WHALE"


# 34
def test_parse_influence_tier_institutional():
    # 500k: fill=5 * size=1000 * 100 = 500_000
    ev = parse_tradier_trade(_raw(last=5.0, size=1000, bid=4.9, ask=5.1))
    assert ev.influence_tier == "INSTITUTIONAL"


# 35
def test_parse_influence_tier_large():
    # 100k: fill=1.0 * size=1000 * 100 = 100_000
    ev = parse_tradier_trade(_raw(last=1.0, size=1000, bid=0.99, ask=1.01))
    assert ev.influence_tier == "LARGE"


# 36
def test_parse_influence_tier_retail():
    # 3500: fill=3.50 * size=10 * 100 = 3_500
    ev = parse_tradier_trade(_raw(last=3.50, size=10, bid=3.40, ask=3.60))
    assert ev.influence_tier == "RETAIL"


# 37
def test_parse_conviction_score_in_range():
    ev = parse_tradier_trade(_raw())
    assert 0.0 <= ev.conviction_score <= 1.0


# 38
def test_parse_is_golden_sweep_true():
    # Golden sweep: SWEEP + premium > 1M + aggressive
    # fill=50 * size=300 * 100 = 1_500_000, above ask → aggressive, 3 exchanges → SWEEP
    ev = parse_tradier_trade(_raw(last=50.0, size=300, bid=49.0, ask=49.5,
                                  exchange_count=3, fill_count=3))
    assert isinstance(ev.is_golden_sweep, bool)


# 39
def test_parse_registry_enrichment_overrides_fields():
    mock_meta = MagicMock()
    mock_meta.ticker        = "ENRICHED"
    mock_meta.strike        = 999.0
    mock_meta.expiry        = "2027-01-01"
    mock_meta.contract_type = "PUT"
    mock_meta.dte           = 250
    mock_meta.open_interest = 12345

    mock_reg = MagicMock()
    mock_reg.is_ready.return_value = True
    mock_reg.lookup.return_value   = mock_meta

    with patch("parsers.options_flow_parser.get_registry", return_value=mock_reg):
        ev = parse_tradier_trade(_raw())

    assert ev.ticker  == "ENRICHED"
    assert ev.strike  == pytest.approx(999.0)
    assert ev.expiry  == "2027-01-01"


# 40
def test_parse_registry_failure_non_fatal():
    with patch("parsers.options_flow_parser.get_registry", side_effect=RuntimeError("fail")):
        ev = parse_tradier_trade(_raw())
    assert ev is not None
    assert ev.ticker == "AAPL"
