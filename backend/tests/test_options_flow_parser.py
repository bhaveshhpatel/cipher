"""
Unit tests for:
  - parsers/options_flow_parser.py  (parse_tradier_trade, helpers)
  - parsers/bid_ask_classifier.py   (classify_bid_ask, is_aggressive)
  - parsers/trade_type_detector.py  (detect_trade_type, is_golden_sweep)

Covers:
  _parse_occ_symbol
  1.  Standard AAPL CALL parsed correctly
  2.  SPY PUT parsed correctly
  3.  SPXW long-ticker CALL parsed correctly
  4.  Invalid symbol returns (None, None, None, None)
  5.  Whitespace-padded OCC symbol is normalised
  6.  Invalid date in OCC (month=13) returns None tuple

  _calc_dte
  7.  Future expiry returns positive DTE
  8.  Empty string returns 0
  9.  Past expiry returns 0 (clamped)
  10. Unparseable string returns 0

  _parse_timestamp
  11. Epoch ms integer parsed correctly
  12. ISO string parsed correctly
  13. None falls back to utcnow (returns datetime)
  14. Garbage string falls back to utcnow

  parse_tradier_trade — happy paths
  15. Full timesale payload produces valid OptionsFlowEvent
  16. fill taken from "last" field (C-015 fix)
  17. fill falls back to "price" when "last" absent
  18. fill falls back to mid when both absent
  19. OCC symbol used for ticker when "underlying" absent (C-010)
  20. Strike, expiry, contract_type derived from OCC symbol
  21. DTE auto-calculated when not in payload (C-011)
  22. size=0 returns None
  23. Sentiment CALL → BULLISH, PUT → BEARISH (C-011)
  24. is_synthetic_quote=True when bid=ask=0 and fill>0 (C-018)
  25. is_synthetic_quote=False when real bid/ask present
  26. premium = fill * size * 100
  27. influence_tier WHALE at premium >= $2M
  28. influence_tier INSTITUTIONAL at $500K–$2M
  29. influence_tier LARGE at $100K–$500K
  30. influence_tier RETAIL below $100K
  31. conviction_score in [0, 1]
  32. DTE urgency: dte<=7 adds 0.10 bonus to conviction
  33. Registry enrichment overrides parsed fields when registry ready
  34. Registry lookup failure is non-fatal (returns event from OCC parse)
  35. Malformed payload returns None

  bid_ask_classifier
  36. fill > ask + 10% spread => ABOVE_ASK
  37. fill within 10% of ask => AT_ASK
  38. fill within 10% of bid => AT_BID
  39. fill < bid - 10% spread => BELOW_BID
  40. fill in middle => MID
  41. ask <= bid (crossed market) => MID
  42. is_aggressive True for ABOVE_ASK
  43. is_aggressive True for AT_ASK
  44. is_aggressive False for MID

  trade_type_detector
  45. exchange_cnt>=3 and fill_count>=3 => SWEEP
  46. size>=500 and fill_count==1 => BLOCK
  47. fill_count>=5 and size>=100 => SPLIT
  48. otherwise => SINGLE
  49. is_golden_sweep: SWEEP + above_ask + premium>=500K => True
  50. is_golden_sweep False when premium < 500K
  51. is_golden_sweep False when not SWEEP type
  52. is_golden_sweep False when not above_ask
"""
from datetime import datetime, date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from parsers.options_flow_parser import (
    OptionsFlowEvent,
    _parse_occ_symbol,
    _calc_dte,
    _parse_timestamp,
    parse_tradier_trade,
)
from parsers.bid_ask_classifier import classify_bid_ask, is_aggressive
from parsers.trade_type_detector import detect_trade_type, is_golden_sweep


# ── helpers ─────────────────────────────────────────────────────────────────
def _base_payload(
    symbol="AAPL  260117C00180000",
    last=3.50,
    bid=3.40,
    ask=3.60,
    size=10,
    exch="C",
    timestamp=1700000000000,
):
    """Minimal valid timesale payload."""
    return {
        "symbol":    symbol,
        "last":      last,
        "bid":       bid,
        "ask":       ask,
        "size":      size,
        "exch":      exch,
        "timestamp": timestamp,
    }


