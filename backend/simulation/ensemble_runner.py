"""
ensemble_runner.py — DEPRECATED (Apex S0)

This module is no longer called by the hot path. build_composite_async has
been removed from composite_signal_engine. This file is retained only to
avoid breaking any external import that has not yet been updated. It will
be deleted once no remaining caller references it.

DO NOT add new callers. DO NOT re-introduce this into the signal pipeline.
"""
import warnings

warnings.warn(
    "simulation.ensemble_runner is deprecated and will be removed. "
    "No production code path should call run_ensemble.",
    DeprecationWarning,
    stacklevel=2,
)


async def run_ensemble(*args, **kwargs):  # type: ignore[override]
    raise NotImplementedError(
        "run_ensemble is deprecated. "
        "Remove this call from the caller and use build_composite() directly."
    )
