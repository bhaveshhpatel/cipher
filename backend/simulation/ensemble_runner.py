"""
Aggregates SwarmEngine agent verdicts into a final ensemble result.

Phase 5A changes:
  - Fixed call to SwarmEngine.run() — now passes flow_events list directly
  - EnsembleResult includes per-agent name field
  - n_agents and n_runs params correctly wired
"""
from dataclasses import dataclass, field
from typing import List, Union
from simulation.swarm_engine import SwarmEngine


@dataclass
class EnsembleResult:
    ticker:      str
    direction:   str    # BUY | SELL | HOLD
    confidence:  float  # winning vote share
    bull_votes:  int
    bear_votes:  int
    hold_votes:  int
    summary:     str
    agents:      List[dict] = field(default_factory=list)


async def run_ensemble(
    ticker:      str,
    flow_events: Union[list, str],
    n_agents:    int = 6,
    n_runs:      int = 1,
) -> EnsembleResult:
    """
    Run the swarm and aggregate verdicts.

    n_agents: number of agents (snapped to nearest of 3, 6, 9, 12)
    n_runs:   reserved for future multi-run averaging (currently single-pass)
    flow_events: list of flow event dicts OR a pre-built summary string
    """
    engine   = SwarmEngine(n_agents=n_agents)
    verdicts = await engine.run(ticker, flow_events)

    bull  = sum(1 for v in verdicts if v.direction == "BUY")
    bear  = sum(1 for v in verdicts if v.direction == "SELL")
    hold  = sum(1 for v in verdicts if v.direction == "HOLD")
    total = len(verdicts) or 1

    if bull > bear and bull > hold:
        direction  = "BUY"
        confidence = round(bull / total, 3)
    elif bear > bull and bear > hold:
        direction  = "SELL"
        confidence = round(bear / total, 3)
    else:
        direction  = "HOLD"
        confidence = round(hold / total, 3)

    summary = (
        f"{total} agents evaluated {ticker}. "
        f"Bull: {bull}, Bear: {bear}, Hold: {hold}. "
        f"Ensemble verdict: {direction} with {confidence:.0%} confidence."
    )

    return EnsembleResult(
        ticker     = ticker,
        direction  = direction,
        confidence = confidence,
        bull_votes = bull,
        bear_votes = bear,
        hold_votes = hold,
        summary    = summary,
        agents     = [
            {
                "role":       v.role,
                "name":       v.name,
                "direction":  v.direction,
                "reasoning":  v.reasoning,
                "confidence": v.confidence,
            }
            for v in verdicts
        ],
    )
