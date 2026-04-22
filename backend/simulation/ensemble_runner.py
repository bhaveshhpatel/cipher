"""
Aggregates SwarmEngine verdicts into a final ensemble result.
"""
from dataclasses import dataclass, field
from typing import List
from simulation.swarm_engine import SwarmEngine

@dataclass
class EnsembleResult:
    ticker:      str
    direction:   str   # BUY | SELL | HOLD
    confidence:  float
    bull_votes:  int
    bear_votes:  int
    hold_votes:  int
    summary:     str
    agents:      List[dict] = field(default_factory=list)


async def run_ensemble(
    ticker:      str,
    flow_events: list,
    n_agents:    int = 6,
    n_runs:      int = 1,
) -> EnsembleResult:
    engine   = SwarmEngine()
    verdicts = await engine.run(ticker, flow_events, n_agents, n_runs)

    bull = sum(1 for v in verdicts if v.direction == "BUY")
    bear = sum(1 for v in verdicts if v.direction == "SELL")
    hold = sum(1 for v in verdicts if v.direction == "HOLD")
    total = len(verdicts) or 1

    if bull > bear and bull > hold:
        direction   = "BUY"
        confidence  = bull / total
    elif bear > bull and bear > hold:
        direction   = "SELL"
        confidence  = bear / total
    else:
        direction   = "HOLD"
        confidence  = hold / total

    summary = (
        f"{total} agents evaluated {ticker}. "
        f"Bull: {bull}, Bear: {bear}, Hold: {hold}. "
        f"Ensemble verdict: {direction} with {confidence:.0%} confidence."
    )

    return EnsembleResult(
        ticker     = ticker,
        direction  = direction,
        confidence = round(confidence, 3),
        bull_votes = bull,
        bear_votes = bear,
        hold_votes = hold,
        summary    = summary,
        agents     = [
            {"role": v.role, "direction": v.direction, "reasoning": v.reasoning}
            for v in verdicts
        ],
    )
