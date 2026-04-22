"""
Parses raw Tradier options flow into a structured OptionsFlowEvent.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from parsers.bid_ask_classifier import classify_bid_ask, is_aggressive, TradeType
from parsers.trade_type_detector import detect_trade_type, is_golden_sweep

@dataclass
class OptionsFlowEvent:
    # Identity
    id:             str
    ticker:         str
    timestamp:      datetime

    # Contract
    contract_type:  str   # CALL | PUT
    strike:         float
    expiry:         str   # YYYY-MM-DD
    dte:            int

    # Trade
    fill_price:     float
    bid:            float
    ask:            float
    size:           int
    premium:        float

    # Derived
    trade_type:     str = ""         # SWEEP | BLOCK | SPLIT | SINGLE
    bid_ask_class:  str = ""         # ABOVE_ASK | AT_ASK | MID | AT_BID | BELOW_BID
    is_aggressive:  bool = False
    is_golden_sweep: bool = False

    # Classification (set later)
    sentiment:       str = "NEUTRAL" # BULLISH | BEARISH | NEUTRAL
    influence_tier:  str = "RETAIL"  # WHALE | INSTITUTIONAL | LARGE | RETAIL
    conviction_score: float = 0.0

    # Metadata
    exchange_count: int = 1
    fill_count:     int = 1
    open_interest:  int = 0
    iv:             float = 0.0
    underlying_price: float = 0.0


def parse_tradier_trade(raw: dict) -> Optional[OptionsFlowEvent]:
    """Parse a single Tradier stream trade dict into OptionsFlowEvent."""
    try:
        symbol    = raw.get("symbol", "")
        # Expect OCC format: e.g. AAPL  240119C00150000
        ticker    = raw.get("underlying", symbol.split(" ")[0])
        bid       = float(raw.get("bid",  0))
        ask       = float(raw.get("ask",  0))
        fill      = float(raw.get("price", (bid+ask)/2))
        size      = int(raw.get("size", 0))
        premium   = fill * size * 100

        ctype     = "CALL" if raw.get("option_type","C").upper() in ("C","CALL") else "PUT"
        strike    = float(raw.get("strike", 0))
        expiry    = raw.get("expiration_date", "")
        dte       = int(raw.get("dte", 0))

        exc_cnt   = int(raw.get("exchange_count", 1))
        fill_cnt  = int(raw.get("fill_count", 1))

        ba_class  = classify_bid_ask(fill, bid, ask)
        ttype     = detect_trade_type(size, premium, exc_cnt, fill_cnt)
        aggressive = is_aggressive(ba_class)
        golden     = is_golden_sweep(ttype, premium, aggressive)

        ev = OptionsFlowEvent(
            id             = raw.get("id", f"{ticker}_{expiry}_{strike}_{ctype}"),
            ticker         = ticker,
            timestamp      = datetime.fromisoformat(raw.get("timestamp", datetime.utcnow().isoformat())),
            contract_type  = ctype,
            strike         = strike,
            expiry         = expiry,
            dte            = dte,
            fill_price     = fill,
            bid            = bid,
            ask            = ask,
            size           = size,
            premium        = premium,
            trade_type     = ttype,
            bid_ask_class  = ba_class,
            is_aggressive  = aggressive,
            is_golden_sweep = golden,
            exchange_count = exc_cnt,
            fill_count     = fill_cnt,
            open_interest  = int(raw.get("open_interest", 0)),
            iv             = float(raw.get("greeks", {}).get("mid_iv", 0)),
            underlying_price = float(raw.get("underlying_price", 0)),
        )

        # Sentiment
        if ctype == "CALL" and aggressive:
            ev.sentiment = "BULLISH"
        elif ctype == "PUT" and aggressive:
            ev.sentiment = "BEARISH"

        # Influence tier
        if premium >= 2_000_000:
            ev.influence_tier = "WHALE"
        elif premium >= 500_000:
            ev.influence_tier = "INSTITUTIONAL"
        elif premium >= 100_000:
            ev.influence_tier = "LARGE"

        return ev
    except Exception:
        return None
