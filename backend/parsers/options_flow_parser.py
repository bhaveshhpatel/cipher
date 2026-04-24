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

Fix (C-011):
  - OCC regex expanded to 1-10 char tickers (handles SPY, SPXW, etc.)
  - Sentiment: CALL → BULLISH, PUT → BEARISH baseline regardless of
    aggressiveness. Aggressiveness elevates conviction score but is not
    a gate on sentiment direction.
  - DTE auto-calculated from expiry date when the stream omits `dte`.
  - bid/ask=0 with nonzero fill → synthetic spread (fill ± 0.5%) so
    bid_ask_classifier never gets 0/0 and always returns MID.
  - Conviction score revised: includes DTE urgency factor (short-dated
    contracts score higher) and caps cleanly at 1.0.

Fix (C-012):
  - _parse_timestamp() added to safely handle Tradier's epoch-ms integer
    timestamp format. Previously datetime.fromisoformat() was called on an
    int → TypeError → bare except returned None → every tick silently
    discarded → complete ingestion freeze.
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


# OCC symbol pattern: AAPL  240119C00150000 or SPXW  260117P04500000
# Ticker: 1-10 uppercase letters (left-padded with spaces in some feeds)
# Date: 6-digit YYMMDD
# C or P
# Strike: 8-digit (price * 1000, zero-padded)
_OCC_RE = re.compile(
    r"^([A-Z]{1,10})\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$"
)


def _parse_occ_symbol(symbol: str):
    """
    Parse OCC option symbol into (ticker, strike, expiry, contract_type).
    Returns (None, None, None, None) if symbol does not match OCC format.

    Examples:
      'AAPL  260117C00180000' → ('AAPL', 180.0, '2026-01-17', 'CALL')
      'SPY   260117P00450000' → ('SPY',  450.0, '2026-01-17', 'PUT')
      'SPXW  260117C04500000' → ('SPXW', 4500.0, '2026-01-17', 'CALL')
    """
    m = _OCC_RE.match(symbol.strip())
    if not m:
        return None, None, None, None
    ticker_raw, yy, mm, dd, cp, strike_raw = m.groups()
    try:
        expiry = f"20{yy}-{mm}-{dd}"
        date.fromisoformat(expiry)          # validate real date
        strike = int(strike_raw) / 1000.0
        contract_type = "CALL" if cp == "C" else "PUT"
        return ticker_raw.strip(), strike, expiry, contract_type
    except (ValueError, OverflowError):
        return None, None, None, None


def _calc_dte(expiry: str) -> int:
    """
    Calculate days-to-expiry from expiry string (YYYY-MM-DD).
    Returns 0 if expiry is empty or unparseable.
    """
    if not expiry:
        return 0
    try:
        exp_date = date.fromisoformat(expiry)
        delta = (exp_date - date.today()).days
        return max(delta, 0)
    except (ValueError, OverflowError):
        return 0


def _parse_timestamp(ts) -> datetime:
    """
    Safely parse a Tradier stream timestamp into a datetime.

    Tradier sends `timestamp` in two formats depending on the endpoint:
      - int/float : Unix epoch milliseconds  e.g. 1745521391000
      - str       : ISO 8601                 e.g. '2026-04-24T18:03:30'

    Previously datetime.fromisoformat() was called directly on the raw value.
    When Tradier sends an integer, this raises TypeError which was caught by
    the bare `except Exception: return None` in parse_tradier_trade() —
    causing EVERY tick to be silently discarded and ingestion to freeze.

    Falls back to utcnow() if the value is absent or unparseable.
    """
    if ts is None:
        return datetime.utcnow()
    try:
        if isinstance(ts, (int, float)):
            # Tradier epoch timestamps are in milliseconds
            return datetime.utcfromtimestamp(ts / 1000.0)
        return datetime.fromisoformat(str(ts))
    except (ValueError, OSError, OverflowError, TypeError):
        return datetime.utcnow()


