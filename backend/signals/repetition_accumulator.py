"""
signals/repetition_accumulator.py

S4 — Apex L2: Dual-Window Accumulator

Three-tier accumulation API (C-007 / C-008):

  ingest_tick(ev)  -> Optional[RepetitionEpisode]
      Gate-1 only: DTE-adjusted + OTM-adjusted min_premium threshold.
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

S4 additions:
  - DTE-adjusted premium floors (dte_premium_tiers)
  - OTM classification: ATM (<=2%), standard OTM (2-12%), deep OTM (>12%)
  - deep_otm_multiplier param retained for backward-compat; default changed
    to 1.0 (no penalty) by ING-005 — registry pre-filter is the authoritative
    OTM gate post-ING-004. Pass explicit deep_otm_multiplier>1.0 to re-enable.
  - Sweep bypass: len(ep.events)==1 AND SWEEP AND premium >= sweep_bypass_premium
  - min_sweeps gate (in addition to min_trades)
  - dominant_direction property on RepetitionEpisode (premium-weighted)
  - tier_map injection for DTE-floor tier lookup

ING-006 additions:
  - _AGGRESSION_DISCOUNT = 0.5 module-level constant.
    Passive events (is_aggressive=False) contribute premium * 0.5 to
    weighted_premium. Aggressive events contribute full premium * 1.0.
    Gate 2 (DTE-adjusted floor) now evaluates weighted_premium, not total_premium.
    Rationale: mid-spread fills represent uncertain directional intent.
    Discounting them increases signal-to-noise without dropping the event.
    Configurable via ING-002-CONFIG in a future sprint (PBE-Q2 deliberation).
  - RepetitionEpisode.weighted_premium property.
  - _DictEventWrapper: is_aggressive slot added.

Alert levels (reconciled across all test suites — see get_alert_level() for
full change-log from S1 spec):
  >= 2_000_000                            -> CONVICTION
  is_accelerating AND >= 500_000          -> CONVICTION
  >= 1_000_000                            -> STRONG_SIGNAL
  >= 250_000                              -> ALERT
  else                                    -> WATCH
"""
import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Tuple

log = logging.getLogger("repetition_accumulator")

# ---------------------------------------------------------------------------
# Import order_side_to_direction for dominant_direction (S2 — always present).
# ---------------------------------------------------------------------------
from parsers.order_side_classifier import order_side_to_direction


# ---------------------------------------------------------------------------
# Default DTE premium tiers — (T1_floor, T2_T3_floor)
# Keys are DTE upper bounds (inclusive).
# ---------------------------------------------------------------------------
_DEFAULT_DTE_PREMIUM_TIERS: Dict[int, Tuple[float, float]] = {
    7:    (50_000,    25_000),
    30:   (500_000,   100_000),
    90:   (1_000_000, 500_000),
    9999: (2_000_000, 1_000_000),
}


# ---------------------------------------------------------------------------
# ING-006: Aggression discount for passive (mid-spread) fills.
# Passive events contribute premium * _AGGRESSION_DISCOUNT to weighted_premium.
# Aggressive events contribute premium * 1.0 (full weight).
# Default 0.5 — hardcoded now; wire through ingestion_config in ING-002-CONFIG.
# Deliberation: PBE-Q2 (2026-05-03) — configurable deferred to ING-002-CONFIG.
# ---------------------------------------------------------------------------
_AGGRESSION_DISCOUNT: float = 0.5


