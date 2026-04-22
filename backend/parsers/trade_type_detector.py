"""
Detects sweep vs block vs split trades from raw options flow.
"""
from typing import Literal

TradeCategory = Literal["SWEEP","BLOCK","SPLIT","SINGLE"]

def detect_trade_type(
    size:         int,
    premium:      float,
    exchange_cnt: int,
    fill_count:   int,
) -> TradeCategory:
    """
    SWEEP  — same order filled across multiple exchanges rapidly.
    BLOCK  — single large fill, single exchange.
    SPLIT  — same strike/exp filled across many small lots.
    SINGLE — small single fill.
    """
    if exchange_cnt >= 3 and fill_count >= 3:
        return "SWEEP"
    if size >= 500 and fill_count == 1:
        return "BLOCK"
    if fill_count >= 5 and size >= 100:
        return "SPLIT"
    return "SINGLE"

def is_golden_sweep(
    trade_type: TradeCategory,
    premium:    float,
    above_ask:  bool,
) -> bool:
    """Golden sweep: aggressive sweep with premium > $500K."""
    return trade_type == "SWEEP" and above_ask and premium >= 500_000
