"""
Parses raw Tradier options flow into a structured OptionsFlowEvent.

Key fix (C-010):
  Tradier streaming trade events send the OCC option symbol in the `symbol`
  field (e.g. 'AAPL  260117C00180000') and the underlying ticker in a
  separate `underlying` field. Previously the parser treated `symbol` as the
  underlying ticker, so the OCC fallback never fired → strike=0, expiry="",
  contract_type always defaulted to PUT.

  Now: if `underlying` is present use it as ticker; otherwise strip the
  alphabetic prefix from the OCC symbol. The OCC fallback path is always
  attempted on the `symbol` field directly.
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
    expiry:         str   # YYYY-MM-DD or "" if unparseable
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
    Parse OCC option symbol into (ticker: str, strike: float, expiry: str, contract_type: str).
    Returns (None, None, None, None) if the symbol does not match OCC format.

    Example: 'AAPL  260117C00180000'
      → ticker='AAPL', strike=180.0, expiry='2026-01-17', contract_type='CALL'
    """
    m = _OCC_RE.match(symbol.strip())
    if not m:
        return None, None, None, None
    ticker_raw, yy, mm, dd, cp, strike_raw = m.groups()
    try:
        expiry = f"20{yy}-{mm}-{dd}"
        # Validate it's a real date
        date.fromisoformat(expiry)
        strike = int(strike_raw) / 1000.0
        contract_type = "CALL" if cp == "C" else "PUT"
        return ticker_raw.strip(), strike, expiry, contract_type
    except (ValueError, OverflowError):
        return None, None, None, None


def parse_tradier_trade(raw: dict) -> Optional[OptionsFlowEvent]:
    """Parse a single Tradier stream trade dict into OptionsFlowEvent.

    Tradier streaming payload anatomy:
      - `symbol`     : OCC option symbol  e.g. 'AAPL  260117C00180000'
      - `underlying` : underlying ticker  e.g. 'AAPL'  (may be absent)
      - `price`      : fill price
      - `bid` / `ask`: NBBO at time of trade
      - `size`       : contract count
      - `option_type`: 'call' | 'put'  (often absent in stream)
      - `strike`     : float           (often absent in stream)
      - `expiration_date`: YYYY-MM-DD  (often absent in stream)

    When option_type / strike / expiration_date are absent the OCC symbol
    is the only reliable source — parse it directly.
    """
    try:
        symbol = raw.get("symbol", "")

        bid  = float(raw.get("bid",  0))
        ask  = float(raw.get("ask",  0))
        fill = float(raw.get("price", (bid + ask) / 2 if (bid + ask) > 0 else 0))
        size = int(raw.get("size") or 0)

        # Skip zero-size events — no valid premium can be derived
        if size == 0:
            return None

        premium = fill * size * 100

        # -- Primary: use top-level fields if present
        ctype_raw = raw.get("option_type", "")
        strike    = float(raw.get("strike", 0) or 0)
        expiry    = raw.get("expiration_date", "") or ""

        # -- Always attempt OCC parse on `symbol` field.
        #    Tradier streams the full OCC contract string in `symbol`
        #    (e.g. 'AAPL  260117C00180000'), NOT the underlying ticker.
        #    `underlying` is a separate field that carries the ticker.
        occ_ticker, occ_strike, occ_expiry, occ_ctype = _parse_occ_symbol(symbol)

        # Ticker: prefer explicit `underlying` field, fall back to OCC prefix,
        # last resort split on whitespace from symbol string.
        ticker = (
            raw.get("underlying")
            or occ_ticker
            or symbol.split()[0]
            or symbol
        )

        if strike == 0 and occ_strike is not None:
            strike = occ_strike
        if not expiry and occ_expiry is not None:
            expiry = occ_expiry
        if not ctype_raw and occ_ctype is not None:
            ctype_raw = occ_ctype

        ctype = "CALL" if ctype_raw.upper() in ("C", "CALL") else "PUT"
        dte   = int(raw.get("dte", 0) or 0)

        exc_cnt  = int(raw.get("exchange_count", 1) or 1)
        fill_cnt = int(raw.get("fill_count", 1) or 1)

        ba_class   = classify_bid_ask(fill, bid, ask)
        ttype      = detect_trade_type(size, premium, exc_cnt, fill_cnt)
        aggressive = is_aggressive(ba_class)
        golden     = is_golden_sweep(ttype, premium, aggressive)

        ev = OptionsFlowEvent(
            id              = raw.get("id", f"{ticker}_{expiry}_{strike}_{ctype}"),
            ticker          = ticker,
            timestamp       = datetime.fromisoformat(
                                raw.get("timestamp", datetime.utcnow().isoformat())
                              ),
            contract_type   = ctype,
            strike          = strike,
            expiry          = expiry,
            dte             = dte,
            fill_price      = fill,
            bid             = bid,
            ask             = ask,
            size            = size,
            premium         = premium,
            trade_type      = ttype,
            bid_ask_class   = ba_class,
            is_aggressive   = aggressive,
            is_golden_sweep = golden,
            exchange_count  = exc_cnt,
            fill_count      = fill_cnt,
            open_interest   = int(raw.get("open_interest", 0) or 0),
            iv              = float((raw.get("greeks") or {}).get("mid_iv", 0) or 0),
            underlying_price = float(raw.get("underlying_price", 0) or 0),
        )

        # Sentiment: aggressive CALL = BULLISH, aggressive PUT = BEARISH
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

        # Conviction score: simple proxy until ML model is wired
        ev.conviction_score = round(
            (0.4 if aggressive else 0.1)
            + (0.3 if golden else 0.0)
            + min(premium / 10_000_000, 0.3),
            3,
        )

        return ev
    except Exception:
        return None
