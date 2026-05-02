"""
signals/ladder_detector.py

S5 — Apex L4: Cross-Contract Ladder Detection

Detects coordinated multi-strike positioning on the same ticker and expiry.
A ladder fires when 3 or more distinct strikes are active on the same
(ticker, expiry) pair from currently qualifying episodes.

Design invariants (from spec and QA-25 / QA-26):
  - Only same-expiry episodes combine into a ladder.
  - Episodes from different expiries on the same ticker do NOT combine.
  - Stale episodes (last_seen older than expires_before) are excluded.
  - Returns the first qualifying ladder found, or None if no ladder exists.
  - total_premium is the sum of all contributing episodes' total_premium.
  - strikes is a sorted list of distinct strike values.

This module has no side-effects and does not mutate the episode objects.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, NamedTuple, Optional, Sequence

from signals.repetition_accumulator import RepetitionEpisode


class LadderSignal(NamedTuple):
    """Result returned when a coordinated multi-strike ladder is detected."""
    ticker:        str
    expiry:        str
    strikes:       list  # sorted list[float] of distinct strikes in the ladder
    total_premium: float


def detect_ladder(
    active_eps: Sequence[RepetitionEpisode],
    min_strikes: int = 3,
    expires_before: Optional[datetime] = None,
) -> Optional[LadderSignal]:
    """
    Scan active qualifying episodes for a coordinated multi-strike ladder.

    Args:
        active_eps:     Iterable of RepetitionEpisode objects that have already
                        passed Apex L2 qualification (Gate-1 + cooldown).
                        Caller is responsible for passing only qualified episodes.
        min_strikes:    Minimum number of distinct strikes required on the same
                        (ticker, expiry) pair to constitute a ladder. Default 3.
        expires_before: If provided, episodes whose last_seen is earlier than
                        this datetime are excluded as stale. Pass
                        datetime.now(timezone.utc) - window to expire old state.
                        None = no stale filtering (all episodes accepted).

    Returns:
        LadderSignal for the first qualifying (ticker, expiry) group, or None
        if no ladder is found.

    Notes:
        - Cross-expiry guard: grouping is keyed by (ticker, expiry) so episodes
          on the same ticker but different expiries never combine into a ladder.
          This satisfies QA-26 (Ladder Negative Cross-Expiry Guard).
        - The returned strikes list is sorted ascending.
        - If multiple (ticker, expiry) groups qualify simultaneously, the first
          one encountered in iteration order is returned. Callers that need
          deterministic ordering should pre-sort active_eps before calling.
    """
    if not active_eps:
        return None

    grouped: dict[tuple[str, str], list[RepetitionEpisode]] = {}

    for ep in active_eps:
        # Stale-episode guard — exclude episodes whose last activity predates
        # the caller-supplied cutoff. When expires_before is None, no filtering.
        if expires_before is not None and ep.last_seen is not None:
            # Ensure both datetimes are comparable (both tz-aware or both naive).
            cutoff = expires_before
            ep_ts  = ep.last_seen
            if ep_ts.tzinfo is None and cutoff.tzinfo is not None:
                ep_ts = ep_ts.replace(tzinfo=timezone.utc)
            elif ep_ts.tzinfo is not None and cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)
            if ep_ts < cutoff:
                continue

        key = (ep.ticker, ep.expiry)
        grouped.setdefault(key, []).append(ep)

    for (ticker, expiry), eps in grouped.items():
        strikes = sorted({ep.strike for ep in eps})
        if len(strikes) >= min_strikes:
            return LadderSignal(
                ticker=ticker,
                expiry=expiry,
                strikes=strikes,
                total_premium=sum(ep.total_premium for ep in eps),
            )

    return None
