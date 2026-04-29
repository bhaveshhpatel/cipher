"""
signals/repetition_accumulator.py

Accumulates option trade ticks per (ticker, contract_type, strike, expiry) episode.
Returns a persist-worthy episode once the threshold is crossed.

Threshold logic:
  Fires when trade_count >= min_trades AND total_premium >= min_premium.

Signal cooldown guard (C-007):
  After an episode first crosses the threshold it only re-emits once
  `signal_cooldown` minutes have elapsed since the last emission.

Defaults:
  min_trades=3
  min_premium=50_000
  signal_cooldown=5  (minutes)

Alert levels:
  >= 5_000_000                            -> CONVICTION
  is_accelerating AND >= 1_000_000        -> CONVICTION
  >= 1_000_000                            -> STRONG_SIGNAL
  >= 250_000                              -> ALERT
  else                                    -> WATCH
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, List

log = logging.getLogger("repetition_accumulator")


@dataclass
class RepetitionEpisode:
    ticker:         str
    contract_type:  str
    strike:         float
    expiry:         str
    events:         List   = field(default_factory=list)
    first_seen:     Optional[datetime] = None
    last_seen:      Optional[datetime] = None
    last_signal_at: Optional[datetime] = None

    # ------------------------------------------------------------------ #
    # Computed properties (derived from self.events)                       #
    # ------------------------------------------------------------------ #

    @property
    def trade_count(self) -> int:
        return len(self.events)

    @property
    def total_premium(self) -> float:
        return sum(getattr(e, "premium", 0.0) for e in self.events)

    @property
    def is_accelerating(self) -> bool:
        """True when the last 3 events all occurred within a 60-second span."""
        if len(self.events) < 3:
            return False
        last3 = self.events[-3:]
        try:
            ts = [e.timestamp for e in last3]
            span = (max(ts) - min(ts)).total_seconds()
            return span <= 60
        except Exception:
            return False

    def summary_str(self) -> str:
        return (
            f"{self.ticker} {self.contract_type} ${self.strike:.0f} {self.expiry} "
            f"trades={self.trade_count} prem=${self.total_premium:,.0f}"
        )


class RepetitionAccumulator:
    """
    Accumulates option flow ticks into episodes.

    Args:
        window_minutes:  Rolling window — events older than this are pruned.
        min_trades:      Minimum number of ticks required.
        min_premium:     Minimum cumulative premium required.
        signal_cooldown: Minutes to suppress re-signals after first emission.
    """

    def __init__(
        self,
        window_minutes:  int   = 30,
        min_trades:      int   = 3,
        min_premium:     float = 50_000,
        signal_cooldown: int   = 0,
        retrigger:       float = 50_000,  # kept for backward-compat, unused internally
    ):
        self.window          = timedelta(minutes=window_minutes)
        self.min_trades      = min_trades
        self.min_premium     = min_premium
        self.signal_cooldown = timedelta(minutes=signal_cooldown)

        self._episodes: dict = {}
        self._locks:    dict = {}
        self._global_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Key helpers                                                          #
    # ------------------------------------------------------------------ #

    def _key(self, ev) -> str:
        return f"{ev.ticker}|{ev.contract_type}|{ev.strike:.2f}|{ev.expiry}"

    # backward-compat alias used in many tests
    def _episode_key(self, ev) -> str:
        return self._key(ev)

    def _key_from_ep(self, ep: RepetitionEpisode) -> str:
        return f"{ep.ticker}|{ep.contract_type}|{ep.strike:.2f}|{ep.expiry}"

    # ------------------------------------------------------------------ #
    # Per-key asyncio.Lock (concurrent safety — issue #1)                 #
    # ------------------------------------------------------------------ #

    def _get_lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    # ------------------------------------------------------------------ #
    # Core ingest                                                          #
    # ------------------------------------------------------------------ #

    async def ingest_tick(self, ev) -> Optional[RepetitionEpisode]:
        """
        Ingest a parsed trade event. Returns the episode once both thresholds
        are met (trade_count >= min_trades AND total_premium >= min_premium).
        Does NOT apply signal_cooldown — callers wanting cooldown should use
        ingest() or get_signal().
        """
        key  = self._key(ev)
        lock = self._get_lock(key)

        async with lock:
            ep = self._episodes.get(key)

            if ep is None:
                ep = RepetitionEpisode(
                    ticker=ev.ticker,
                    contract_type=ev.contract_type,
                    strike=ev.strike,
                    expiry=ev.expiry,
                )
                self._episodes[key] = ep

            # Prune stale events from the rolling window
            ev_ts = getattr(ev, "timestamp", None) or datetime.now(timezone.utc)
            cutoff = ev_ts - self.window
            ep.events = [e for e in ep.events if getattr(e, "timestamp", ev_ts) >= cutoff]

            ep.events.append(ev)
            ep.last_seen  = ev_ts
            if ep.first_seen is None:
                ep.first_seen = ev_ts

            # Evict completely empty episode after pruning (edge case)
            if ep.trade_count == 0:
                del self._episodes[key]
                return None

            # Check thresholds
            if ep.trade_count >= self.min_trades and ep.total_premium >= self.min_premium:
                return ep

        return None

    async def ingest(self, ev) -> Optional[RepetitionEpisode]:
        """
        Backward-compat entry point that respects signal_cooldown.
        Returns the episode on first threshold crossing OR after cooldown elapses.
        """
        ep = await self.ingest_tick(ev)
        if ep is None:
            return None
        ev_ts = getattr(ev, "timestamp", None) or datetime.now(timezone.utc)
        return await self.get_signal(ev_ts, ep)

    # ------------------------------------------------------------------ #
    # Cooldown gate (concurrent safety — issue #2)                        #
    # ------------------------------------------------------------------ #

    async def get_signal(
        self,
        ts: datetime,
        ep: Optional[RepetitionEpisode],
    ) -> Optional[RepetitionEpisode]:
        """
        Atomically check the cooldown and emit the episode if eligible.

        Returns ep if:
          - ep is not None
          - no cooldown configured (signal_cooldown == 0), OR
          - last_signal_at is None (first emission), OR
          - enough time has elapsed since last_signal_at
        """
        if ep is None:
            return None

        key  = self._key_from_ep(ep)
        lock = self._get_lock(key)

        async with lock:
            if self.signal_cooldown.total_seconds() == 0:
                ep.last_signal_at = ts
                return ep

            if ep.last_signal_at is None:
                ep.last_signal_at = ts
                return ep

            elapsed = (ts - ep.last_signal_at).total_seconds()
            if elapsed >= self.signal_cooldown.total_seconds():
                ep.last_signal_at = ts
                return ep

        return None

    # ------------------------------------------------------------------ #
    # Alert level                                                          #
    # ------------------------------------------------------------------ #

    def get_alert_level(self, ep: RepetitionEpisode) -> str:
        prem = ep.total_premium
        if prem >= 5_000_000:
            return "CONVICTION"
        if ep.is_accelerating and prem >= 1_000_000:
            return "CONVICTION"
        if prem >= 1_000_000:
            return "STRONG_SIGNAL"
        if prem >= 250_000:
            return "ALERT"
        return "WATCH"

    # ------------------------------------------------------------------ #
    # Maintenance                                                          #
    # ------------------------------------------------------------------ #

    async def cleanup_expired(self) -> int:
        """Remove stale episodes. Returns count removed."""
        now = datetime.now(timezone.utc)
        async with self._global_lock:
            expired = [
                k for k, ep in self._episodes.items()
                if ep.last_seen and (now - ep.last_seen) > self.window
            ]
            for k in expired:
                del self._episodes[k]
        return len(expired)
