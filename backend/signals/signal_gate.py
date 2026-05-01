from typing import NamedTuple


class GateVerdict(NamedTuple):
    passed: bool
    reason: str


# Tier-aware premium floors keyed by tier → trade_type → minimum premium.
# T2 and T3 share the same floor table.
_PREMIUM_FLOORS: dict[int, dict[str, int]] = {
    1: {"SWEEP": 50_000, "BLOCK": 100_000, "SPLIT": 150_000, "SINGLE": 250_000},
    2: {"SWEEP": 25_000, "BLOCK": 50_000, "SPLIT": 100_000, "SINGLE": 150_000},
    3: {"SWEEP": 25_000, "BLOCK": 50_000, "SPLIT": 100_000, "SINGLE": 150_000},
}

# Unknown trade types are treated as impossible to pass (no floor defined).
_UNKNOWN_FLOOR_SENTINEL = 999_999_999


def passes_signal_gate(ev, tier: int) -> GateVerdict:
    """Evaluate whether *ev* (OptionsFlowEvent) clears the Apex L1 signal gate.

    Parameters
    ----------
    ev:
        Any object exposing the fields: ``ask`` (float), ``bid`` (float),
        ``premium`` (float), ``trade_type`` (str).
    tier:
        Ticker quality tier — 1 (mega-cap), 2 (large-cap), or 3 (everything else).

    Returns
    -------
    GateVerdict
        ``passed=True`` / ``reason="passed"`` when the event clears all checks.
        ``passed=False`` with a descriptive ``reason`` string on the first failing check.
    """
    # ── Spread gate ──────────────────────────────────────────────────────────────
    # Uniform 50% threshold across all tiers.  Deliberately permissive:
    # pre-market and thin-hour institutional flow on T1 names (NVDA, TSLA)
    # routinely shows wide quoted spreads that would be incorrectly rejected
    # by a tighter cap.  The gate targets genuine junk — extreme-width synthetic
    # or zero-bid quotes — not normal thin-market conditions.
    if ev.ask > 0:
        spread_pct = (ev.ask - ev.bid) / ev.ask
        if spread_pct > 0.50:
            return GateVerdict(False, "spread_too_wide")

    # ── Premium floor gate ───────────────────────────────────────────────────────
    # Tier-aware per-trade-type minimum premium.
    # Clamp unknown tier values to tier-3 floors (most permissive of all defined
    # tiers) so a mis-classified ticker does not silently pass as if no floor
    # existed — it still gets *a* floor, just the least restrictive one.
    tier_floors = _PREMIUM_FLOORS.get(tier, _PREMIUM_FLOORS[3])
    min_premium = tier_floors.get(ev.trade_type, _UNKNOWN_FLOOR_SENTINEL)

    if ev.premium < min_premium:
        return GateVerdict(False, "premium_below_floor")

    return GateVerdict(True, "passed")
