"""
test_ensemble_runner.py (Apex S0 — deprecated module)

ensemble_runner.run_ensemble is now deprecated and raises NotImplementedError.
This file retains EnsembleResult structural tests (dataclass still exported
for import compatibility) and replaces all run_ensemble call tests with
deprecation / NotImplementedError assertions.
"""
import asyncio
import importlib
import sys
import warnings
from dataclasses import fields

import pytest


# ---------------------------------------------------------------------------
# EnsembleResult structural tests — still valid post-deprecation
# ---------------------------------------------------------------------------

def test_ensemble_result_has_all_fields():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from simulation.ensemble_runner import EnsembleResult
    field_names = {f.name for f in fields(EnsembleResult)}
    for name in ("ticker", "direction", "confidence",
                 "bull_votes", "bear_votes", "hold_votes", "summary", "agents"):
        assert name in field_names


def test_ensemble_result_agents_default_empty():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from simulation.ensemble_runner import EnsembleResult
    r = EnsembleResult(
        ticker="X", direction="HOLD", confidence=0.5,
        bull_votes=0, bear_votes=0, hold_votes=1,
        summary="test",
    )
    assert r.agents == []


# ---------------------------------------------------------------------------
# Deprecation contract — import must warn, run_ensemble must raise
# ---------------------------------------------------------------------------

def test_ensemble_runner_import_emits_deprecation_warning():
    sys.modules.pop("simulation.ensemble_runner", None)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        importlib.import_module("simulation.ensemble_runner")
    categories = [str(w.category) for w in caught]
    assert any("DeprecationWarning" in c for c in categories)


def test_run_ensemble_raises_not_implemented():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from simulation.ensemble_runner import run_ensemble
    with pytest.raises(NotImplementedError):
        asyncio.run(run_ensemble())


def test_run_ensemble_not_callable_from_composite_engine():
    """S0 guard: composite_signal_engine must not expose run_ensemble."""
    import signals.composite_signal_engine as cse
    assert not hasattr(cse, "run_ensemble")
