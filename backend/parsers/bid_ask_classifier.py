"""
Classifies a trade as ABOVE_ASK, AT_ASK, AT_BID, BELOW_BID, or MID
based on fill price relative to the bid/ask spread.
"""
from typing import Literal

TradeType = Literal["ABOVE_ASK","AT_ASK","MID","AT_BID","BELOW_BID"]

def classify_bid_ask(fill: float, bid: float, ask: float) -> TradeType:
    """Return trade aggressiveness classification."""
    if ask <= bid:
        return "MID"
    mid   = (bid + ask) / 2
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

def is_aggressive(trade_type: TradeType) -> bool:
    return trade_type in ("ABOVE_ASK", "AT_ASK")
