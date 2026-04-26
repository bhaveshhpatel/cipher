"""
Parses raw Tradier options flow into a structured OptionsFlowEvent.

Architecture change (Layer 1):
  Registry enrichment — get_registry is imported at module level so
  patch('parsers.options_flow_parser.get_registry', ...) works in tests.
"""
import re
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional
from parsers.bid_ask_classifier import classify_bid_ask, is_aggressive
from parsers.trade_type_detector import detect_trade_type, is_golden_sweep

# Module-level import so tests can patch('parsers.options_flow_parser.get_registry', ...)
try:
    from services.symbol_registry import get_registry  # noqa: F401
except Exception:  # pragma: no cover
    def get_registry():  # type: ignore[misc]
        return None


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
    exchange_count:    int = 1
    fill_count:        int = 1
    open_interest:     int = 0
    iv:                float = 0.0
    underlying_price:  float = 0.0

    # Data quality flag
    is_synthetic_quote: bool = False


_OCC_RE = re.compile(
    r"^([A-Z]{1,10})\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$"
)


def _parse_occ_symbol(symbol: str):
    m = _OCC_RE.match(symbol.strip())
    if not m:
        return None, None, None, None
    ticker_raw, yy, mm, dd, cp, strike_raw = m.groups()
    try:
        expiry = f"20{yy}-{mm}-{dd}"
        date.fromisoformat(expiry)
        strike = int(strike_raw) / 1000.0
        contract_type = "CALL" if cp == "C" else "PUT"
        return ticker_raw.strip(), strike, expiry, contract_type
    except (ValueError, OverflowError):
        return None, None, None, None


def _calc_dte(expiry: str) -> int:
    if not expiry:
        return 0
    try:
        exp_date = date.fromisoformat(expiry)
        delta = (exp_date - date.today()).days
        return max(delta, 0)
    except (ValueError, OverflowError):
        return 0


def _parse_timestamp(ts) -> datetime:
    if ts is None:
        return datetime.utcnow()
    try:
        if isinstance(ts, (int, float)):
            return datetime.utcfromtimestamp(ts / 1000.0)
        return datetime.fromisoformat(str(ts))
    except (ValueError, OSError, OverflowError, TypeError):
        return datetime.utcnow()


def parse_tradier_trade(raw: dict) -> Optional[OptionsFlowEvent]:
    """Parse a single Tradier stream trade dict into OptionsFlowEvent."""
    try:
        symbol = raw.get("symbol", "")

        bid  = float(raw.get("bid",  0) or 0)
        ask  = float(raw.get("ask",  0) or 0)

        fill = float(
            raw.get("last") or
            raw.get("price") or
            ((bid + ask) / 2 if (bid + ask) > 0 else 0)
        )

        size = int(raw.get("size") or 0)

        if size == 0:
            return None

        premium = fill * size * 100

        ctype_raw = raw.get("option_type", "") or ""
        strike    = float(raw.get("strike", 0) or 0)
        expiry    = raw.get("expiration_date", "") or ""

        occ_ticker, occ_strike, occ_expiry, occ_ctype = _parse_occ_symbol(symbol)

        ticker = (
            raw.get("underlying")
            or occ_ticker
            or (symbol.split()[0] if symbol.split() else symbol)
        )

        if strike == 0 and occ_strike is not None:
            strike = occ_strike
        if not expiry and occ_expiry is not None:
            expiry = occ_expiry
        if not ctype_raw and occ_ctype is not None:
            ctype_raw = occ_ctype

        ctype = "CALL" if ctype_raw.upper() in ("C", "CALL") else "PUT"

        dte = int(raw.get("dte", 0) or 0)
        if dte == 0 and expiry:
            dte = _calc_dte(expiry)

        exc_cnt  = int(raw.get("exchange_count", 1) or 1)
        fill_cnt = int(raw.get("fill_count", 1) or 1)

        is_synthetic_quote = False
        effective_bid = bid
        effective_ask = ask
        if effective_bid == 0 and effective_ask == 0 and fill > 0:
            effective_bid = round(fill * 0.995, 4)
            effective_ask = round(fill * 1.005, 4)
            is_synthetic_quote = True

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
            is_synthetic_quote = is_synthetic_quote,
        )

        if ctype == "CALL":
            ev.sentiment = "BULLISH"
        elif ctype == "PUT":
            ev.sentiment = "BEARISH"

        if premium >= 2_000_000:
            ev.influence_tier = "WHALE"
        elif premium >= 500_000:
            ev.influence_tier = "INSTITUTIONAL"
        elif premium >= 100_000:
            ev.influence_tier = "LARGE"

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

        try:
            reg = get_registry()
            if reg and reg.is_ready():
                meta = reg.lookup(symbol)
                if meta:
                    ev.ticker        = meta.ticker
                    ev.strike        = meta.strike
                    ev.expiry        = meta.expiry
                    ev.contract_type = meta.contract_type
                    ev.dte           = meta.dte
                    ev.open_interest = meta.open_interest
                    ev.sentiment     = "BULLISH" if meta.contract_type == "CALL" else "BEARISH"
        except Exception:
            pass

        return ev
    except Exception:
        return None
