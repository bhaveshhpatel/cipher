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

C-008 — Decouple Persist Tier from Signal Tier:
  ingest() was the single gate for both DB writes and bus signals.
  When C-007 cooldown suppressed ingest(), qualifying ticks during the
  cooldown window were silently dropped from flow_events — backtesting gap.

  Fix: split into two explicit methods:
    ingest_tick(ev)  -> ep if above threshold (persist gate, ignores cooldown)
    get_signal(ts, ep) -> ep if cooldown elapsed (signal gate)

  ingest() preserved as backward-compat shim (calls both internally).
  _process_trade now calls ingest_tick + get_signal independently.
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
        signal_cooldown: int   = 5,   # C-007: minutes between repeated signals
    ):
        self.window          = timedelta(minutes=window_minutes)
        self.min_trades      = min_trades
        self.min_premium     = min_premium
        self.signal_cooldown = timedelta(minutes=signal_cooldown)
        self._episodes: Dict[str, RepetitionEpisode] = {}

    def _key(self, ev: OptionsFlowEvent) -> str:
        return f"{ev.ticker}:{ev.contract_type}:{ev.strike}:{ev.expiry}"

    def ingest_tick(self, ev: OptionsFlowEvent) -> Optional["RepetitionEpisode"]:
        """
        C-008: Persist tier gate.

        Adds ev to episode state, prunes the rolling window, and returns
        the episode if trade_count >= min_trades AND total_premium >= min_premium.

        Cooldown is NOT applied here — every qualifying tick returns ep so
        that persist_flow_event() can write it to flow_events for full
        backtesting fidelity.

        Returns None only when thresholds are not yet met.
        """
        key = self._key(ev)
        ep  = self._episodes.setdefault(key, RepetitionEpisode(
            ticker        = ev.ticker,
            contract_type = ev.contract_type,
            strike        = ev.strike,
            expiry        = ev.expiry,
        ))

        cutoff    = ev.timestamp - self.window
        ep.events = [e for e in ep.events if e.timestamp >= cutoff]
        ep.events.append(ev)
        ep.first_seen = ep.events[0].timestamp
        ep.last_seen  = ev.timestamp

        if ep.trade_count >= self.min_trades and ep.total_premium >= self.min_premium:
            return ep
        return None

    def get_signal(self, ts: datetime, ep: Optional["RepetitionEpisode"]) -> Optional["RepetitionEpisode"]:
        """
        C-008: Signal tier gate.

        Given an episode returned by ingest_tick(), applies the C-007 cooldown.
        Returns ep if cooldown has elapsed (or this is the first signal).
        Returns None if cooldown is still active — suppresses bus publish.

        ep.last_signal_at is updated here on every fire so that the next
        call to get_signal() sees the correct elapsed time.

        Pass ep=None (sub-threshold) and this is a guaranteed no-op returning None.
        """
        if ep is None:
            return None

        if ep.last_signal_at is not None:
            elapsed = ts - ep.last_signal_at
            if elapsed < self.signal_cooldown:
                return None

        ep.last_signal_at = ts
        return ep

    def ingest(self, ev: OptionsFlowEvent) -> Optional["RepetitionEpisode"]:
        """
        Backward-compat shim for C-002 / C-007 tests and any callers
        that use the original single-return API.

        Internally calls ingest_tick() + get_signal() and returns the
        signal ep (None if sub-threshold or cooldown active).

        New code in _process_trade should call ingest_tick() and
        get_signal() independently for decoupled persist/signal control.
        """
        persist_ep = self.ingest_tick(ev)
        return self.get_signal(ev.timestamp, persist_ep)

    def get_alert_level(self, ep: "RepetitionEpisode") -> str:
        prem = ep.total_premium
        if prem >= 5_000_000 or (ep.is_accelerating and prem >= 1_000_000):
            return "CONVICTION"
        if prem >= 1_000_000:
            return "STRONG_SIGNAL"
        if prem >= 250_000:
            return "ALERT"
        return "WATCH"
