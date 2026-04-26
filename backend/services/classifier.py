"""
services/classifier.py — Options flow trade classifier.

Classifies a single options flow event into a human-readable signal label
based on trade_type, premium size, contract_type, and sentiment.

Labels (in descending priority):
  GOLDEN_SWEEP   — sweep, premium >= 500k, CALL, bullish
  WHALE_BLOCK    — block, premium >= 1M,   any,  any
  DARK_POOL_BULL — block, premium >= 500k, CALL, bullish
  DARK_POOL_BEAR — block, premium >= 500k, PUT,  bearish
  CALL_SWEEP     — sweep, CALL,  bullish/neutral
  PUT_SWEEP      — sweep, PUT,   bearish/neutral
  SMART_MONEY    — block, premium >= 100k, any, bullish
  UNUSUAL_CALL   — CALL,  any premium, any trade type
  UNUSUAL_PUT    — PUT,   any premium, any trade type
  FLOW           — fallback for all other combos
"""
from __future__ import annotations

_GOLDEN_SWEEP_THRESHOLD  = 500_000.0
_WHALE_BLOCK_THRESHOLD   = 1_000_000.0
_DARK_POOL_THRESHOLD     = 500_000.0
_SMART_MONEY_THRESHOLD   = 100_000.0


def classify(
    trade_type: str,
    premium: float,
    contract_type: str,
    sentiment: str,
) -> str:
    """Return a classification label string for an options flow event.

    All string comparisons are case-insensitive.
    Always returns a non-empty str; never raises on unknown inputs.

    Args:
        trade_type:    e.g. "sweep", "block", "split"
        premium:       dollar value of the trade (float)
        contract_type: "CALL" or "PUT" (case-insensitive)
        sentiment:     "bullish", "bearish", or "neutral" (case-insensitive)

    Returns:
        Classification label as an UPPER_SNAKE_CASE string.
    """
    tt  = (trade_type    or "").strip().lower()
    ct  = (contract_type or "").strip().upper()
    snt = (sentiment     or "").strip().lower()

    try:
        prem = float(premium)
    except (TypeError, ValueError):
        prem = 0.0

    # ── Tier-1: GOLDEN_SWEEP ────────────────────────────────────────────────
    if tt == "sweep" and prem >= _GOLDEN_SWEEP_THRESHOLD and ct == "CALL" and snt == "bullish":
        return "GOLDEN_SWEEP"

    # ── Tier-2: WHALE_BLOCK ─────────────────────────────────────────────────
    if tt == "block" and prem >= _WHALE_BLOCK_THRESHOLD:
        return "WHALE_BLOCK"

    # ── Tier-3: DARK_POOL directional ───────────────────────────────────────
    if tt == "block" and prem >= _DARK_POOL_THRESHOLD:
        if ct == "CALL" and snt == "bullish":
            return "DARK_POOL_BULL"
        if ct == "PUT" and snt == "bearish":
            return "DARK_POOL_BEAR"

    # ── Tier-4: Sweeps ───────────────────────────────────────────────────────
    if tt == "sweep":
        if ct == "CALL":
            return "CALL_SWEEP"
        if ct == "PUT":
            return "PUT_SWEEP"

    # ── Tier-5: SMART_MONEY block ────────────────────────────────────────────
    if tt == "block" and prem >= _SMART_MONEY_THRESHOLD and snt == "bullish":
        return "SMART_MONEY"

    # ── Tier-6: Unusual activity by contract type ────────────────────────────
    if ct == "CALL":
        return "UNUSUAL_CALL"
    if ct == "PUT":
        return "UNUSUAL_PUT"

    # ── Fallback ─────────────────────────────────────────────────────────────
    return "FLOW"
