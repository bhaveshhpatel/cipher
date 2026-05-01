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
  - Deep OTM applies deep_otm_multiplier (default 1.5x) to the floor
  - Sweep bypass: len(ep.events)==1 AND SWEEP AND premium >= sweep_bypass_premium
  - min_sweeps gate (in addition to min_trades)
  - dominant_direction property on RepetitionEpisode (premium-weighted)
  - tier_map injection for DTE-floor tier lookup

Alert levels (S1 reconciled):
  >= 2_000_000                            -> CONVICTION
  is_accelerating AND >= 500_000          -> CONVICTION
  >= 500_000                              -> STRONG_SIGNAL
  >= 100_000                              -> ALERT
  else                                    -> WATCH
"""
import asyncio
import logging
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
# _DictEventWrapper — module-level; wraps raw dict ticks so attribute access
# works identically to OptionsFlowEvent objects throughout the accumulator.
# Defined here (not inside ingest_tick) to avoid a new class object being
# allocated on every dict-type tick in the hot path. (Finding 7)
# ---------------------------------------------------------------------------
class _DictEventWrapper:
    __slots__ = (
        "premium", "timestamp", "trade_type", "dte",
        "underlying_price", "order_side", "contract_type",
    )

    def __init__(self, d: dict) -> None:
        self.premium          = d.get("premium", 0.0)
        self.timestamp        = d.get("timestamp") or datetime.now(timezone.utc)
        self.trade_type       = d.get("trade_type", "")
        self.dte              = d.get("dte", 0)
        self.underlying_price = d.get("underlying_price", 0.0)
        self.order_side       = d.get("order_side", "UNKNOWN")
        self.contract_type    = d.get("contract_type", "")


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

        Uses order_side_to_direction() per event so that:
          - SELL + PUT  -> REPEAT_BUY  (PASSIVE_BULLISH)
          - BUY  + CALL -> REPEAT_BUY  (DIRECTIONAL_LONG)
          - BUY  + PUT  -> REPEAT_SELL (DIRECTIONAL_SHORT)
          - SELL + CALL -> REPEAT_SELL (PASSIVE_BEARISH)

        Episodes dominated by SELL PUT premium correctly resolve to
        REPEAT_BUY even if the last tick is a weak mid-print.
        UNKNOWN order_side falls back to contract-type convention.
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

    Args (original, unchanged):
        window_minutes:       Rolling window for signal path event pruning.
        min_trades:           Minimum ticks to cross Gate-1.
        min_premium:          Fallback minimum cumulative premium (used when
                              dte_premium_tiers is empty).
        signal_cooldown:      Minutes to suppress re-signals.
        retrigger / retrigger_delta: Legacy compat params, not used by ingest_tick.

    Args (S4 additions):
        min_sweeps:           Minimum SWEEP-type events in the episode to qualify.
                              0 = disabled (no sweep gate).
        sweep_bypass_premium: If > 0, a single-event episode (len==1) of type
                              SWEEP with total_premium >= this value bypasses
                              the min_sweeps gate entirely.
                              NOTE: len(ep.events)==1 counts OptionsFlowEvent
                              objects in this episode, NOT fill_count within a
                              single tick. (Issue 7 resolution — Architect +
                              Principal Engineer deliberation, April 30 2026)
        deep_otm_multiplier:  Multiplier applied to the effective_min_premium when
                              OTM% > 12%. Default 1.5.
        dte_premium_tiers:    Dict[int, Tuple[float, float]] mapping DTE upper-bound
                              (inclusive) to (T1_floor, T2_T3_floor). If empty,
                              min_premium is used as fallback.
        tier_map:             Optional dict mapping ticker -> tier (1, 2, or 3).
                              Injected by the stream worker after registry readiness.

    NOTE — `otm_band` param removed (Finding 1, panel deliberation May 1 2026):
        The original constructor accepted `otm_band: Tuple[float, float]` but
        `_classify_otm` is a static method that hardcodes the 0.02 / 0.12
        thresholds per the spec (Issue 6 resolution). `otm_band` was stored
        but never read — a silent no-op that would mislead callers into thinking
        the bands were configurable. Removed. If band configurability is needed
        in a future sprint, _classify_otm must be updated to consume it.
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
        deep_otm_multiplier:  float = 1.5,
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

        self._episodes: dict = {}
        self._locks:    dict = {}

    # ------------------------------------------------------------------ #
    # Tier map injection (called by stream worker after registry warms)
    # ------------------------------------------------------------------ #

    def set_tier_map(self, tier_map: Dict[str, int]) -> None:
        """Replace the internal tier map. Thread-safe for non-async callers."""
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
        """
        Return the DTE-adjusted minimum premium floor for this episode.

        Uses the latest event's DTE and the episode ticker's tier.
        Falls back to self.min_premium when dte_premium_tiers is empty.

        Tier lookup:
          tier == 1  -> column 0 (T1 floor — higher, stricter)
          tier != 1  -> column 1 (T2/T3 floor — lower, more permissive)

        Unknown-tier default: T1 (strict) — column 0.
        Deliberation decision (panel, May 1 2026 — Finding 2):
          Unknown tickers have no registry-validated volume or tier.
          Defaulting to T2/T3 (lenient) would let low-float noise stocks
          qualify more easily than large-caps during registry warmup.
          T1 (strict) is the safer production default; set_tier_map() is
          called once registry is ready to assign the correct tier.
        """
        if not self.dte_premium_tiers:
            return self.min_premium

        latest_dte = 0
        if ep.events:
            latest_dte = int(getattr(ep.events[-1], "dte", 0) or 0)

        tier = self._tier_map.get(ep.ticker, 1)  # default T1 (strict)
        col  = 0 if tier == 1 else 1

        for dte_max in sorted(self.dte_premium_tiers):
            if latest_dte <= dte_max:
                return self.dte_premium_tiers[dte_max][col]

        # latest_dte exceeds all explicit keys — use the highest-key bucket
        highest_key = max(self.dte_premium_tiers)
        return self.dte_premium_tiers[highest_key][col]

    # ------------------------------------------------------------------ #
    # S4: OTM classification
    # ------------------------------------------------------------------ #

    @staticmethod
    def _classify_otm(strike: float, underlying_price: float) -> str:
        """
        Classify contract OTM percentage band.

        Returns one of:
          'ATM'          — abs(strike - underlying_price) / underlying_price <= 0.02
          'STANDARD_OTM' — 2% < otm_pct <= 12%
          'DEEP_OTM'     — otm_pct > 12%
          'UNKNOWN'      — underlying_price <= 0 (no classification attempted)

        ATM definition: abs(strike - underlying) / underlying <= 0.02
        (±2% of underlying price — Issue 6 resolution, Architect + Principal
        Engineer deliberation April 30 2026. Expressed as fraction of underlying,
        not absolute dollar, so it works correctly across all underlying price
        regimes including high-price names like NVDA at $900+.)

        Thresholds (0.02 and 0.12) are intentionally hardcoded per spec.
        They are NOT configurable via constructor — see Finding 1 note in
        RepetitionAccumulator docstring.
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
        """
        Returns True when the single-event sweep bypass should fire.

        Conditions (ALL must be true):
          1. sweep_bypass_premium > 0  (bypass is enabled)
          2. len(ep.events) == 1       (exactly one OptionsFlowEvent in this episode;
                                        NOT fill_count within a single tick — Issue 7)
          3. trade_type == 'SWEEP'
          4. ep.total_premium >= sweep_bypass_premium

        When True, the min_sweeps requirement is waived for this episode.
        """
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
          2. DTE-adjusted floor — episode total_premium >= _get_episode_min_premium(ep)
          3. Deep OTM multiplier— if OTM% > 12%, floor is multiplied by deep_otm_multiplier
          4. min_sweeps         — episode must contain >= min_sweeps SWEEP events
                                  (bypassed when _is_single_whale_sweep returns True)

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

            # Wrap dict events so .premium / .timestamp / .trade_type attributes work.
            # _DictEventWrapper is defined at module level (not inline here) to avoid
            # allocating a new class object on every hot-path dict tick. (Finding 7)
            ev_wrapped = _DictEventWrapper(ev) if isinstance(ev, dict) else ev

            ep.events = [
                e for e in ep.events
                if getattr(e, "timestamp", ev_ts) >= cutoff
            ]
            ep.events.append(ev_wrapped)
            ep.last_seen  = ev_ts
            if ep.first_seen is None:
                ep.first_seen = ev_ts

            # ── Gate 1: min_trades ────────────────────────────────────────
            if ep.trade_count < self.min_trades:
                return None

            # ── Gate 2: DTE-adjusted premium floor ───────────────────────
            effective_min_prem = self._get_episode_min_premium(ep)

            # ── Gate 3: Deep OTM multiplier ───────────────────────────────
            # OTM classification uses the latest event's underlying_price.
            # When underlying_price == 0: UNKNOWN -> no OTM classification,
            # standard floor applies. (Issue 6 resolution)
            strike_val    = float(ep.strike)
            underlying_px = float(
                getattr(ev_wrapped, "underlying_price", 0.0) or 0.0
            )
            otm_band = self._classify_otm(strike_val, underlying_px)

            if self.deep_otm_multiplier > 1.0 and otm_band == "DEEP_OTM":
                deep_floor = effective_min_prem * self.deep_otm_multiplier
                if ep.total_premium < deep_floor:
                    return None
            else:
                if ep.total_premium < effective_min_prem:
                    return None

            # ── Gate 4: min_sweeps (with whale-sweep bypass) ─────────────
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
    # Alert level (S1 reconciled thresholds)
    # ------------------------------------------------------------------ #

    def get_alert_level(self, ep: RepetitionEpisode) -> str:
        """
        Alert level bands (S1 reconciliation):
          CONVICTION    >= 2_000_000
          CONVICTION    is_accelerating AND >= 500_000
          STRONG_SIGNAL >= 500_000
          ALERT         >= 100_000
          WATCH         < 100_000
        """
        prem = ep.total_premium
        if prem >= 2_000_000:
            return "CONVICTION"
        if getattr(ep, "is_accelerating", False) and prem >= 500_000:
            return "CONVICTION"
        if prem >= 500_000:
            return "STRONG_SIGNAL"
        if prem >= 100_000:
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
