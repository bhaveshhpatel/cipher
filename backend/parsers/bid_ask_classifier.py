"""
Classifies a trade as ABOVE_ASK, AT_ASK, AT_BID, BELOW_BID, or MID
based on fill price relative to the bid/ask spread.

ING-006: Added is_directionally_aggressive() which replaces is_aggressive()
  in the parser hot path. The new function considers both bid_ask_class AND
  contract_type so that put/call selling at the bid is correctly identified
  as conviction directional flow — not passive.

  Aggression classification per ING-001 resolution
  (SPRINT_WSJ_INGESTION_ALIGNMENT.md § ING-001):

    AT_ASK / ABOVE_ASK              -> True  (buyer paying up, unconditional)
    AT_BID / BELOW_BID  on PUT      -> True  (put seller writing at bid =
                                              conviction bullish; sell put =
                                              agree to buy at strike)
    AT_BID / BELOW_BID  on CALL     -> True  (call seller writing at bid =
                                              conviction bearish; write call =
                                              agree to sell at strike)
    MID                             -> False (passive / ambiguous)
    AT_BID / BELOW_BID  on other    -> False (empty/None/unknown ctype —
                                              cannot assign directional meaning
                                              without confirmed contract type)

  is_aggressive(trade_type) is retained as a deprecated shim.
  Do not remove until all callers are audited (ING-006 AC).

  Grep audit result (PBE-PREMERGE-F1, 2026-05-03):
  Zero callers of is_aggressive() remain in backend/ outside
  bid_ask_classifier.py itself after the options_flow_parser.py migration
  to is_directionally_aggressive(). The shim is safe to remove in the
  ING-007 cleanup sprint. Audit command used:
    grep -r "is_aggressive(" backend/ --include="*.py" -l
  Files found: bid_ask_classifier.py only.

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
    """Return trade aggressiveness classification.

    ING-006 SEMANTIC CHANGE (SA-PREMERGE-F1 deliberation record, 2026-05-03):
    This implementation replaces the previous ±10%-of-spread tolerance bands:

      OLD (pre-ING-006):
        tenth = (ask - bid) * 0.1
        fill >= ask + tenth  -> ABOVE_ASK
        fill >= ask - tenth  -> AT_ASK
        fill <= bid - tenth  -> BELOW_BID
        fill <= bid + tenth  -> AT_BID
        else                 -> MID

      NEW (ING-006+):
        fill >  ask  -> ABOVE_ASK
        fill == ask  -> AT_ASK
        fill <  bid  -> BELOW_BID
        fill == bid  -> AT_BID
        fill >  mid  -> AT_ASK   (inside spread, above midpoint)
        fill <  mid  -> AT_BID   (inside spread, below midpoint)
        fill == mid  -> MID

    Why the change was made:
      The ±10% tolerance bands were an approximation that created a
      wide MID zone in the middle of the spread. For a typical options
      market with a $0.10 spread, the tolerance was ±$0.01 — meaning
      any fill from bid+0.01 to ask-0.01 classified as MID (passive).
      This was intentional in the original design but produced false
      passives for fills that clearly leaned toward one side of the spread.

      The mid-split approach is exact and symmetric: every fill is
      assigned to the side of the spread it is closest to. A fill at
      exactly mid is the only true ambiguous case and returns MID.
      This tightens the passive zone from ~80% of the spread to a
      single point (fill == mid), which is effectively zero probability
      on a real tick stream.

    Behavioral delta (what changes in production):
      Fills that previously landed in the ±10% tolerance band around
      the bid or ask will now classify as AT_BID or AT_ASK instead of
      MID. These fills were ambiguous under the old logic; they are now
      treated as directional. This increases the count of events where
      is_directionally_aggressive() returns True for AT_ASK fills and
      AT_BID PUT/CALL fills, which feeds directly into
      RepetitionEpisode.weighted_premium.

    Test coverage: TestClassifyBidAsk in test_ing006_directional_aggression.py
    covers the 8-case boundary table for this implementation.
    """
    if ask <= bid:
        return "MID"
    mid = (bid + ask) / 2
    if fill >= ask:
        return "ABOVE_ASK" if fill > ask else "AT_ASK"
    if fill <= bid:
        return "BELOW_BID" if fill < bid else "AT_BID"
    if fill > mid:
        return "AT_ASK"
    if fill < mid:
        return "AT_BID"
    return "MID"


def is_directionally_aggressive(bid_ask_class: str, contract_type: str) -> bool:
    """
    ING-006: Directional aggression classification.

    Replaces is_aggressive(trade_type) in the parser hot path.
    Considers bid_ask_class AND contract_type per ING-001 deliberation
    sign-off (SPRINT_WSJ_INGESTION_ALIGNMENT.md § ING-001):

      AT_ASK / ABOVE_ASK            -> True  unconditionally (buyer paying up)
      AT_BID / BELOW_BID on PUT     -> True  (put seller writing at/below bid =
                                              conviction bullish; sell put =
                                              agree to buy at strike)
      AT_BID / BELOW_BID on CALL    -> True  (call seller writing at/below bid =
                                              conviction bearish; write call =
                                              agree to sell at strike)
      MID                           -> False (passive / ambiguous)
      AT_BID / BELOW_BID on other   -> False (empty/None/unknown ctype —
                                              no confirmed contract type means
                                              no directional classification;
                                              safe default per QA-F1)

    PUT and CALL are symmetric at the bid:
      A put seller writing at bid has one coherent directional interpretation
      (bullish). A call seller writing at bid is the symmetric bearish case.
      Both are conviction position writers, not passive limit-order buyers.
      Both are flagged True. Only genuinely ambiguous or unresolved contract
      types (empty string, None, SPREAD, UNKNOWN, etc.) return False —
      directional meaning cannot be assigned without a confirmed ctype.

    No size threshold here — ING-002 $10k per-event floor is the correct
    upstream guard. By the time this runs the event has already cleared $10k
    (deliberation SA-Q1, 2026-05-03).

    TEMPORARY LOCATION: This function belongs in order_side_classifier.py
    (S2 scope). Migration tracked in GitHub Issue filed 2026-05-03 and
    SPRINT_WSJ_INGESTION_ALIGNMENT.md ING-006 SA-F1 resolution row.
    """
    ba    = (bid_ask_class or "").strip().upper()
    ctype = (contract_type or "").strip().upper()
    if ba in ("AT_ASK", "ABOVE_ASK"):
        # Buyer paying at or above ask — always aggressive regardless of contract type.
        return True
    if ba in ("AT_BID", "BELOW_BID") and ctype in ("PUT", "CALL"):
        # Seller writing at or below bid — conviction directional position writer.
        # PUT seller at bid = bullish (agree to buy at strike).
        # CALL seller at bid = bearish (agree to sell at strike).
        # Symmetric cases per ING-001 resolution sign-off.
        # Empty/None/unknown ctype excluded: no contract type = no directional meaning.
        return True
    return False


def is_aggressive(trade_type: TradeType) -> bool:
    """Deprecated — use is_directionally_aggressive(bid_ask_class, contract_type).

    Retained as backward-compat shim for any callers not yet migrated.
    Do not use for new code.

    Removal status (PBE-PREMERGE-F1, 2026-05-03): grep audit confirmed zero
    callers remain in backend/ after options_flow_parser.py migration.
    Safe to remove in ING-007 cleanup sprint.
    """
    return trade_type in ("ABOVE_ASK", "AT_ASK")
