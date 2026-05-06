"""
parsers/order_side_classifier.py

Maps (order_side, contract_type) → dominant direction string used by
RepetitionEpisode.dominant_direction and the S2 parser pipeline.

Semantics (S2 invariant):
  BUY  + CALL → REPEAT_BUY    (straightforward bullish)
  SELL + PUT  → REPEAT_BUY    (put-selling is bullish positioning)
  BUY  + PUT  → REPEAT_SELL   (protective/directional bear)
  SELL + CALL → REPEAT_SELL   (call-selling is bearish/capped)
  UNKNOWN     → falls back on contract_type
                  CALL → REPEAT_BUY
                  PUT  → REPEAT_SELL
                  else → REPEAT_BUY  (safe default)

All comparisons are case-insensitive and stripped.

Re-exports:
  is_directionally_aggressive — source of truth is bid_ask_classifier.py
  (ING-006). Re-exported here so callers importing from this module
  resolve correctly. Issues #63 and #66 track long-term migration to a
  dedicated aggression module.
"""

from __future__ import annotations

from parsers.bid_ask_classifier import is_directionally_aggressive  # noqa: F401  re-export

__all__ = ["order_side_to_direction", "is_directionally_aggressive"]

_REPEAT_BUY = "REPEAT_BUY"
_REPEAT_SELL = "REPEAT_SELL"

# (order_side_upper, contract_type_upper) → direction
_LOOKUP: dict[tuple[str, str], str] = {
    ("BUY", "CALL"): _REPEAT_BUY,
    ("SELL", "PUT"): _REPEAT_BUY,
    ("BUY", "PUT"): _REPEAT_SELL,
    ("SELL", "CALL"): _REPEAT_SELL,
}


def order_side_to_direction(order_side: str, contract_type: str) -> str:
    """Return ``REPEAT_BUY`` or ``REPEAT_SELL`` for the given side/contract pair.

    Args:
        order_side: Raw order side string, e.g. ``"BUY"``, ``"SELL"``,
            ``"UNKNOWN"`` (case-insensitive).
        contract_type: Option contract type, e.g. ``"CALL"`` or ``"PUT"``
            (case-insensitive).

    Returns:
        ``"REPEAT_BUY"`` or ``"REPEAT_SELL"``.
    """
    side = (order_side or "").strip().upper()
    ctype = (contract_type or "").strip().upper()

    result = _LOOKUP.get((side, ctype))
    if result is not None:
        return result

    # UNKNOWN order_side — fall back on contract_type alone
    if ctype == "PUT":
        return _REPEAT_SELL
    # CALL or anything else → bullish default
    return _REPEAT_BUY
