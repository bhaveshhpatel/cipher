"""
Parses raw Tradier options flow into a structured OptionsFlowEvent.

Architecture change (Layer 1):
  Registry enrichment — get_registry is imported at module level so
  patch('parsers.options_flow_parser.get_registry', ...) works in tests.

ING-002: Hard per-event $10k premium floor.
  parse_tradier_trade() returns the sentinel "below_premium" for events
  whose premium (fill * size * 100) is below _MIN_EVENT_PREMIUM.
  This is a clean data-quality drop — not a parse error.
  Counter: _stats["below_min_premium"] owned by this module (gate owns counter).
  Exposed via get_stats() — visible in /health/stream through tradier_stream.
  Future: wire through ingestion_config key "min_event_premium" with
  10_000 as hardcoded cold-start fallback (ING-002-CONFIG story).
"""
import re
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional, Union, Literal
from parsers.bid_ask_classifier import classify_bid_ask, is_aggressive
from parsers.trade_type_detector import detect_trade_type, is_golden_sweep

# Module-level import so tests can patch('parsers.options_flow_parser.get_registry', ...)
try:
    from services.symbol_registry import get_registry  # noqa: F401
except Exception:  # pragma: no cover
    def get_registry():  # type: ignore[misc]
        return None

# ---------------------------------------------------------------------------
# ING-002: Hard per-event premium floor.
# Hardcoded safe default — active at import time, no DB dependency, no cold-start gap.
# Future: wire through ingestion_config key "min_event_premium" with this as
# fallback when admin config page is built (ING-002-CONFIG).
# ---------------------------------------------------------------------------
_MIN_EVENT_PREMIUM = 10_000

# ---------------------------------------------------------------------------
# Parser-level stats.
# Gate owns its counter — below_min_premium increments here, inside
# parse_tradier_trade(), before the sentinel is returned.
# Exposed via get_stats() so /health/stream can surface it.
# ---------------------------------------------------------------------------
_stats: dict = {
    "below_min_premium": 0,  # ING-002: clean filter drops at parser premium floor ($10k)
    "parse_failed":      0,  # genuine parse errors (bad data, exception, size==0)
}


def get_stats() -> dict:
    """Return a snapshot of parser-level stats."""
    return dict(_stats)


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


def parse_tradier_trade(raw: dict) -> Union[OptionsFlowEvent, Literal["below_premium"], None]:
    """Parse a single Tradier stream trade dict into OptionsFlowEvent.

    Returns:
      OptionsFlowEvent  — valid event, passes all gates
      "below_premium"   — clean filter drop: premium < _MIN_EVENT_PREMIUM (ING-002)
                          Caller must NOT increment parse_failed for this sentinel.
                          _stats["below_min_premium"] is incremented here, by the gate.
      None              — genuine parse error or size==0 guard triggered
    """
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

        # ING-002: Hard per-event premium floor.
        # Gate fires after size==0 guard, after premium is known,
        # before OCC parsing and OptionsFlowEvent construction.
        # Counter incremented here — gate owns its counter.
        if premium < _MIN_EVENT_PREMIUM:
            _stats["below_min_premium"] += 1
            return "below_premium"

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

        # ------------------------------------------------------------------
        # Synthetic quote handling
        # When bid=ask=0 we have no real spread data. Instead of fabricating
        # a tight ±0.5% spread and running classification (which almost
        # always produces ABOVE_ASK/AT_ASK -> is_aggressive=True on
        # wide-spread contracts), we:
        #   1. Flag is_synthetic_quote=True
        #   2. Force bid_ask_class="MID" — neutral, unknown fill placement
        #   3. Force is_aggressive=False — cannot claim aggression without quotes
        #   4. Apply a 40% conviction haircut (×0.6) downstream
        # ------------------------------------------------------------------
        is_synthetic_quote = False
        effective_bid = bid
        effective_ask = ask

        if effective_bid == 0 and effective_ask == 0 and fill > 0:
            effective_bid = round(fill * 0.995, 4)
            effective_ask = round(fill * 1.005, 4)
            is_synthetic_quote = True
            ba_class   = "MID"   # force neutral, skip classify_bid_ask
            aggressive = False   # cannot claim aggression without real quotes
        else:
            ba_class   = classify_bid_ask(fill, effective_bid, effective_ask)
            aggressive = is_aggressive(ba_class)

        ttype  = detect_trade_type(size, premium, exc_cnt, fill_cnt)
        golden = is_golden_sweep(ttype, premium, aggressive)

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
        raw_conviction = round(
            min(
                (0.4 if aggressive else 0.15)
                + (0.25 if golden else 0.0)
                + min(premium / 10_000_000, 0.25)
                + dte_urgency,
                1.0,
            ),
            3,
        )

        # 40% conviction haircut for synthetic quotes — prevents wide-spread
        # contracts with unknown fill placement from scoring as high-conviction
        # events solely on premium size.
        if is_synthetic_quote:
            ev.conviction_score = round(raw_conviction * 0.6, 3)
        else:
            ev.conviction_score = raw_conviction

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
