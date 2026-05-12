"""
services/signal_engine.py — DELETED (REARCH-006 Fix 1/4)

This file previously housed a thin dict-based SignalEngine and EpisodeEvalResult
that duplicated logic from signals/signal_engine.py.

As of Fix 1/4 (pre-merge SA finding), signals/signal_engine.py is the SOLE
authority for all gate evaluation.  This file now re-exports the canonical
symbols from signals/ so any import of the form:

    from services.signal_engine import EpisodeEvalResult, get_engine

does not break, but all implementations live in signals/signal_engine.py.

DO NOT add logic here.  Any gate, threshold, or evaluation change belongs
in signals/signal_engine.py only.
"""
# Re-export canonical symbols — no logic lives here.
from signals.signal_engine import (  # noqa: F401
    EpisodeEvalResult,
    GateResult,
    SignalEngine,
    compute_conviction_score,
    build_signal_row,
    get_engine,
    engine,
)

__all__ = [
    "EpisodeEvalResult",
    "GateResult",
    "SignalEngine",
    "compute_conviction_score",
    "build_signal_row",
    "get_engine",
    "engine",
]
