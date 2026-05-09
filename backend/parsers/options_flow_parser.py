"""
Parses raw Tradier options flow into a structured OptionsFlowEvent.

Architecture change (Layer 1):
  Registry enrichment — get_registry is imported at module level so
  patch('parsers.options_flow_parser.get_registry', ...) works in tests.

ING-002: Hard per-event $10k premium floor.
  parse_tradier_trade() returns the sentinel "below_premium" for events
  whose premium (fill * size * 100) is below the effective floor.
  The floor is resolved tier-aware by the caller (_process_trade in
  tradier_stream.py) and passed as min_premium kwarg. When min_premium
  is None (e.g. unit tests, cold-start), the module-level _MIN_EVENT_PREMIUM
  fallback applies (10_000). This maintains backwards compatibility with
  all existing callers and tests.
  Counter: _stats["below_min_premium"] owned by this module (gate owns counter).
  Exposed via get_stats() — visible in /health/stream through tradier_stream.
  Sampling log: every _BELOW_PREMIUM_SAMPLE_RATE-th drop emits a DEBUG line
  with ticker + premium so the composition of this gate is observable.

ING-004: Fallback underlying_price from registry stock_price.
  When Tradier omits underlying_price (returns 0.0), the registry
  stock_price(ticker) is used as a fallback. Fires inside the existing
  get_registry() enrichment block after meta enrichment so ev.ticker is
  already canonical. Only applied when registry is ready and sp > 0.
  Counter: _stats["underlying_price_fallback_applied"] owned by this module.

ING-006: Directional aggression classification.
  is_aggressive field on OptionsFlowEvent now set via
  is_directionally_aggressive(bid_ask_class, contract_type) instead of
  the deprecated is_aggressive(trade_type) shim.
  AT_BID/BELOW_BID on PUT or CALL now correctly returns True — seller
  writing at bid is conviction directional flow.
  IMPORTANT: is_aggressive is re-computed after registry enrichment block
  in case the registry overwrites contract_type (meta.contract_type).
  is_directionally_aggressive() depends on contract_type — if the registry
  flips PUT->CALL or CALL->PUT on an AT_BID/BELOW_BID fill, the
  pre-enrichment value would be stale.
  The re-computation runs in its own isolated try/except (separate from
  the registry enrichment try/except) so that a registry exception cannot
  silently skip the aggression update — deliberation F1 fix (2026-05-03).
  Synthetic quotes are exempt: is_aggressive is pinned False regardless
  of contract_type (no valid spread data to claim aggression on).
  is_aggressive is NOT yet persisted as a separate column in flow_events;
  that column is added in the ING-007 migration (SA-Q2 deliberation, 2026-05-03).

ING-010: Tier-aware min_premium floor.
  parse_tradier_trade() accepts an optional min_premium kwarg.
  When the caller supplies it (e.g. _process_trade resolves the ticker
  tier from the registry, then reads gate_config_store.get("min_premium", tier)),
  that value overrides _MIN_EVENT_PREMIUM for this tick only.
  Backwards compatible: None falls back to _MIN_EVENT_PREMIUM (10_000).

SENTIMENT FIX (2026-05-09):
  ev.sentiment is now derived from classify_sentiment(bid_ask_class, contract_type)
  instead of contract_type alone. The old logic (CALL->BULLISH, PUT->BEARISH)
  was correct for ASK-side fills (buyers initiating) but wrong for BID-side
  fills (sellers writing):
    - AT_BID/BELOW_BID CALL fill = call seller (bearish), was incorrectly BULLISH
    - AT_BID/BELOW_BID PUT  fill = put seller  (bullish), was incorrectly BEARISH
  Both the initial assignment and the registry enrichment block now use
  classify_sentiment() so that registry contract_type overrides don't
  silently revert to the old naive logic.
  All historical flow_events rows had order_side=UNKNOWN (Tradier stream
  never populated it), so bid_ask_class is the only available fill-placement
  signal and is the correct input to classify_sentiment().
"""
import logging
import re
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional, Union, Literal
from parsers.bid_ask_classifier import classify_bid_ask, classify_sentiment, is_directionally_aggressive
from parsers.trade_type_detector import detect_trade_type, is_golden_sweep

logger = logging.getLogger(__name__)

# Module-level import so tests can patch('parsers.options_flow_parser.get_registry', ...)
try:
    from services.symbol_registry import get_registry  # noqa: F401
except Exception:  # pragma: no cover
    def get_registry():  # type: ignore[misc]
        return None

# ---------------------------------------------------------------------------
# ING-002 / ING-010: Per-event premium floor.
# This is the module-level cold-start fallback. In production, _process_trade
# in tradier_stream.py resolves the tier-aware floor from gate_config_store and
# passes it as min_premium= to parse_tradier_trade(). This fallback is only
# used when min_premium= is not supplied (tests, standalone callers, cold-start
# before gate_config_store.load() has completed).
# ---------------------------------------------------------------------------
_MIN_EVENT_PREMIUM = 10_000

# ---------------------------------------------------------------------------
# ING-002: Sampling rate for the below_min_premium gate log.
# Every Nth drop emits a DEBUG line: [below_premium] TICKER prem=$X (floor=$Y)
# Set to 0 to disable sampling entirely (aggregate counter only).
# Tune upward to reduce log volume in high-throughput sessions.
# ---------------------------------------------------------------------------
_BELOW_PREMIUM_SAMPLE_RATE = 500