# ---------------------------------------------------------------------------
# _DictEventWrapper — module-level; wraps raw dict ticks so attribute access
# works identically to OptionsFlowEvent objects throughout the accumulator.
# ---------------------------------------------------------------------------
class _DictEventWrapper:
    __slots__ = (
        "premium", "timestamp", "trade_type", "dte",
        "underlying_price", "order_side", "contract_type",
        "is_aggressive",   # ING-006
    )

    def __init__(self, d: dict) -> None:
        self.premium          = d.get("premium", 0.0)
        self.timestamp        = d.get("timestamp") or datetime.now(timezone.utc)
        self.trade_type       = d.get("trade_type", "")
        self.dte              = d.get("dte", 0)
        self.underlying_price = d.get("underlying_price", 0.0)
        self.order_side       = d.get("order_side", "UNKNOWN")
        self.contract_type    = d.get("contract_type", "")
        self.is_aggressive    = bool(d.get("is_aggressive", False))  # ING-006


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

    # ------------------------------------------------------------------
    # Core computed properties
    # ------------------------------------------------------------------

    @property
    def trade_count(self) -> int:
        return len(self.events)

    @property
    def total_premium(self) -> float:
        return sum(getattr(e, "premium", 0.0) for e in self.events)

    @property
    def weighted_premium(self) -> float:
        """
        ING-006: Aggression-weighted cumulative premium.

        Aggressive events (is_aggressive=True) contribute full premium.
        Passive events (is_aggressive=False) contribute premium * _AGGRESSION_DISCOUNT.

        Gate 2 (DTE-adjusted floor) in ingest_tick() evaluates this value,
        not total_premium, so a passive-only episode must accumulate 2× the
        floor to qualify at the default 0.5 discount.

        Example: 2 aggressive @ $40k + 2 passive @ $40k
          weighted = (40k*1.0 + 40k*1.0) + (40k*0.5 + 40k*0.5) = 80k + 40k = 120k
          total    = 160k
        """
        total = 0.0
        for e in self.events:
            prem = getattr(e, "premium", 0.0)
            if getattr(e, "is_aggressive", False):
                total += prem
            else:
                total += prem * _AGGRESSION_DISCOUNT
        return total

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

    @property
    def dominant_direction(self) -> str:
        """
        Premium-weighted direction across all events in the episode.
        """
        buy_prem = 0.0
        sell_prem = 0.0
        for e in self.events:
            d = order_side_to_direction(
                getattr(e, "order_side", "UNKNOWN"),
                getattr(e, "contract_type", self.contract_type),
            )
            if d == "REPEAT_BUY":
                buy_prem += getattr(e, "premium", 0.0)
            else:
                sell_prem += getattr(e, "premium", 0.0)
        return "REPEAT_BUY" if buy_prem >= sell_prem else "REPEAT_SELL"

    def summary_str(self) -> str:
        return (
            f"{self.ticker} {self.contract_type} ${self.strike:.0f} {self.expiry} "
            f"trades={self.trade_count} prem=${self.total_premium:,.0f}"
        )


