"""
Tracks repeated options flow on the same contract over a rolling window.
Emits a signal when repetition thresholds are met.

C-007 — Signal Cooldown:
  ingest() previously returned the episode on EVERY call once thresholds
  were crossed, causing signal spam with 32 concurrent StreamWorker coroutines.
  Fix: last_signal_at tracked per RepetitionEpisode. ingest() only returns ep
  when threshold is crossed AND either:
    (a) this is the first signal (last_signal_at is None), OR
    (b) signal_cooldown has elapsed since last_signal_at.
  Default cooldown: 5 minutes. Configurable via signal_cooldown param.

  Note: asyncio is single-threaded and ingest() has zero await points, so
  concurrent worker access is safe without locks. The bug was signal spam,
  not a mutation race.

Clink 8 (future): decouple persist tier from signal tier so ticks during
  cooldown still write to flow_events for full backtesting fidelity.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from parsers.options_flow_parser import OptionsFlowEvent


@dataclass
class RepetitionEpisode:
    ticker:          str
    contract_type:   str
    strike:          float
    expiry:          str
    events:          List[OptionsFlowEvent] = field(default_factory=list)
    first_seen:      Optional[datetime]     = None
    last_seen:       Optional[datetime]     = None
    last_signal_at:  Optional[datetime]     = None  # C-007: cooldown tracking

    @property
    def trade_count(self) -> int:
        return len(self.events)

    @property
    def total_premium(self) -> float:
        return sum(e.premium for e in self.events)

    @property
    def is_accelerating(self) -> bool:
        if len(self.events) < 3:
            return False
        recent = self.events[-3:]
        span   = (recent[-1].timestamp - recent[0].timestamp).total_seconds()
        return span <= 60

    def summary_str(self) -> str:
        return (
            f"{self.trade_count}x {self.contract_type} ${self.strike} "
            f"exp {self.expiry} | ${self.total_premium:,.0f} total prem"
        )


class RepetitionAccumulator:
    def __init__(
        self,
        window_minutes:  int   = 30,
        min_trades:      int   = 3,
        min_premium:     float = 50_000,
        signal_cooldown: int   = 5,   # C-007: minutes between repeated signals on same episode
    ):
        self.window          = timedelta(minutes=window_minutes)
        self.min_trades      = min_trades
        self.min_premium     = min_premium
        self.signal_cooldown = timedelta(minutes=signal_cooldown)  # C-007
        self._episodes: Dict[str, RepetitionEpisode] = {}

    def _key(self, ev: OptionsFlowEvent) -> str:
        return f"{ev.ticker}:{ev.contract_type}:{ev.strike}:{ev.expiry}"

    def ingest(self, ev: OptionsFlowEvent) -> Optional[RepetitionEpisode]:
        """
        Add event to the rolling episode window.

        Returns the episode if:
          - trade_count >= min_trades AND total_premium >= min_premium, AND
          - either this is the first qualifying signal (last_signal_at is None)
            OR signal_cooldown has elapsed since the last signal.

        Returns None if:
          - thresholds not yet met, OR
          - thresholds met but cooldown is still active (suppress spam).

        C-007: cooldown is per-episode-key. Different contracts on the same
        ticker are independent episodes and do not share cooldown state.
        """
        key = self._key(ev)
        ep  = self._episodes.setdefault(key, RepetitionEpisode(
            ticker        = ev.ticker,
            contract_type = ev.contract_type,
            strike        = ev.strike,
            expiry        = ev.expiry,
        ))

        # Prune stale events outside rolling window
        cutoff    = ev.timestamp - self.window
        ep.events = [e for e in ep.events if e.timestamp >= cutoff]
        ep.events.append(ev)
        ep.first_seen = ep.events[0].timestamp
        ep.last_seen  = ev.timestamp

        # Check thresholds
        if ep.trade_count < self.min_trades or ep.total_premium < self.min_premium:
            return None

        # C-007: cooldown gate — suppress if signal fired recently
        if ep.last_signal_at is not None:
            elapsed = ev.timestamp - ep.last_signal_at
            if elapsed < self.signal_cooldown:
                return None

        # Threshold crossed AND (first signal OR cooldown elapsed) — fire
        ep.last_signal_at = ev.timestamp
        return ep

    def get_alert_level(self, ep: RepetitionEpisode) -> str:
        prem = ep.total_premium
        if prem >= 5_000_000 or (ep.is_accelerating and prem >= 1_000_000):
            return "CONVICTION"
        if prem >= 1_000_000:
            return "STRONG_SIGNAL"
        if prem >= 250_000:
            return "ALERT"
        return "WATCH"