# ---------------------------------------------------------------------------
# Parser-level stats.
# This module owns exactly two counters:
#   below_min_premium                 — ING-002: clean filter drops at parser premium floor
#   underlying_price_fallback_applied — ING-004: ticks where registry stock_price used
#
# parse_failed is owned by tradier_stream — do NOT add it here.
# If get_stats() returned parse_failed, stats.update(get_parser_stats()) in
# tradier_stream.get_stats() would overwrite the stream's real parse_failed
# counter with 0 on every /health/stream call (F-1 fix, 2026-05-03).
# ---------------------------------------------------------------------------
_stats: dict = {
    "below_min_premium":                  0,  # ING-002: clean filter drops at parser premium floor
    "underlying_price_fallback_applied":  0,  # ING-004: ticks where registry stock_price used as fallback
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
    is_aggressive:  bool = False     # ING-006: set via is_directionally_aggressive(); re-computed post-registry
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


def parse_tradier_trade(
    raw: dict,
    min_premium: Optional[int] = None,
) -> Union[OptionsFlowEvent, Literal["below_premium"], None]:
    """Parse a single Tradier stream trade dict into OptionsFlowEvent.

    Args:
        raw:         Raw Tradier timesale tick dict.
        min_premium: ING-010 — tier-aware premium floor resolved by the caller.
                     When None, falls back to module-level _MIN_EVENT_PREMIUM (10_000).
                     Caller (_process_trade) resolves: registry.influence_tier(ticker)
                     -> gate_config_store.get("min_premium", tier_int) and passes result.

    Returns:
      OptionsFlowEvent  — valid event, passes all gates
      "below_premium"   — clean filter drop: premium < effective floor (ING-002/ING-010)
                          Caller must NOT increment parse_failed for this sentinel.
                          _stats["below_min_premium"] is incremented here, by the gate.
      None              — genuine parse error or size==0 guard triggered
    """
    # ING-010: resolve effective floor for this tick.
    # Caller supplies tier-resolved value; fallback to module constant when absent.
    effective_floor: int = min_premium if min_premium is not None else _MIN_EVENT_PREMIUM

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

        # ING-002 / ING-010: Tier-aware per-event premium floor.
        # Gate fires after size==0 guard, after premium is known,
        # before OCC parsing and OptionsFlowEvent construction.
        # Counter incremented here — gate owns its counter.
        # Sampling log: every _BELOW_PREMIUM_SAMPLE_RATE-th drop emits a DEBUG
        # line so the ticker composition of this gate is observable in logs.
        if premium < effective_floor:
            _stats["below_min_premium"] += 1
            if (
                _BELOW_PREMIUM_SAMPLE_RATE > 0
                and _stats["below_min_premium"] % _BELOW_PREMIUM_SAMPLE_RATE == 0
            ):
                sample_ticker = (
                    raw.get("underlying")
                    or (symbol.split()[0] if symbol else symbol)
                    or "UNKNOWN"
                )
                logger.debug(
                    "[below_premium] %s prem=$%s (floor=$%s tier_resolved=%s) sample=%d/total=%d",
                    sample_ticker,
                    f"{premium:,.0f}",
                    f"{effective_floor:,}",
                    min_premium is not None,
                    _BELOW_PREMIUM_SAMPLE_RATE,
                    _stats["below_min_premium"],
                )
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
        # NOTE (ING-006): is_aggressive stays False for synthetic quotes even
        # post-registry — synthetic spreads have no valid aggression signal.
        # NOTE (SENTIMENT FIX): synthetic quotes force ba_class="MID" which
        # means classify_sentiment() falls back to contract type — correct
        # behaviour since we have no fill placement signal.
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
            # ING-006: use directional aggression classifier (replaces is_aggressive shim)
            aggressive = is_directionally_aggressive(ba_class, ctype)

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

        # SENTIMENT FIX (2026-05-09): derive from bid_ask_class + contract_type.
        # ASK-side fills -> buyer initiating -> sentiment follows contract type.
        # BID-side fills -> seller writing  -> sentiment is INVERSE of contract type.
        # MID fills      -> ambiguous        -> fallback to contract type.
        ev.sentiment = classify_sentiment(ev.bid_ask_class, ev.contract_type)

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

        # ------------------------------------------------------------------
        # Registry enrichment block.
        # Guarded by its own try/except so registry errors degrade gracefully
        # without poisoning ev.is_aggressive (see F1 fix below).
        # ------------------------------------------------------------------
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
                    # SENTIMENT FIX: re-derive after registry may flip contract_type.
                    # bid_ask_class does not change here — only contract_type can flip.
                    ev.sentiment = classify_sentiment(ev.bid_ask_class, ev.contract_type)

                # ING-004: Fallback underlying_price from registry stock_price.
                if ev.underlying_price == 0.0:
                    sp = reg.stock_price(ev.ticker)
                    if sp > 0.0:
                        ev.underlying_price = sp
                        _stats["underlying_price_fallback_applied"] += 1
        except Exception:
            pass

        # ------------------------------------------------------------------
        # ING-006 (F1 fix): re-compute is_aggressive AFTER registry enrichment.
        # ------------------------------------------------------------------
        if not ev.is_synthetic_quote:
            try:
                ev.is_aggressive = is_directionally_aggressive(
                    ev.bid_ask_class, ev.contract_type
                )
            except Exception:
                pass

        return ev
    except Exception:
        return None