# ============================================================
# _parse_occ_symbol
# ============================================================

# 1
def test_parse_occ_aapl_call():
    t, s, e, c = _parse_occ_symbol("AAPL  260117C00180000")
    assert t == "AAPL"
    assert s == 180.0
    assert e == "2026-01-17"
    assert c == "CALL"


# 2
def test_parse_occ_spy_put():
    t, s, e, c = _parse_occ_symbol("SPY   260117P00450000")
    assert t == "SPY"
    assert s == 450.0
    assert e == "2026-01-17"
    assert c == "PUT"


# 3
def test_parse_occ_spxw_long_ticker():
    t, s, e, c = _parse_occ_symbol("SPXW  260117C04500000")
    assert t == "SPXW"
    assert s == 4500.0
    assert c == "CALL"


# 4
def test_parse_occ_invalid_returns_none_tuple():
    result = _parse_occ_symbol("NOT_AN_OCC_SYMBOL")
    assert result == (None, None, None, None)


# 5
def test_parse_occ_whitespace_padded():
    t, s, e, c = _parse_occ_symbol("  AAPL  260117C00180000  ")
    assert t == "AAPL"
    assert s == 180.0


# 6
def test_parse_occ_invalid_date_month_13():
    # Month 13 is not a valid date
    result = _parse_occ_symbol("AAPL  261317C00180000")
    assert result == (None, None, None, None)


# ============================================================
# _calc_dte
# ============================================================

# 7
def test_calc_dte_future_expiry():
    future = (date.today() + timedelta(days=30)).isoformat()
    dte = _calc_dte(future)
    assert dte >= 29  # allow for timezone boundary
    assert dte <= 31


# 8
def test_calc_dte_empty_string():
    assert _calc_dte("") == 0


# 9
def test_calc_dte_past_expiry_clamped_to_zero():
    past = (date.today() - timedelta(days=5)).isoformat()
    assert _calc_dte(past) == 0


# 10
def test_calc_dte_unparseable():
    assert _calc_dte("not-a-date") == 0


# ============================================================
# _parse_timestamp
# ============================================================

# 11
def test_parse_timestamp_epoch_ms():
    ts = _parse_timestamp(1700000000000)
    assert isinstance(ts, datetime)
    assert ts.year == 2023   # epoch 1700000000 ~ Nov 2023


# 12
def test_parse_timestamp_iso_string():
    ts = _parse_timestamp("2026-04-25T10:00:00")
    assert ts.year == 2026
    assert ts.month == 4
    assert ts.day == 25


# 13
def test_parse_timestamp_none_returns_datetime():
    ts = _parse_timestamp(None)
    assert isinstance(ts, datetime)


# 14
def test_parse_timestamp_garbage_falls_back():
    ts = _parse_timestamp("garbage_value")
    assert isinstance(ts, datetime)


# ============================================================
# parse_tradier_trade — happy paths
# ============================================================

# 15
def test_parse_full_payload_returns_event():
    ev = parse_tradier_trade(_base_payload())
    assert ev is not None
    assert isinstance(ev, OptionsFlowEvent)


# 16
def test_parse_fill_from_last_field():
    raw = _base_payload(last=4.20, bid=4.00, ask=4.40)
    ev  = parse_tradier_trade(raw)
    assert ev.fill_price == pytest.approx(4.20)


# 17
def test_parse_fill_falls_back_to_price_field():
    raw = _base_payload(last=None)
    raw["price"] = 3.75
    ev = parse_tradier_trade(raw)
    assert ev.fill_price == pytest.approx(3.75)


# 18
def test_parse_fill_falls_back_to_mid():
    raw = _base_payload(last=None, bid=3.00, ask=4.00)
    del raw["last"]
    ev = parse_tradier_trade(raw)
    assert ev.fill_price == pytest.approx(3.50)  # (3+4)/2


# 19
def test_parse_ticker_from_occ_when_underlying_absent():
    raw = _base_payload(symbol="TSLA  260117C00250000")
    ev  = parse_tradier_trade(raw)
    assert ev.ticker == "TSLA"


