"""
Tracks repeated options flow on the same contract over a rolling window.
Emits a signal when repetition thresholds are met.
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
        # Check if last 3 events happened within 60 seconds
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
        window_minutes:  int = 30,
        min_trades:      int = 3,
        min_premium:     float = 50_000,
    ):
        self.window   = timedelta(minutes=window_minutes)
        self.min_trades   = min_trades
        self.min_premium  = min_premium
        self._episodes: Dict[str, RepetitionEpisode] = {}

    def _key(self, ev: OptionsFlowEvent) -> str:
        return f"{ev.ticker}:{ev.contract_type}:{ev.strike}:{ev.expiry}"

    def ingest(self, ev: OptionsFlowEvent) -> Optional[RepetitionEpisode]:
        """Add event. Returns episode if signal threshold crossed, else None."""
        key = self._key(ev)
        ep  = self._episodes.setdefault(key, RepetitionEpisode(
            ticker        = ev.ticker,
            contract_type = ev.contract_type,
            strike        = ev.strike,
            expiry        = ev.expiry,
        ))

        # Prune stale events outside rolling window
        cutoff = ev.timestamp - self.window
        ep.events = [e for e in ep.events if e.timestamp >= cutoff]
        ep.events.append(ev)
        ep.first_seen = ep.events[0].timestamp
        ep.last_seen  = ev.timestamp

        if ep.trade_count >= self.min_trades and ep.total_premium >= self.min_premium:
            return ep
        return None

    def get_alert_level(self, ep: RepetitionEpisode) -> str:
        prem = ep.total_premium
        if prem >= 5_000_000 or (ep.is_accelerating and prem >= 1_000_000):
            return "CONVICTION"
        if prem >= 1_000_000:
            return "STRONG_SIGNAL"
        if prem >= 250_000:
            return "ALERT"
        return "WATCH"
