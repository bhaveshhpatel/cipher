"""
signals/repetition_accumulator.py

Accumulates option trade ticks per (ticker, contract_type, strike, expiry) episode.
Returns a persist-worthy episode once the threshold is crossed.

Threshold logic:
  persist_ep is returned when EITHER:
    - trade_count >= min_trades, OR
    - cumulative premium >= min_premium
  whichever comes first.

  Signal re-emission guard (2026-04-28):
    After an episode first crosses the threshold, it only emits again when
    total_premium has grown by at least SIGNAL_RETRIGGER_THRESHOLD ($50k) since
    the last emission. This prevents QQQ/SPY episodes from spamming a new
    signal_history row on every single tick once threshold is crossed.

Defaults:
  min_trades=3          - gates sub-$10k prints until 3 repeats
  min_premium=$10,000   - single large prints fire immediately via OR
  retrigger=$50,000     - re-signal every $50k of new premium per episode

The OR logic means:
  - Single print >= $10k          -> fires on tick 1 (premium OR condition)
  - Repeated prints < $10k        -> needs 3 ticks (trade_count condition)
  - Pure retail noise             -> < $10k AND < 3 trades = filtered
  - Ongoing episode               -> only re-signals every +$50k, not every tick
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, List

log = logging.getLogger("repetition_accumulator")

# Re-emit a signal for an active episode only after this much new premium
SIGNAL_RETRIGGER_THRESHOLD: float = 50_000


@dataclass
class RepetitionEpisode:
    ticker:               str
    contract_type:        str
    strike:               float
    expiry:               str
    # _trade_count and _total_premium are stored fields; public properties
    # fall back to len(events) / sum(event.premium) so that episodes
    # constructed directly in tests (without going through ingest_tick)
    # produce correct values for compute_flow_score / get_alert_level.
    _trade_count:         int      = field(default=0, repr=False)
    _total_premium:       float    = field(default=0.0, repr=False)
    _is_accelerating:     bool     = field(default=False, repr=False)
    last_seen:            datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    timestamps:           list     = field(default_factory=list)
    events:               List     = field(default_factory=list)
    # Signal re-emission guard - tracks premium at last signal emission
    last_signaled_premium: float   = 0.0
    # first_seen: used by tests that construct episodes directly
    first_seen:           Optional[datetime] = field(default=None)

    # ------------------------------------------------------------------
    # Public aliases expected by tests and composite_signal_engine
    # ------------------------------------------------------------------

    @property
    def trade_count(self) -> int:
        """Falls back to len(events) so directly-constructed episodes work."""
        if self._trade_count:
            return self._trade_count
        return len(self.events)

    @trade_count.setter
    def trade_count(self, value: int) -> None:
        self._trade_count = value

    @property
    def total_premium(self) -> float:
        """Falls back to sum of event.premium so directly-constructed episodes work."""
        if self._total_premium:
            return self._total_premium
        total = 0.0
        for ev in self.events:
            total += getattr(ev, "premium", 0) or 0
        return total

    @total_premium.setter
    def total_premium(self, value: float) -> None:
        self._total_premium = value

    @property
    def is_accelerating(self) -> bool:
        """Recomputes from timestamps so directly-constructed episodes work."""
        if self._is_accelerating:
            return True
        if len(self.timestamps) >= 2:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(minutes=5)
            recent = [t for t in self.timestamps if t >= cutoff]
            return len(recent) >= 2
        # Fall back to event timestamps if available
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=5)
        recent = 0
        for ev in self.events:
            ts = getattr(ev, "timestamp", None) or getattr(ev, "ts", None)
            if ts is not None and ts >= cutoff:
                recent += 1
        return recent >= 2

    @is_accelerating.setter
    def is_accelerating(self, value: bool) -> None:
        self._is_accelerating = value

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
        retrigger:       Minimum new premium delta before re-emitting signal (default: $50,000).
        signal_cooldown: Alias for retrigger (accepted for test compatibility).
    """

    def __init__(
        self,
        window_minutes:  int   = 30,
        min_trades:      int   = 3,
        min_premium:     float = 10_000,
        retrigger:       float = SIGNAL_RETRIGGER_THRESHOLD,
        signal_cooldown: Optional[float] = None,
    ):
        self.window      = timedelta(minutes=window_minutes)
        self.min_trades  = min_trades
        self.min_premium = min_premium
        # signal_cooldown is an alias for retrigger used in tests
        self.retrigger   = signal_cooldown if signal_cooldown is not None else retrigger
        self._episodes: dict[str, RepetitionEpisode] = {}
        self._lock = asyncio.Lock()

    def _episode_key(self, ev) -> str:
        return f"{ev.ticker}|{ev.contract_type}|{ev.strike:.2f}|{ev.expiry}"

    # Alias used by tests
    def _key(self, ev) -> str:
        return self._episode_key(ev)

    async def ingest_tick(self, ev) -> Optional[RepetitionEpisode]:
        """
        Ingest a parsed trade event.

        Returns the RepetitionEpisode if:
          1. The persist threshold has been crossed (trade_count >= min_trades
             OR total_premium >= min_premium), AND
          2. Either this is the first emission, OR total_premium has grown by
             at least `retrigger` since the last emission.
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

            ep._trade_count   += 1
            ep._total_premium += getattr(ev, "premium", 0.0)
            ep.last_seen       = now
            ep.timestamps.append(now)
            ep.events.append(ev)

            # Acceleration: 2+ ticks within the last 5 minutes
            recent_cutoff = now - timedelta(minutes=5)
            recent = [t for t in ep.timestamps if t >= recent_cutoff]
            ep._is_accelerating = len(recent) >= 2

            # Gate 1: must cross threshold (min_trades OR min_premium)
            threshold_crossed = (
                ep._trade_count >= self.min_trades
                or ep._total_premium >= self.min_premium
            )
            if not threshold_crossed:
                return None

            # Gate 2: only re-emit if this is the first crossing, or
            # at least `retrigger` new premium has accumulated since last signal
            delta = ep._total_premium - ep.last_signaled_premium
            if ep.last_signaled_premium == 0 or delta >= self.retrigger:
                ep.last_signaled_premium = ep._total_premium
                return ep

        return None

    # Alias: tests call acc.ingest(ev)
    async def ingest(self, ev) -> Optional[RepetitionEpisode]:
        return await self.ingest_tick(ev)

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
        prem = ep.total_premium
        if prem >= 1_000_000:
            return "CONVICTION"
        if prem >= 500_000:
            return "STRONG_SIGNAL"
        if prem >= 200_000:
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