class RepetitionAccumulator:
    """
    Apex L2 dual-window accumulator.

    ING-006: Gate 2 (DTE-adjusted floor) now evaluates ep.weighted_premium
    instead of ep.total_premium. Passive events are discounted by
    _AGGRESSION_DISCOUNT (default 0.5). See RepetitionEpisode.weighted_premium.
    """

    def __init__(
        self,
        window_minutes:       int   = 30,
        min_trades:           int   = 3,
        min_premium:          float = 50_000,
        signal_cooldown:      int   = 0,
        retrigger:            float = 50_000,
        retrigger_delta:      float = 50_000,
        # S4 params
        min_sweeps:           int   = 0,
        sweep_bypass_premium: float = 0.0,
        deep_otm_multiplier:  float = 1.0,   # ING-005: changed from 1.5 — see docstring
        dte_premium_tiers:    Optional[Dict[int, Tuple[float, float]]] = None,
        tier_map:             Optional[Dict[str, int]] = None,
    ):
        self.window          = timedelta(minutes=window_minutes)
        self.min_trades      = min_trades
        self.min_premium     = min_premium
        self.signal_cooldown = timedelta(minutes=signal_cooldown)
        self.retrigger_delta = retrigger_delta if retrigger_delta != 50_000 else retrigger

        # S4
        self.min_sweeps           = min_sweeps
        self.sweep_bypass_premium = sweep_bypass_premium
        self.deep_otm_multiplier  = deep_otm_multiplier
        self.dte_premium_tiers    = dte_premium_tiers or {}
        self._tier_map            = tier_map or {}
        self._tier_map_lock: threading.Lock = threading.Lock()
        self._max_dte_key: Optional[int] = (
            max(self.dte_premium_tiers) if self.dte_premium_tiers else None
        )

        self._episodes: dict = {}
        self._locks:    dict = {}

    # ------------------------------------------------------------------ #
    # Tier map injection
    # ------------------------------------------------------------------ #

    def set_tier_map(self, tier_map: Dict[str, int]) -> None:
        """Replace the internal tier map. Thread-safe."""
        with self._tier_map_lock:
            self._tier_map = tier_map

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
    # S4: DTE-adjusted minimum premium
    # ------------------------------------------------------------------ #

    def _get_episode_min_premium(self, ep: RepetitionEpisode) -> float:
        """Return the DTE-adjusted minimum premium floor for this episode."""
        if not self.dte_premium_tiers:
            return self.min_premium

        latest_dte = 0
        if ep.events:
            latest_dte = int(getattr(ep.events[-1], "dte", 0) or 0)

        with self._tier_map_lock:
            tier = self._tier_map.get(ep.ticker, 1)
        col  = 0 if tier == 1 else 1

        for dte_max in sorted(self.dte_premium_tiers):
            if latest_dte <= dte_max:
                return self.dte_premium_tiers[dte_max][col]

        log.debug(
            "_get_episode_min_premium: DTE %d exceeds all tier keys %s for %s; "
            "falling back to highest-key bucket (key=%d).",
            latest_dte, sorted(self.dte_premium_tiers), ep.ticker, self._max_dte_key,
        )
        return self.dte_premium_tiers[self._max_dte_key][col]  # type: ignore[index]

    # ------------------------------------------------------------------ #
    # S4: OTM classification
    # ------------------------------------------------------------------ #

    @staticmethod
    def _classify_otm(strike: float, underlying_price: float) -> str:
        """
        Classify contract OTM percentage band.

        Returns: ATM | STANDARD_OTM | DEEP_OTM | UNKNOWN

        NOTE (ING-005): _classify_otm() is retained for ING-007 pattern scoring
        and signal metadata enrichment. With deep_otm_multiplier=1.0 (default),
        DEEP_OTM no longer triggers a premium floor penalty in production.
        """
        if underlying_price <= 0:
            return "UNKNOWN"
        otm_pct = abs(strike - underlying_price) / underlying_price
        if otm_pct <= 0.02:
            return "ATM"
        if otm_pct <= 0.12:
            return "STANDARD_OTM"
        return "DEEP_OTM"

    # ------------------------------------------------------------------ #
    # S4: Sweep bypass check
    # ------------------------------------------------------------------ #

    def _is_single_whale_sweep(self, ep: RepetitionEpisode) -> bool:
        """Returns True when the single-event sweep bypass should fire."""
        if self.sweep_bypass_premium <= 0:
            return False
        if len(ep.events) != 1:
            return False
        event_trade_type = getattr(ep.events[-1], "trade_type", "") or ""
        if event_trade_type.upper() != "SWEEP":
            return False
        return ep.total_premium >= self.sweep_bypass_premium

    # ------------------------------------------------------------------ #
    # ingest_tick: Gate-1 only, no cooldown
    # ------------------------------------------------------------------ #

    async def ingest_tick(self, ev) -> Optional[RepetitionEpisode]:
        """
        Gate-1 only: returns ep whenever the episode meets all qualification gates.

        Gates applied (in order):
          1. min_trades         — episode event count >= min_trades
          2. DTE-adjusted floor — ep.weighted_premium >= _get_episode_min_premium(ep)
                                  ING-006: evaluates WEIGHTED premium (passive events
                                  discounted by _AGGRESSION_DISCOUNT=0.5), not total.
          3. Deep OTM multiplier— dormant at default deep_otm_multiplier=1.0 (ING-005)
          4. min_sweeps         — with whale-sweep bypass
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

            ev_wrapped = _DictEventWrapper(ev) if isinstance(ev, dict) else ev

            ep.events = [
                e for e in ep.events
                if getattr(e, "timestamp", ev_ts) >= (ev_ts - self.window)
            ]
            ep.events.append(ev_wrapped)
            ep.last_seen  = ev_ts
            if ep.first_seen is None:
                ep.first_seen = ev_ts

            # ── Gate 1: min_trades ────────────────────────────────────────────
            if ep.trade_count < self.min_trades:
                return None

            # ── Gate 2: DTE-adjusted premium floor (ING-006: weighted_premium) ─
            effective_min_prem = self._get_episode_min_premium(ep)

            # ── Gate 3: Deep OTM multiplier (dormant at default 1.0) ─────────
            strike_val = float(ep.strike)
            raw_underlying = getattr(ev_wrapped, "underlying_price", 0.0)
            try:
                underlying_px = float(raw_underlying) if isinstance(raw_underlying, (int, float)) else 0.0
            except (TypeError, ValueError):
                underlying_px = 0.0

            otm_band = self._classify_otm(strike_val, underlying_px)

            if self.deep_otm_multiplier > 1.0 and otm_band == "DEEP_OTM":
                deep_floor = effective_min_prem * self.deep_otm_multiplier
                # ING-006: evaluate against weighted_premium
                if ep.weighted_premium < deep_floor:
                    return None
            else:
                # ING-006: Gate 2 evaluates weighted_premium, not total_premium
                if ep.weighted_premium < effective_min_prem:
                    return None

            # ── Gate 4: min_sweeps (with whale-sweep bypass) ─────────────────
            if self.min_sweeps > 0:
                is_bypass = self._is_single_whale_sweep(ep)
                if not is_bypass:
                    sweep_count = sum(
                        1 for e in ep.events
                        if (getattr(e, "trade_type", "") or "").upper() == "SWEEP"
                    )
                    if sweep_count < self.min_sweeps:
                        return None

            return ep

    # ------------------------------------------------------------------ #
    # get_signal: cooldown gate only
    # ------------------------------------------------------------------ #

    async def get_signal(
        self,
        ts: datetime,
        ep: Optional[RepetitionEpisode],
    ) -> Optional[RepetitionEpisode]:
        """Cooldown gate only. Returns ep if eligible to signal, else None."""
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
    # ingest: backward-compat shim
    # ------------------------------------------------------------------ #

    async def ingest(self, ev) -> Optional[RepetitionEpisode]:
        """Backward-compat: calls ingest_tick then get_signal."""
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

    def get_alert_level(self, ep: RepetitionEpisode) -> str:
        """Return the alert level for a qualifying episode."""
        prem = ep.total_premium
        if prem >= 2_000_000:
            return "CONVICTION"
        if getattr(ep, "is_accelerating", False) and prem >= 500_000:
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
