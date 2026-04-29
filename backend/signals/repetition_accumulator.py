"""
signals/repetition_accumulator.py

Three-tier accumulation API (C-007 / C-008):

  ingest_tick(ev)  -> Optional[RepetitionEpisode]
      Gate-1 only: min_trades + min_premium threshold.
      NO cooldown. NO Gate-2 delta.
      Called by _process_trade() to decide whether to persist_flow_event.
      Returns ep every time above threshold, on every qualifying tick.

  get_signal(ts, ep) -> Optional[RepetitionEpisode]
      Cooldown gate only. Takes a pre-built ep + current timestamp.
      Returns ep if cooldown has elapsed since last_signal_at, else None.
      Called by _process_trade() to decide whether to publish to bus.

  ingest(ev)  -> Optional[RepetitionEpisode]
      Backward-compat shim: calls ingest_tick then get_signal.
      Applies both Gate-1 and cooldown. Used by C-002/C-007 tests.

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
    ticker:                str
    contract_type:         str
    strike:                float = 0.0
    expiry:                str   = ""
    occ_symbol:            Optional[str]  = None
    direction:             Optional[str]  = None
    last_signaled_premium: float          = 0.0
    last_signal_at:        Optional[datetime] = None
    events:                List  = field(default_factory=list)
    first_seen:            Optional[datetime] = None
    last_seen:             Optional[datetime] = None

    @property
    def trade_count(self) -> int:
        return len(self.events)

    @property
    def total_premium(self) -> float:
        return sum(getattr(e, "premium", 0.0) for e in self.events)

    @property
    def is_accelerating(self) -> bool:
        if len(self.events) < 3:
            return False
        last3 = self.events[-3:]
        try:
            ts_vals = [e.timestamp for e in last3]
            span = (max(ts_vals) - min(ts_vals)).total_seconds()
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
    Accumulates option flow ticks into per-contract episodes.

    Args:
        window_minutes:   Rolling window — events older than this are pruned.
        min_trades:       Minimum ticks to cross Gate-1.
        min_premium:      Minimum cumulative premium to cross Gate-1.
        signal_cooldown:  Minutes to suppress re-signals (applied in get_signal / ingest only).
        retrigger / retrigger_delta:  Not used by ingest_tick (legacy param, kept for compat).
    """

    def __init__(
        self,
        window_minutes:  int   = 30,
        min_trades:      int   = 3,
        min_premium:     float = 50_000,
        signal_cooldown: int   = 0,
        retrigger:       float = 50_000,
        retrigger_delta: float = 50_000,
    ):
        self.window          = timedelta(minutes=window_minutes)
        self.min_trades      = min_trades
        self.min_premium     = min_premium
        self.signal_cooldown = timedelta(minutes=signal_cooldown)
        self.retrigger_delta = retrigger_delta if retrigger_delta != 50_000 else retrigger

        self._episodes: dict = {}
        # One lock per episode key — only used within ingest_tick, never re-entered.
        self._locks:    dict = {}

    # ------------------------------------------------------------------ #
    # Key helpers
    # ------------------------------------------------------------------ #

    def _key(self, ev) -> str:
        if isinstance(ev, dict):
            ticker = ev.get("ticker", "")
            ctype  = ev.get("contract_type", "")
            strike = ev.get("strike", 0.0)
            expiry = ev.get("expiry", "")
        else:
            ticker = getattr(ev, "ticker", "")
            ctype  = getattr(ev, "contract_type", "")
            strike = getattr(ev, "strike", 0.0)
            expiry = getattr(ev, "expiry", "")
        return f"{ticker}|{ctype}|{float(strike):.2f}|{expiry}"

    def _episode_key(self, ev) -> str:
        return self._key(ev)

    def _key_from_ep(self, ep: RepetitionEpisode) -> str:
        return f"{ep.ticker}|{ep.contract_type}|{ep.strike:.2f}|{ep.expiry}"

    def _get_lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    @staticmethod
    def _ev_attr(ev, attr: str, default=None):
        if isinstance(ev, dict):
            return ev.get(attr, default)
        return getattr(ev, attr, default)

    # ------------------------------------------------------------------ #
    # ingest_tick: Gate-1 only, no cooldown, no Gate-2
    # ------------------------------------------------------------------ #

    async def ingest_tick(self, ev) -> Optional[RepetitionEpisode]:
        """
        Gate-1 only: returns ep whenever min_trades AND min_premium are met.
        No cooldown. No Gate-2 delta check.
        Used by _process_trade() to decide whether to call persist_flow_event.
        """
        key  = self._key(ev)
        lock = self._get_lock(key)

        async with lock:
            ep = self._episodes.get(key)

            if ep is None:
                ticker = self._ev_attr(ev, "ticker", "")
                ctype  = self._ev_attr(ev, "contract_type", "")
                strike = float(self._ev_attr(ev, "strike", 0.0) or 0.0)
                expiry = self._ev_attr(ev, "expiry", "") or ""
                occ    = self._ev_attr(ev, "occ_symbol")
                dirn   = self._ev_attr(ev, "direction") or self._ev_attr(ev, "sentiment")
                ep = RepetitionEpisode(
                    ticker=ticker,
                    contract_type=ctype,
                    strike=strike,
                    expiry=expiry,
                    occ_symbol=occ,
                    direction=dirn,
                )
                self._episodes[key] = ep

            ev_ts = self._ev_attr(ev, "timestamp") or datetime.now(timezone.utc)
            if isinstance(ev_ts, (int, float)):
                ev_ts = datetime.fromtimestamp(ev_ts, tz=timezone.utc)
            cutoff = ev_ts - self.window

            # Wrap dict events so .premium and .timestamp attributes are available
            if isinstance(ev, dict):
                class _W:
                    def __init__(self, d):
                        self.premium   = d.get("premium", 0.0)
                        self.timestamp = d.get("timestamp") or datetime.now(timezone.utc)
                ev_wrapped = _W(ev)
            else:
                ev_wrapped = ev

            ep.events = [
                e for e in ep.events
                if getattr(e, "timestamp", ev_ts) >= cutoff
            ]
            ep.events.append(ev_wrapped)
            ep.last_seen  = ev_ts
            if ep.first_seen is None:
                ep.first_seen = ev_ts

            # Gate-1
            if ep.trade_count < self.min_trades or ep.total_premium < self.min_premium:
                return None

            return ep
        # lock released — ep returned above

    # ------------------------------------------------------------------ #
    # get_signal: cooldown gate only
    # ------------------------------------------------------------------ #

    async def get_signal(
        self,
        ts: datetime,
        ep: Optional[RepetitionEpisode],
    ) -> Optional[RepetitionEpisode]:
        """
        Cooldown gate only. Takes a pre-built ep and current timestamp.
        Returns ep if eligible to signal, else None.
        Does NOT acquire the episode lock (called after ingest_tick releases it).
        """
        if ep is None:
            return None

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
    # ingest: backward-compat shim (Gate-1 + cooldown)
    # ------------------------------------------------------------------ #

    async def ingest(self, ev) -> Optional[RepetitionEpisode]:
        """
        Backward-compat entry point used by C-002/C-007 tests.
        Calls ingest_tick (Gate-1) then get_signal (cooldown).
        Returns ep only when both gates pass.
        """
        ep = await self.ingest_tick(ev)
        if ep is None:
            return None
        ev_ts = self._ev_attr(ev, "timestamp") or datetime.now(timezone.utc)
        if isinstance(ev_ts, (int, float)):
            ev_ts = datetime.fromtimestamp(ev_ts, tz=timezone.utc)
        return await self.get_signal(ev_ts, ep)

    # ------------------------------------------------------------------ #
    # Alert level
    # ------------------------------------------------------------------ #

    def get_alert_level(self, ep) -> str:
        prem = ep.total_premium
        if prem >= 5_000_000:
            return "CONVICTION"
        if getattr(ep, "is_accelerating", False) and prem >= 1_000_000:
            return "CONVICTION"
        if prem >= 1_000_000:
            return "STRONG_SIGNAL"
        if prem >= 250_000:
            return "ALERT"
        return "WATCH"

    # ------------------------------------------------------------------ #
    # Maintenance
    # ------------------------------------------------------------------ #

    async def cleanup_expired(self) -> int:
        now = datetime.now(timezone.utc)
        expired = [
            k for k, ep in list(self._episodes.items())
            if ep.last_seen and (now - ep.last_seen) > self.window
        ]
        for k in expired:
            del self._episodes[k]
        return len(expired)
