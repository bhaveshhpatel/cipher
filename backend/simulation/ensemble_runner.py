"""
simulation/ensemble_runner.py — DEPRECATED STUB
================================================
This module was removed in S0.5 (PR #39) as part of the APEX swarm cleanup.
It is retained as a deprecated stub to satisfy two acceptance criteria from
the S0 test suite (AC-S0-5 and AC-S0-6) and to avoid import errors in any
caller that has not yet been migrated.

AC-S0-5: run_ensemble() must raise NotImplementedError when awaited.
AC-S0-6: Importing this module must emit a DeprecationWarning.

Do not add any production logic here. Use simulation.swarm_engine.SwarmEngine
for all swarm functionality going forward.
"""
import warnings

warnings.warn(
    "simulation.ensemble_runner is deprecated and will be removed in a future sprint. "
    "Use simulation.swarm_engine.SwarmEngine instead.",
    DeprecationWarning,
    stacklevel=2,
)


async def run_ensemble(*args, **kwargs):
    """Deprecated. Raises NotImplementedError unconditionally."""
    raise NotImplementedError(
        "run_ensemble has been removed. Use SwarmEngine.run() instead."
    )