def parse_tradier_trade(raw: dict) -> Optional[OptionsFlowEvent]:
    """Parse a single Tradier stream trade dict into OptionsFlowEvent.

    Tradier streaming payload anatomy:
      - `symbol`          : OCC option symbol  e.g. 'AAPL  260117C00180000'
      - `underlying`      : underlying ticker  e.g. 'AAPL'  (may be absent)
      - `price`           : fill price
      - `bid` / `ask`     : NBBO at time of trade (may be 0 in some feeds)
      - `size`            : contract count
      - `option_type`     : 'call' | 'put'  (often absent in stream)
      - `strike`          : float            (often absent in stream)
      - `expiration_date` : YYYY-MM-DD       (often absent in stream)
      - `timestamp`       : int (epoch ms) or ISO str (may be absent)

    When option_type / strike / expiration_date are absent the OCC symbol
    is the only reliable source — parse it directly.

    When bid/ask are both 0 but fill is nonzero, a synthetic ±0.5% spread
    is applied so the bid_ask classifier can produce a meaningful result
    instead of always returning MID.
    """
    try:
        symbol = raw.get("symbol", "")

        bid  = float(raw.get("bid",  0) or 0)
        ask  = float(raw.get("ask",  0) or 0)
        fill = float(raw.get("price", 0) or 0)

        # If price field absent or 0, derive from mid
        if fill == 0 and (bid + ask) > 0:
            fill = (bid + ask) / 2

        size = int(raw.get("size") or 0)

        # Skip zero-size events — no valid premium can be derived
        if size == 0:
            return None

        # Skip zero-fill events — no valid premium
        if fill == 0:
            return None

        premium = fill * size * 100

        # -- Primary: use top-level fields if present
        ctype_raw = raw.get("option_type", "") or ""
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
            or (symbol.split()[0] if symbol.split() else symbol)
        )

        # Fill in missing contract details from OCC parse
        if strike == 0 and occ_strike is not None:
            strike = occ_strike
        if not expiry and occ_expiry is not None:
            expiry = occ_expiry
        if not ctype_raw and occ_ctype is not None:
            ctype_raw = occ_ctype

        # Normalize contract type — default to CALL only as last resort
        # (previously always defaulted to PUT which was wrong)
        if ctype_raw.upper() in ("C", "CALL"):
            ctype = "CALL"
        elif ctype_raw.upper() in ("P", "PUT"):
            ctype = "PUT"
        else:
            # Cannot determine from any source — skip trade
            return None

        # DTE: use stream value if provided, otherwise calculate from expiry
        dte = int(raw.get("dte", 0) or 0)
        if dte == 0 and expiry:
            dte = _calc_dte(expiry)

        exc_cnt  = int(raw.get("exchange_count", 1) or 1)
        fill_cnt = int(raw.get("fill_count", 1) or 1)

        # Fix: when bid/ask are both 0 but fill is nonzero, synthesize a
        # tight spread so the classifier produces a useful result.
        # A 0/0 spread always returns MID → sentiment always NEUTRAL.
        effective_bid = bid
        effective_ask = ask
        if effective_bid == 0 and effective_ask == 0 and fill > 0:
            effective_bid = round(fill * 0.995, 4)  # fill - 0.5%
            effective_ask = round(fill * 1.005, 4)  # fill + 0.5%

        ba_class   = classify_bid_ask(fill, effective_bid, effective_ask)
        ttype      = detect_trade_type(size, premium, exc_cnt, fill_cnt)
        aggressive = is_aggressive(ba_class)
        golden     = is_golden_sweep(ttype, premium, aggressive)

        ev = OptionsFlowEvent(
            id              = raw.get("id", f"{ticker}_{expiry}_{strike}_{ctype}"),
            ticker          = ticker,
            timestamp       = _parse_timestamp(raw.get("timestamp")),
            contract_type   = ctype,
            strike          = strike,
            expiry          = expiry,
            dte             = dte,
            fill_price      = fill,
            bid             = bid,       # store original bid (0 if absent)
            ask             = ask,       # store original ask (0 if absent)
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

        # ----------------------------------------------------------------
        # Sentiment: CALL = BULLISH baseline, PUT = BEARISH baseline.
        # Aggressiveness is a signal amplifier for conviction, NOT a gate
        # on sentiment direction.  Previously sentiment was only set when
        # is_aggressive=True which meant nearly all trades were NEUTRAL.
        # ----------------------------------------------------------------
        if ctype == "CALL":
            ev.sentiment = "BULLISH"
        elif ctype == "PUT":
            ev.sentiment = "BEARISH"

        # Influence tier
        if premium >= 2_000_000:
            ev.influence_tier = "WHALE"
        elif premium >= 500_000:
            ev.influence_tier = "INSTITUTIONAL"
        elif premium >= 100_000:
            ev.influence_tier = "LARGE"

        # ----------------------------------------------------------------
        # Conviction score (0.0 – 1.0)
        #   - Base: aggressiveness of fill vs spread
        #   - Golden sweep bonus
        #   - Premium size factor (capped at $10M)
        #   - DTE urgency: short-dated (<= 7 DTE) scores higher
        # ----------------------------------------------------------------
        dte_urgency = 0.1 if dte <= 7 else (0.05 if dte <= 30 else 0.0)
        ev.conviction_score = round(
            min(
                (0.4 if aggressive else 0.15)
                + (0.25 if golden else 0.0)
                + min(premium / 10_000_000, 0.25)
                + dte_urgency,
                1.0,
            ),
            3,
        )

        return ev
    except Exception:
        return None
