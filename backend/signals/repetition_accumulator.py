"""
signals/repetition_accumulator.py

Accumulates option trade ticks per (ticker, contract_type, strike, expiry) episode.
Returns a persist-worthy episode once the threshold is crossed.

Threshold logic:
  persist_ep is returned when EITHER:
    - trade_count >= min_trades, OR
    - cumulative premium >= min_premium
  whichever comes first.

  This means a single large trade (e.g. $80k+ print) persists immediately on
  the first tick via the min_premium OR condition, while small retail prints
  (< $10k, < 3 trades) are gated until the repetition threshold is crossed.

  Whale accumulation via many small lots is captured: each $12k print increments
  the episode until trade_count=3, then fires — even though each individual print
  is below the $10k floor (they're not — $12k > $10k fires immediately via OR).
  True sub-$10k repeated prints need 3 trades before persisting.

Defaults (2026-04-28):
  min_trades=3  — restored from 1; prevents every single print from persisting
                  as a raw flow log entry with no repetition signal value.
  min_premium=$10,000 — kept low so whale accumulation via small lots is captured
                        and single large institutional prints fire immediately.

The OR logic means:
  - Single print >= $10k    -> fires on tick 1 (premium OR condition)
  - Repeated prints < $10k  -> needs 3 ticks (trade_count condition)
  - Pure retail noise       -> < $10k AND < 3 trades = filtered
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, List

log = logging.getLogger("repetition_accumulator")


@dataclass
class RepetitionEpisode:
    ticker:          str
    contract_type:   str
    strike:          float
    expiry:          str
    trade_count:     int          = 0
    total_premium:   float        = 0.0
    is_accelerating: bool         = False
    last_seen:       datetime     = field(default_factory=lambda: datetime.now(timezone.utc))
    timestamps:      list         = field(default_factory=list)
    events:          List         = field(default_factory=list)

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
        min_trades:      Minimum ticks before episode persists (default: 3).
        min_premium:     Minimum cumulative premium before episode persists (default: $10,000).
    """

    def __init__(
        self,
        window_minutes: int = 30,
        min_trades: int     = 3,
        min_premium: float  = 10_000,
    ):
        self.window      = timedelta(minutes=window_minutes)
        self.min_trades  = min_trades
        self.min_premium = min_premium
        self._episodes: dict[str, RepetitionEpisode] = {}
        self._lock = asyncio.Lock()

    def _episode_key(self, ev) -> str:
        return f"{ev.ticker}|{ev.contract_type}|{ev.strike:.2f}|{ev.expiry}"

    async def ingest_tick(self, ev) -> Optional[RepetitionEpisode]:
        """
        Ingest a parsed trade event.

        Returns the RepetitionEpisode if the persist threshold has been crossed
        (trade_count >= min_trades OR total_premium >= min_premium), else None.
        """
        now = datetime.now(timezone.utc)
        key = self._episode_key(ev)

        async with self._lock:
            ep = self._episodes.get(key)
            if ep is None or (now - ep.last_seen) > self.window:
                ep = RepetitionEpisode(
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
            ep.events.append(ev)

            # Acceleration: 2+ ticks within the last 5 minutes
            recent_cutoff = now - timedelta(minutes=5)
            recent = [t for t in ep.timestamps if t >= recent_cutoff]
            ep.is_accelerating = len(recent) >= 2

            # Threshold: persist when min_trades OR min_premium crossed
            if ep.trade_count >= self.min_trades or ep.total_premium >= self.min_premium:
                return ep

        return None

    async def get_signal(self, ts: datetime, ep: Optional[RepetitionEpisode]) -> Optional[RepetitionEpisode]:
        """
        Returns the episode for signal emission if it has meaningful size.
        """
        if ep is None:
            return None
        if ep.trade_count >= 1 and ep.total_premium >= self.min_premium:
            return ep
        return None

    def get_alert_level(self, ep: RepetitionEpisode) -> str:
        if ep.total_premium >= 1_000_000:
            return "CONVICTION"
        if ep.total_premium >= 500_000:
            return "STRONG_SIGNAL"
        if ep.total_premium >= 200_000:
            return "ALERT"
        return "WATCH"

    async def cleanup_expired(self) -> int:
        """Remove stale episodes. Returns count removed."""
        now = datetime.now(timezone.utc)
        async with self._lock:
            expired = [k for k, ep in self._episodes.items() if (now - ep.last_seen) > self.window]
            for k in expired:
                del self._episodes[k]
        return len(expired)
