"""
ensemble_runner.py — DEPRECATED (Apex S0)

This module is no longer called by the hot path. build_composite_async has
been removed from composite_signal_engine. This file is retained only to
avoid breaking any external import that has not yet been updated. It will
be deleted once no remaining caller references it.

DO NOT add new callers. DO NOT re-introduce this into the signal pipeline.
"""
import warnings
from dataclasses import dataclass, field
from typing import List

warnings.warn(
    "simulation.ensemble_runner is deprecated and will be removed. "
    "No production code path should call run_ensemble.",
    DeprecationWarning,
    stacklevel=2,
)


@dataclass
class EnsembleResult:
    """Retained for import compatibility. Do not instantiate in new code."""
    ticker:     str
    direction:  str
    confidence: float
    bull_votes: int
    bear_votes: int
    hold_votes: int
    summary:    str
    agents:     List[dict] = field(default_factory=list)


async def run_ensemble(*args, **kwargs):  # type: ignore[override]
    raise NotImplementedError(
        "run_ensemble is deprecated. "
        "Remove this call from the caller and use build_composite() directly."
    )