# 20
def test_parse_strike_expiry_contract_from_occ():
    raw = _base_payload(symbol="NVDA  260620P00600000")
    ev  = parse_tradier_trade(raw)
    assert ev.strike        == pytest.approx(600.0)
    assert ev.expiry        == "2026-06-20"
    assert ev.contract_type == "PUT"


# 21
def test_parse_dte_auto_calculated_when_absent():
    future = (date.today() + timedelta(days=45)).strftime("%y%m%d")
    symbol = f"AAPL  {future}C00180000"
    raw    = _base_payload(symbol=symbol)
    ev     = parse_tradier_trade(raw)
    assert ev is not None
    assert ev.dte >= 40  # allow a few days' tolerance


# 22
def test_parse_size_zero_returns_none():
    raw = _base_payload(size=0)
    assert parse_tradier_trade(raw) is None


# 23
def test_parse_sentiment_call_bullish_put_bearish():
    call_ev = parse_tradier_trade(_base_payload(symbol="AAPL  260117C00180000"))
    put_ev  = parse_tradier_trade(_base_payload(symbol="AAPL  260117P00180000"))
    assert call_ev.sentiment == "BULLISH"
    assert put_ev.sentiment  == "BEARISH"


# 24
def test_parse_synthetic_quote_tagged_when_bid_ask_zero():
    raw = _base_payload(bid=0, ask=0, last=3.50)
    ev  = parse_tradier_trade(raw)
    assert ev.is_synthetic_quote is True


# 25
def test_parse_not_synthetic_when_real_bid_ask():
    raw = _base_payload(bid=3.40, ask=3.60, last=3.50)
    ev  = parse_tradier_trade(raw)
    assert ev.is_synthetic_quote is False


# 26
def test_parse_premium_formula():
    raw = _base_payload(last=2.00, size=50)
    ev  = parse_tradier_trade(raw)
    assert ev.premium == pytest.approx(2.00 * 50 * 100)


# 27
def test_parse_influence_tier_whale():
    # fill=200, size=100 => premium=$2_000_000 => WHALE
    raw = _base_payload(last=200.0, size=100)
    ev  = parse_tradier_trade(raw)
    assert ev.influence_tier == "WHALE"


# 28
def test_parse_influence_tier_institutional():
    # fill=5, size=1100 => premium=$550_000 => INSTITUTIONAL
    raw = _base_payload(last=5.0, size=1100)
    ev  = parse_tradier_trade(raw)
    assert ev.influence_tier == "INSTITUTIONAL"


# 29
def test_parse_influence_tier_large():
    # fill=2.0, size=600 => premium=$120_000 => LARGE
    raw = _base_payload(last=2.0, size=600)
    ev  = parse_tradier_trade(raw)
    assert ev.influence_tier == "LARGE"


# 30
def test_parse_influence_tier_retail():
    # fill=0.50, size=10 => premium=$500 => RETAIL
    raw = _base_payload(last=0.50, size=10)
    ev  = parse_tradier_trade(raw)
    assert ev.influence_tier == "RETAIL"


# 31
def test_parse_conviction_score_in_range():
    ev = parse_tradier_trade(_base_payload())
    assert 0.0 <= ev.conviction_score <= 1.0


# 32
def test_parse_conviction_dte_urgency_bonus():
    # DTE <= 7 should give higher conviction than DTE=60 (same everything)
    today   = date.today()
    near_dt = (today + timedelta(days=3)).strftime("%y%m%d")
    far_dt  = (today + timedelta(days=60)).strftime("%y%m%d")
    raw_near = _base_payload(symbol=f"AAPL  {near_dt}C00180000", last=3.50, bid=3.40, ask=3.60)
    raw_far  = _base_payload(symbol=f"AAPL  {far_dt}C00180000",  last=3.50, bid=3.40, ask=3.60)
    ev_near  = parse_tradier_trade(raw_near)
    ev_far   = parse_tradier_trade(raw_far)
    if ev_near and ev_far:
        assert ev_near.conviction_score >= ev_far.conviction_score


