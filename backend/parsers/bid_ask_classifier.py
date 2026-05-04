"""
Classifies a trade as ABOVE_ASK, AT_ASK, AT_BID, BELOW_BID, or MID
based on fill price relative to the bid/ask spread.

ING-006: Added is_directionally_aggressive() which replaces is_aggressive()
  in the parser hot path. The new function considers both bid_ask_class AND
  contract_type so that put/call selling at the bid is correctly identified
  as conviction directional flow — not passive.

  AT_ASK / ABOVE_ASK  -> True  (buyer paying up, unconditional)
  AT_BID / BELOW_BID  -> True  (seller writing at bid — conviction on both
                                 PUT and CALL sides per ING-001 resolution)
  MID                 -> False (passive / ambiguous)

  is_aggressive(trade_type) is retained as a deprecated shim.
  Do not remove until all callers are audited (ING-006 AC).

SA-F2 / ING-007 NOTE:
  is_aggressive on OptionsFlowEvent is NOT yet persisted as a separate column
  in flow_events. The column must be added before this branch ships to production
  otherwise historical rows will be missing is_aggressive data and S8 backtest
  stratification will be degraded (same argument as execution_mechanic — Session
  21 deliberation). ING-007 story must be created and S2.5 migration extended
  before production deploy.
"""
from typing import Literal

TradeType = Literal["ABOVE_ASK","AT_ASK","MID","AT_BID","BELOW_BID"]


def classify_bid_ask(fill: float, bid: float, ask: float) -> TradeType:
    """Return trade aggressiveness classification."""
    if ask <= bid:
        return "MID"
    tenth = (ask - bid) * 0.1
    if fill >= ask + tenth:
        return "ABOVE_ASK"
    if fill >= ask - tenth:
        return "AT_ASK"
    if fill <= bid - tenth:
        return "BELOW_BID"
    if fill <= bid + tenth:
        return "AT_BID"
    return "MID"


def is_directionally_aggressive(bid_ask_class: str, contract_type: str) -> bool:
    """
    ING-006: Directional aggression classification.

    Replaces is_aggressive(trade_type) in the parser hot path.
    Considers bid_ask_class AND contract_type per ING-001 resolution:

      AT_ASK / ABOVE_ASK  -> True  unconditionally (buyer paying up)
      AT_BID / BELOW_BID  -> True  on PUT: put seller writing at bid
                                          = conviction bullish positioning
                             True  on CALL: call seller writing at bid
                                          = conviction bearish positioning
      MID                 -> False (passive / ambiguous)

    No size threshold here — ING-002 $10k per-event floor is the correct
    upstream guard. By the time this runs the event has already cleared $10k
    (deliberation SA-Q1, 2026-05-03).

    TEMPORARY LOCATION: This function belongs in order_side_classifier.py
    (S2 scope). Migration tracked in GitHub Issue filed 2026-05-03 and
    SPRINT_WSJ_INGESTION_ALIGNMENT.md ING-006 SA-F1 resolution row.
    Do not add TODO comments here — the issue is the tracking mechanism
    (Rule 6 Constraint 3, SA-F1 deliberation fix 2026-05-03).
    """
    ba    = (bid_ask_class or "").strip().upper()
    ctype = (contract_type or "").strip().upper()
    if ba in ("AT_ASK", "ABOVE_ASK"):
        return True
    if ba in ("AT_BID", "BELOW_BID") and ctype in ("PUT", "CALL"):
        return True
    return False


def is_aggressive(trade_type: TradeType) -> bool:
    """Deprecated — use is_directionally_aggressive(bid_ask_class, contract_type).

    Retained as backward-compat shim for any callers not yet migrated.
    Do not use for new code.
    """
    return trade_type in ("ABOVE_ASK", "AT_ASK")
