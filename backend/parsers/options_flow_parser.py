"""
Parses raw Tradier options flow into a structured OptionsFlowEvent.
"""
import re
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional
from parsers.bid_ask_classifier import classify_bid_ask, is_aggressive
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


# OCC symbol pattern: AAPL  240119C00150000
# Ticker (up to 6 chars, left-padded with spaces), 6-digit date YYMMDD,
# C or P, 8-digit strike (price * 1000, zero-padded)
_OCC_RE = re.compile(
    r"^([A-Z]{1,6})\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$"
)


def _parse_occ_symbol(symbol: str):
    """
    Parse OCC option symbol into (strike: float, expiry: str, contract_type: str).
    Returns (None, None, None) if the symbol does not match OCC format.

    Example: 'AAPL  260117C00180000'
      → strike=180.0, expiry='2026-01-17', contract_type='CALL'
    """
    m = _OCC_RE.match(symbol.strip())
    if not m:
        return None, None, None
    _, yy, mm, dd, cp, strike_raw = m.groups()
    try:
        expiry = f"20{yy}-{mm}-{dd}"
        # Validate it's a real date
        date.fromisoformat(expiry)
        strike = int(strike_raw) / 1000.0
        contract_type = "CALL" if cp == "C" else "PUT"
        return strike, expiry, contract_type
    except (ValueError, OverflowError):
        return None, None, None


def parse_tradier_trade(raw: dict) -> Optional[OptionsFlowEvent]:
    """Parse a single Tradier stream trade dict into OptionsFlowEvent."""
    try:
        symbol    = raw.get("symbol", "")
        # Expect OCC format: e.g. AAPL  240119C00150000
        ticker    = raw.get("underlying", symbol.split(" ")[0])
        bid       = float(raw.get("bid",  0))
        ask       = float(raw.get("ask",  0))
        fill      = float(raw.get("price", (bid+ask)/2))
        size      = int(raw.get("size") or 0)  # Phase 3: guard missing/null size

        # Skip zero-size events — no valid premium can be derived
        if size == 0:
            return None

        premium   = fill * size * 100

        # -- Primary: use top-level fields from Tradier payload
        ctype_raw = raw.get("option_type", "")
        strike    = float(raw.get("strike", 0))
        expiry    = raw.get("expiration_date", "")

        # -- Fallback: parse from OCC symbol when fields are absent/zero
        # Tradier streaming trade events often omit strike/expiration_date
        # at the top level — they are embedded in the OCC symbol string.
        if (not ctype_raw or strike == 0 or not expiry) and symbol:
            occ_strike, occ_expiry, occ_ctype = _parse_occ_symbol(symbol)
            if strike == 0 and occ_strike is not None:
                strike = occ_strike
            if not expiry and occ_expiry is not None:
                expiry = occ_expiry
            if not ctype_raw and occ_ctype is not None:
                ctype_raw = occ_ctype

        ctype  = "CALL" if ctype_raw.upper() in ("C", "CALL") else "PUT"
        dte    = int(raw.get("dte", 0))

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