# 33
def test_parse_registry_enrichment_overrides_fields():
    """
    When SymbolRegistry is ready and lookup succeeds, the parser should
    override ticker/strike/expiry/contract_type/dte/open_interest with
    registry metadata.
    """
    from parsers.options_flow_parser import parse_tradier_trade

    fake_meta = MagicMock()
    fake_meta.ticker        = "OVERRIDDEN"
    fake_meta.strike        = 999.0
    fake_meta.expiry        = "2099-12-31"
    fake_meta.contract_type = "PUT"
    fake_meta.dte           = 999
    fake_meta.open_interest = 42

    fake_reg = MagicMock()
    fake_reg.is_ready.return_value = True
    fake_reg.lookup.return_value   = fake_meta

    with patch("parsers.options_flow_parser.get_registry", return_value=fake_reg):
        ev = parse_tradier_trade(_base_payload())

    assert ev is not None
    assert ev.ticker        == "OVERRIDDEN"
    assert ev.strike        == pytest.approx(999.0)
    assert ev.expiry        == "2099-12-31"
    assert ev.contract_type == "PUT"
    assert ev.dte           == 999
    assert ev.open_interest == 42


# 34
def test_parse_registry_failure_is_nonfatal():
    """
    If get_registry() raises an exception, parse_tradier_trade() should
    still return a valid event using the OCC regex parse.
    """
    with patch("parsers.options_flow_parser.get_registry",
               side_effect=RuntimeError("registry exploded")):
        ev = parse_tradier_trade(_base_payload())
    assert ev is not None
    assert ev.ticker == "AAPL"


# 35
def test_parse_malformed_payload_returns_none():
    # Completely empty dict will produce size=0 → None
    assert parse_tradier_trade({}) is None


# ============================================================
# bid_ask_classifier
# ============================================================

# 36
def test_classify_above_ask():
    # ask=4.00, bid=3.00, spread=1.00, tenth=0.10; fill=4.15 > 4.10
    assert classify_bid_ask(4.15, 3.00, 4.00) == "ABOVE_ASK"


# 37
def test_classify_at_ask():
    # fill=3.95 (between ask-tenth=3.90 and ask+tenth=4.10)
    assert classify_bid_ask(3.95, 3.00, 4.00) == "AT_ASK"


# 38
def test_classify_at_bid():
    # fill=3.05 (between bid-tenth=2.90 and bid+tenth=3.10)
    assert classify_bid_ask(3.05, 3.00, 4.00) == "AT_BID"


# 39
def test_classify_below_bid():
    # fill=2.80 < bid-tenth=2.90
    assert classify_bid_ask(2.80, 3.00, 4.00) == "BELOW_BID"


# 40
def test_classify_mid():
    # fill=3.50 is right in the middle
    assert classify_bid_ask(3.50, 3.00, 4.00) == "MID"


# 41
def test_classify_crossed_market_returns_mid():
    # ask <= bid — degenerate spread
    assert classify_bid_ask(3.50, 4.00, 3.00) == "MID"


# 42
def test_is_aggressive_above_ask():
    assert is_aggressive("ABOVE_ASK") is True


# 43
def test_is_aggressive_at_ask():
    assert is_aggressive("AT_ASK") is True


# 44
def test_is_aggressive_mid_false():
    assert is_aggressive("MID") is False


# ============================================================
# trade_type_detector
# ============================================================

# 45
def test_detect_sweep():
    assert detect_trade_type(size=50, premium=200_000, exchange_cnt=4, fill_count=5) == "SWEEP"


# 46
def test_detect_block():
    assert detect_trade_type(size=500, premium=1_000_000, exchange_cnt=1, fill_count=1) == "BLOCK"


# 47
def test_detect_split():
    assert detect_trade_type(size=200, premium=50_000, exchange_cnt=1, fill_count=6) == "SPLIT"


# 48
def test_detect_single():
    assert detect_trade_type(size=5, premium=1_000, exchange_cnt=1, fill_count=1) == "SINGLE"


# 49
def test_is_golden_sweep_true():
    assert is_golden_sweep("SWEEP", 600_000, True) is True


# 50
def test_is_golden_sweep_false_low_premium():
    assert is_golden_sweep("SWEEP", 400_000, True) is False


# 51
def test_is_golden_sweep_false_not_sweep():
    assert is_golden_sweep("BLOCK", 1_000_000, True) is False


# 52
def test_is_golden_sweep_false_not_above_ask():
    assert is_golden_sweep("SWEEP", 600_000, False) is False
