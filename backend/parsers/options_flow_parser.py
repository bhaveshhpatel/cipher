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
    aggressiveness.
  - DTE auto-calculated from expiry date when the stream omits `dte`.
  - bid/ask=0 with nonzero fill → synthetic spread (fill ± 0.5%) so
    bid_ask_classifier never gets 0/0.
  - Conviction score revised: includes DTE urgency factor.

Fix (C-012):
  - _parse_timestamp() handles Tradier epoch-ms integer timestamps.

Fix (C-014):
  - Reverted regressions introduced in 59becaee:
    * Removed `if fill == 0: return None` — fill=0 is handled gracefully
      by deriving from mid price, not by dropping the trade.
    * Removed hard `return None` for unknown ctype — defaults to PUT
      as last resort (same as original working code).

Fix (C-015):
  - fill field: use "last" first, then "price" as fallback.
    Tradier timesale events send the fill price in the "last" field.
    "price" is kept as a fallback for compatibility with any legacy path.

Architecture change (Layer 1):
  - Registry enrichment: after OCC regex parse, lookup the symbol in the
    SymbolRegistry (if built). If found, override ticker/strike/expiry/
    contract_type/dte/open_interest with pre-validated chain data.
    This ensures 100% accurate metadata even if the OCC regex fails on
    unusual symbol formats.

Fix (C-018) — Synthetic Quote Tagging:
  - Added `is_synthetic_quote: bool` field to OptionsFlowEvent dataclass.
  - Set to True when bid=ask=0 and fill > 0 (synthetic spread was applied).
  - Passed through to flow_store → flow_events.is_synthetic_quote column.
  - Rows with is_synthetic_quote=True have unreliable bid_ask_class and
    is_aggressive values — exclude them from backtesting aggression metrics.
"""
import re
from dataclasses import dataclass, field
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
    exchange_count:    int = 1
    fill_count:        int = 1
    open_interest:     int = 0
    iv:                float = 0.0
    underlying_price:  float = 0.0

    # Data quality flag — True when bid=ask=0 and spread was synthesized from fill.
    # Rows with this flag set have unreliable bid_ask_class / is_aggressive values.
    # Exclude from backtesting aggression and net-premium calculations.
    is_synthetic_quote: bool = False


# OCC symbol pattern: AAPL  240119C00150000 or SPXW  260117P04500000
# Expanded to 1-10 char tickers to handle SPY, SPXW, etc.
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
    Safely parse a Tradier stream timestamp.
    Handles int/float (epoch ms), ISO string, or missing (falls back to utcnow).
    """
    if ts is None:
        return datetime.utcnow()
    try:
        if isinstance(ts, (int, float)):
            return datetime.utcfromtimestamp(ts / 1000.0)
        return datetime.fromisoformat(str(ts))
    except (ValueError, OSError, OverflowError, TypeError):
        return datetime.utcnow()


def parse_tradier_trade(raw: dict) -> Optional[OptionsFlowEvent]:
    """Parse a single Tradier stream trade dict into OptionsFlowEvent.

    Tradier streaming payload anatomy (filter=timesale):
      - `symbol`          : OCC option symbol  e.g. 'AAPL  260117C00180000'
      - `underlying`      : underlying ticker  e.g. 'AAPL'  (may be absent)
      - `last`            : fill price  ← PRIMARY field for timesale events
      - `price`           : fill price  ← FALLBACK (some legacy feed formats)
      - `bid` / `ask`     : NBBO at time of trade (may be 0 in some feeds)
      - `size`            : contract count
      - `option_type`     : 'call' | 'put'  (often absent in stream)
      - `strike`          : float            (often absent in stream)
      - `expiration_date` : YYYY-MM-DD       (often absent in stream)
      - `timestamp`       : int (epoch ms) or ISO str

    When option_type / strike / expiration_date are absent the OCC symbol
    is the only reliable source — parse it directly.

    Fill price priority: "last" → "price" → mid(bid, ask) → 0
    """
    try:
        symbol = raw.get("symbol", "")

        bid  = float(raw.get("bid",  0) or 0)
        ask  = float(raw.get("ask",  0) or 0)

        # FIX: Tradier timesale sends fill price in "last" field, not "price".
        # Fall back to "price" for compatibility, then mid, then 0.
        fill = float(
            raw.get("last") or
            raw.get("price") or
            ((bid + ask) / 2 if (bid + ask) > 0 else 0)
        )

        size = int(raw.get("size") or 0)

        # Only skip genuinely zero-size events
        if size == 0:
            return None

        premium = fill * size * 100

        # -- Primary: use top-level fields if present
        ctype_raw = raw.get("option_type", "") or ""
        strike    = float(raw.get("strike", 0) or 0)
        expiry    = raw.get("expiration_date", "") or ""

        # -- Always attempt OCC parse on `symbol` field
        occ_ticker, occ_strike, occ_expiry, occ_ctype = _parse_occ_symbol(symbol)

        # Ticker: prefer explicit `underlying` field, fall back to OCC prefix
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

        # Normalize contract type — default to PUT as last resort
        ctype = "CALL" if ctype_raw.upper() in ("C", "CALL") else "PUT"

        # DTE: use stream value if provided, otherwise calculate from expiry
        dte = int(raw.get("dte", 0) or 0)
        if dte == 0 and expiry:
            dte = _calc_dte(expiry)

        exc_cnt  = int(raw.get("exchange_count", 1) or 1)
        fill_cnt = int(raw.get("fill_count", 1) or 1)

        # Synthetic spread when bid/ask both 0 but fill is nonzero.
        # Tag the event so downstream backtesting can exclude these rows
        # from aggression and net-premium calculations.
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

        # Sentiment: CALL = BULLISH baseline, PUT = BEARISH baseline
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

        # Conviction score
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

        # Registry enrichment — override parsed fields with pre-validated chain metadata.
        # If the OCC SymbolRegistry is built, use it as ground truth for ticker/strike/
        # expiry/contract_type/dte/open_interest. Falls back gracefully to OCC regex
        # parse above if registry is not yet available.
        try:
            from services.symbol_registry import get_registry
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
            pass  # registry not yet built — OCC regex parse above is sufficient

        return ev
    except Exception:
        return None
