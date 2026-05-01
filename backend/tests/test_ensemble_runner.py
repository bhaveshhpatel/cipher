"""
test_ensemble_runner.py (post S0.5 — ensemble_runner.py deleted)

simulation/ensemble_runner.py was deleted in apex/s0.5.
All tests that imported it have been removed.

Retained: S0 guard — composite_signal_engine must not expose run_ensemble.
"""


def test_run_ensemble_not_callable_from_composite_engine():
    """S0 guard: composite_signal_engine must not expose run_ensemble."""
    import signals.composite_signal_engine as cse
    assert not hasattr(cse, "run_ensemble")
