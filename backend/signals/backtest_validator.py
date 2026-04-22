"""
Lightweight backtest validator: checks historical win-rate for
similar (ticker, contract_type, DTE bucket, tier) combinations.
Results are cached per session; a real deployment would use Supabase.
"""
from typing import Dict, Tuple
import random

# Simulated historical win-rates per (ticker, contract_type, dte_bucket)
# In production this is a Supabase query aggregated over past 90 days.
_CACHE: Dict[Tuple, float] = {}

def _dte_bucket(dte: int) -> str:
    if dte <= 7:
        return "0-7"
    if dte <= 30:
        return "8-30"
    if dte <= 90:
        return "31-90"
    return "90+"

def get_backtest_score(
    ticker:        str,
    contract_type: str,
    dte:           int,
    influence_tier: str,
) -> float:
    """
    Returns a 0-1 score representing historical win-rate for similar setups.
    Fallback: deterministic pseudo-random seeded by inputs for demo consistency.
    """
    key = (ticker, contract_type, _dte_bucket(dte), influence_tier)
    if key not in _CACHE:
        # Seed with key for deterministic output
        seed = hash(key) % 10_000
        rng  = random.Random(seed)
        # Tier bias: whale signals historically stronger
        base = {"WHALE":0.72, "INSTITUTIONAL":0.63, "LARGE":0.55, "RETAIL":0.44}.get(influence_tier, 0.5)
        _CACHE[key] = round(min(0.95, max(0.2, base + rng.gauss(0, 0.08))), 3)
    return _CACHE[key]
