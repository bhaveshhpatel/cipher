"""
signals/repetition_accumulator.py

Accumulates option trade ticks per (ticker, contract_type, strike, expiry) episode.
Returns a persist-worthy episode once the threshold is crossed.

Threshold logic:
  persist_ep is returned when EITHER:
    - trade_count >= min_trades, OR
    - cumulative premium >= min_premium
  whichever comes first.

  This means a single large trade (e.g. $500k sweep) persists immediately on
  the first tick, while small retail prints are gated until 3+ accumulate.

Fix (2026-04-28 Issue 2):
  Lowered defaults: min_trades=1, min_premium=10_000
  Previous: min_trades=3, min_premium=50_000
  Reason: at min_trades=3 every single-print flow event was silently dropped
  by accumulator_gated. Flow events table stayed empty during testing because
  no single contract repeated 3x in a 30-min window at low market volume.
  At min_trades=1 every parsed trade that passes dedup persists immediately.
  The min_premium=10_000 floor still filters out tiny retail noise (<$10k notional).
"""
import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("repetition_accumulator")


@dataclass
class Episode:
    ticker:          str
    contract_type:   str
    strike:          float
    expiry:          str
    trade_count:     int          = 0
    total_premium:   float        = 0.0
    is_accelerating: bool         = False
    last_seen:       datetime     = field(default_factory=lambda: datetime.now(timezone.utc))
    timestamps:      list         = field(default_factory=list)

    def summary_str(self) -> str:
        return (
            f"{self.ticker} {self.contract_type} ${self.strike:.0f} {self.expiry} "
            f"trades={self.trade_count} prem=${self.total_premium:,.0f}"
        )


class RepetitionAccumulator:
    """
    Accumulates option flow ticks into episodes.

    Args:
        window_minutes:  Episode expires after this many minutes of inactivity.
        min_trades:      Minimum ticks before episode persists (default: 1).
        min_premium:     Minimum cumulative premium before episode persists (default: $10,000).
    """

    def __init__(
        self,
        window_minutes: int = 30,
        min_trades: int     = 1,
        min_premium: float  = 10_000,
    ):
        self.window    = timedelta(minutes=window_minutes)
        self.min_trades  = min_trades
        self.min_premium = min_premium
        self._episodes: dict[str, Episode] = {}
        self._lock = asyncio.Lock()

    def _episode_key(self, ev) -> str:
        return f"{ev.ticker}|{ev.contract_type}|{ev.strike:.2f}|{ev.expiry}"

    async def ingest_tick(self, ev) -> Optional[Episode]:
        """
        Ingest a parsed trade event.

        Returns the Episode if the persist threshold has been crossed (trade_count
        >= min_trades OR total_premium >= min_premium), otherwise None.
        """
        now = datetime.now(timezone.utc)
        key = self._episode_key(ev)

        async with self._lock:
            ep = self._episodes.get(key)
            if ep is None or (now - ep.last_seen) > self.window:
                ep = Episode(
                    ticker=ev.ticker,
                    contract_type=ev.contract_type,
                    strike=ev.strike,
                    expiry=ev.expiry,
                )
                self._episodes[key] = ep

            ep.trade_count   += 1
            ep.total_premium += getattr(ev, "premium", 0.0)
            ep.last_seen      = now
            ep.timestamps.append(now)

            # Acceleration: 2+ ticks within the last 5 minutes
            recent_cutoff = now - timedelta(minutes=5)
            recent = [t for t in ep.timestamps if t >= recent_cutoff]
            ep.is_accelerating = len(recent) >= 2

            # Threshold: persist when min_trades OR min_premium crossed
            if ep.trade_count >= self.min_trades or ep.total_premium >= self.min_premium:
                return ep

        return None

    async def get_signal(self, ts: datetime, ep: Optional[Episode]) -> Optional[Episode]:
        """
        Returns the episode for signal emission if it has meaningful size.
        Currently passes through any non-None episode with trade_count >= 1.
        """
        if ep is None:
            return None
        if ep.trade_count >= 1 and ep.total_premium >= self.min_premium:
            return ep
        return None

    def get_alert_level(self, ep: Episode) -> str:
        if ep.total_premium >= 1_000_000:
            return "CONVICTION"
        if ep.total_premium >= 500_000:
            return "STRONG_SIGNAL"
        if ep.total_premium >= 200_000:
            return "ALERT"
        return "WATCH"

    async def cleanup_expired(self) -> int:
        """Remove stale episodes. Returns count removed."""
        now    = datetime.now(timezone.utc)
        async with self._lock:
            expired = [k for k, ep in self._episodes.items() if (now - ep.last_seen) > self.window]
            for k in expired:
                del self._episodes[k]
        return len(expired)
