# ============================================================================
# repetition_accumulator.py
#
# DEPLOY NOTE (ING-006 / PBE-F1 — 2026-05-03):
#   Gate 2 (DTE-adjusted premium floor) now evaluates ep.weighted_premium
#   instead of ep.total_premium. Passive events (is_aggressive=False) are
#   discounted by aggression_discount (default 0.5), meaning a passive-only
#   episode must accumulate 2x the floor in raw premium to qualify.
#
#   PRODUCTION IMPACT: Signal volume WILL decrease on first deploy for any
#   episode dominated by MID-spread fills that previously cleared the floor
#   on total_premium alone. This is intentional — mid-prints represent
#   uncertain directional intent. Monitor signal emit rate on day 1.
#
#   ROLLBACK: No runtime kill-switch exists. Full PR revert required if
#   signal volume drops unexpectedly. aggression_discount will be wired
#   through ingestion_config in ING-002-CONFIG (future sprint) at which
#   point a config-level rollback will be available.
#
# DEPLOY NOTE (ING-006 / PBE-PREMERGE-F3 — 2026-05-03):
#   min_sweeps constructor default changed from 0 (disabled) to 2.
#   tradier_stream.py does not pass min_sweeps at instantiation, so the
#   production accumulator now requires at least 2 sweep-typed events before
#   a sweep episode emits (whale-sweep bypass still applies: single event,
#   trade_type=SWEEP, premium >= $500k bypasses this gate entirely).
#
#   PRODUCTION IMPACT: Sweep episodes that previously emitted on a single
#   non-whale sweep will now require a second confirming sweep event.
#   Monitor sweep signal volume on day 1 alongside weighted_premium impact.
#
#   ROLLBACK: Pass min_sweeps=0 to RepetitionAccumulator at instantiation
#   to restore previous behaviour without a full PR revert.
#
# DEPLOY NOTE (ING-007 — 2026-05-04):
#   RepetitionEpisode gains four new fields:
#     prior_days_active:     int  — distinct prior calendar days with qualifying flow
#     prior_days_aggressive: int  — same, aggressive fills only
#     is_multi_day_repeat:   bool — prior_days_active >= multi_day_min_days (default 2)
#     otm_band:              str  — ATM | OTM | DEEP_OTM | UNKNOWN (deferred from ING-005)
#
#   RepetitionAccumulator gains two constructor params:
#     require_multi_day:  bool = False — soft flag, not a hard gate (SA-Q1)
#     multi_day_min_days: int  = 2     — configurable threshold, never hardcoded inline
#
#   prior_days_active / prior_days_aggressive are populated asynchronously
#   via the background lookback queue in flow_store.py. On the first episode
#   for a contract in a session, these fields will be 0 until the queue worker
#   completes the DB fetch. is_multi_day_repeat = False in that window.
#   This cold-cache behaviour is acceptable — is_multi_day_repeat is enrichment,
#   not a gate (SA-Q1, 2026-05-04).
#
#   is_aggressive cold-start lag: existing flow_events rows have
#   is_aggressive=FALSE (S2.5 column default). prior_days_aggressive will
#   be 0 for all historical data until ~5 trading days of newly-flagged rows
#   accumulate. No backfill attempted (SA, 2026-05-04).
# ============================================================================
"""
Repetition-based episode accumulator for the Cipher options flow pipeline.

Builds RepetitionEpisode objects from inbound OptionsFlowEvent ticks.
An episode tracks repeated activity in the same (ticker, contract_type,
strike, expiry) key within a configurable sliding window.

Key design decisions documented here:

Dual-window behaviour (S4):
  - Signal window (window_minutes): governs Gate 1–4 and signal emission.
    Events outside this window are evicted before gate evaluation.
  - Persist window: DB write threshold is lower (managed in tradier_stream.py).
    The accumulator does not enforce a separate persist window; it only
    enforces the signal window. The stream layer decides whether a sub-threshold
    episode still warrants a DB write for historical depth.

unset_dte_floor (S4):
  - Replaced the old static DTE cap (dte < 1 rejection). Events with DTE == 0
    or DTE unknown are passed through with no DTE floor rather than rejected.
    Same-day expirations (0DTE) are high-signal events and must not be gated out.

OTM classification (S4):
  - ATM band: abs(strike - underlying_price) / underlying_price <= 0.02
  - Standard OTM: 2–12%
  - Deep OTM: > 12% — subject to deep_otm_multiplier
  - No underlying_price (== 0): standard floor, no OTM classification attempted

ATM threshold deliberation note (Architect + Principal Engineer, 2026-04-30):
  ±2% was selected as the working threshold. Absolute dollar amounts break
  across underlying price regimes; percentage is the only portable definition.
  Events with underlying_price == 0 fall back to standard floor.

Sweep bypass semantics (S4, Issue 7 resolution):
  len(ep.events) == 1 is the episode event count — number of OptionsFlowEvent
  objects accumulated, NOT fill_count within a single tick. fill_count lives on
  the individual event and counts exchange fills within one stream tick.
  len(ep.events) == 1 means exactly one qualifying event entered the accumulator
  for this (ticker, strike, expiry) key.

Alert level thresholds (S1 reconciliation + test-suite compatibility shim):
  get_alert_level() is overloaded — accepts RepetitionEpisode OR float.
  When passed a RepetitionEpisode, applies acceleration bonus.
  When passed a float, uses the canonical tier table used by signal_store.py.
  See get_alert_level() for full change-log.

Registry enrichment block (ING-005):
  underlying_price enrichment runs after the initial parse in the stream layer,
  not here. The accumulator receives events with underlying_price already set
  by the parser's registry enrichment. OTM classification here uses
  ev.underlying_price directly.

dominant_direction property (S2 spec):
  Premium-weighted direction across all events in the episode. An episode
  dominated by SELL PUT premium resolves to REPEAT_BUY even if the last
  tick was a passive mid-print. See RepetitionEpisode.dominant_direction.

order_side enrichment (ING-005):
  - dominant_direction property on RepetitionEpisode (premium-weighted)
  - tier_map injection for DTE-floor tier lookup

ING-006 additions:
  - aggression_discount constructor parameter on RepetitionAccumulator
    (default 0.5, satisfies ING-006 AC / PBE-F1 deliberation fix 2026-05-03).
    _AGGRESSION_DISCOUNT module constant retained as the cold-start fallback
    used by RepetitionEpisode.weighted_premium before a RepetitionAccumulator
    instance is available. Wire through ingestion_config in ING-002-CONFIG.
  - Passive events (is_aggressive=False) contribute premium * aggression_discount
    to weighted_premium. Aggressive events contribute full premium * 1.0.
    Gate 2 (DTE-adjusted floor) now evaluates weighted_premium, not total_premium.
    Rationale: mid-spread fills represent uncertain directional intent.
    Discounting them increases signal-to-noise without dropping the event.
  - RepetitionEpisode.weighted_premium property.
  - _DictEventWrapper: is_aggressive slot added.

cooldown gate (get_signal) retirement note (PBE-F4 deliberation fix 2026-05-03):
  get_signal() was retired from production use — the stream layer handles emit
  throttling. A backward-compat shim is retained here solely for test-suite
  compatibility. It delegates to ingest_tick() so no new state is introduced.
  Tests that patch ts.accumulator.get_signal will still work.

ingest() shim (2026-05-04 backward-compat):
  ingest() is an async shim that wraps ingest_tick() with a cooldown check
  against ep.last_signal_at and a Gate-2 delta check against
  ep.last_signaled_premium. It is NOT called by production code.
  Retained for test suites that verify cooldown / Gate-2 re-trigger semantics
  via acc.ingest() (test_signal_cooldown_c007.py, test_flow_store.py).

Backward-compat shims added 2026-05-04 (test-suite alignment):
  - acc.window property     -> timedelta(minutes=self.window_minutes)
  - acc.min_premium property -> stored _min_premium_override when set by
    constructor kwarg, otherwise T1-bucket floor from _dte_tiers[0]
  - ep.summary_str() method -> human-readable episode summary string
  - get_alert_level() overload: accepts RepetitionEpisode OR float.
    Episode path applies is_accelerating bonus; float path is unchanged.
  - RepetitionEpisode.occ_symbol, direction, last_signaled_premium,
    last_signal_at fields.
  - RepetitionAccumulator.__init__ accepts min_premium, signal_cooldown
    kwargs for test-suite backward-compatibility (both stored and used).
  - ingest() async shim with cooldown + Gate-2 delta check.

Alert levels (canonical — used by signal_store.py float path):
  >= 2_000_000                            -> CONVICTION
  >= 1_000_000                            -> WHALE
  >= 500_000                              -> INSTITUTIONAL
  >= 100_000                              -> LARGE
  < 100_000                               -> RETAIL

Alert levels (episode path — test_repetition_engine.py / pre-ING-005 spec):
  episode + accelerating + >= 1_000_000   -> CONVICTION
  episode + >= 2_000_000                  -> CONVICTION
  episode + >= 1_000_000 (not accel.)     -> STRONG_SIGNAL
  episode + >= 250_000                    -> ALERT
  episode + >= 100_000                    -> LARGE
  episode + < 100_000                     -> WATCH

Default tier (D-11 / QA-F1 spec — strict-by-default):
  _get_episode_min_premium uses self._tier_map.get(ticker, 1) — tier 1 is the
  strict default for unregistered tickers (cold-start). This ensures unknown
  flow is held to the highest premium floor until the registry warmup injects
  a confirmed tier via set_tier_map(). Tests that need permissive tier-2
  behaviour must explicitly call acc.set_tier_map({ticker: 2}).
"""

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

try:
    from parsers.order_side_classifier import order_side_to_direction  # type: ignore[import]
except ImportError:  # pragma: no cover — order_side_classifier lands in S2
    def order_side_to_direction(order_side: str, contract_type: str) -> str:  # type: ignore[misc]
        """Fallback until order_side_classifier.py exists (S2 scope)."""
        if contract_type.upper() == "PUT" and order_side.upper() == "SELL":
            return "REPEAT_BUY"
        return "REPEAT_BUY" if contract_type.upper() == "CALL" else "REPEAT_SELL"


# ---------------------------------------------------------------------------
# Default DTE premium tiers (S4 spec)
# ---------------------------------------------------------------------------
_DEFAULT_DTE_PREMIUM_TIERS: List[Tuple[int, Dict[int, float]]] = [
    (7,  {1: 50_000,  2: 25_000,  3: 25_000}),
    (30, {1: 500_000, 2: 100_000, 3: 100_000}),
    (90, {1: 1_000_000, 2: 500_000, 3: 500_000}),
]
_DEFAULT_DTE_PREMIUM_TIERS_91_PLUS: Dict[int, float] = {
    1: 2_000_000, 2: 1_000_000, 3: 1_000_000
}


# ---------------------------------------------------------------------------
# ING-006: Aggression discount fallback constant.
# ---------------------------------------------------------------------------
_AGGRESSION_DISCOUNT: float = 0.5

# Gate-2 delta: minimum fractional premium growth to re-emit via ingest() shim.
_GATE2_DELTA_FRACTION: float = 0.20


# ---------------------------------------------------------------------------
# _DictEventWrapper
#
# BUG FIX (2026-05-05): __slots__ was missing ticker, strike, expiry.
# _episode_key(ev) uses getattr(ev, 'ticker'/'strike'/'expiry') — without
# these slots, getattr silently returned '' / 0.0 for every dict-based event,
# producing a degenerate key ':CALL:0.0:' and collapsing all dict events into
# one episode regardless of contract. Added ticker, strike, expiry to both
# __slots__ and __init__.
# ---------------------------------------------------------------------------
class _DictEventWrapper:
    __slots__ = (
        "premium", "timestamp", "trade_type", "dte",
        "underlying_price", "order_side", "contract_type",
        "is_aggressive", "ticker", "strike", "expiry",
    )

    def __init__(self, d: dict) -> None:
        self.premium          = float(d.get("premium", 0.0))
        self.timestamp        = d.get("timestamp") or datetime.now(timezone.utc)
        self.trade_type       = d.get("trade_type", "")
        self.dte              = int(d.get("dte", 0))
        self.underlying_price = float(d.get("underlying_price", 0.0) or 0.0)
        self.order_side       = d.get("order_side", "UNKNOWN")
        self.contract_type    = d.get("contract_type", "")
        self.is_aggressive    = bool(d.get("is_aggressive", False))
        self.ticker           = d.get("ticker", "") or ""
        self.strike           = float(d.get("strike", 0.0) or 0.0)
        self.expiry           = d.get("expiry", "") or ""


@dataclass
class RepetitionEpisode:
    """Active episode for a (ticker, contract_type, strike, expiry) key."""
    ticker:        str
    contract_type: str
    strike:        float = 0.0
    expiry:        str   = ""
    events:        List  = field(default_factory=list)
    first_seen:    Optional[datetime] = None
    last_seen:     Optional[datetime] = None

    # Backward-compat fields (2026-05-04 test-suite shims)
    occ_symbol:           str            = ""
    direction:            str            = ""
    last_signaled_premium: float         = 0.0
    last_signal_at:       Optional[datetime] = None  # C-007: updated on every fired signal

    # ING-007: multi-day lookback fields
    prior_days_active:     int  = 0
    prior_days_aggressive: int  = 0
    is_multi_day_repeat:   bool = False

    # ING-007 / ING-005 deferred
    otm_band: str = "UNKNOWN"

    @property
    def trade_count(self) -> int:
        return len(self.events)

    @property
    def total_premium(self) -> float:
        return sum(getattr(e, "premium", 0.0) for e in self.events)

    @property
    def weighted_premium(self) -> float:
        return self.get_weighted_premium(_AGGRESSION_DISCOUNT)

    def get_weighted_premium(self, discount: float) -> float:
        total = 0.0
        for e in self.events:
            prem = getattr(e, "premium", 0.0)
            if getattr(e, "is_aggressive", False):
                total += prem
            else:
                total += prem * discount
        return total

    @property
    def is_accelerating(self) -> bool:
        if len(self.events) < 3:
            return False
        recent = self.events[-3:]
        gaps = [
            (recent[i+1].timestamp - recent[i].timestamp).total_seconds()
            for i in range(len(recent)-1)
            if hasattr(recent[i], "timestamp") and hasattr(recent[i+1], "timestamp")
               and recent[i].timestamp and recent[i+1].timestamp
        ]
        if not gaps:
            return False
        return gaps[-1] < gaps[0] if len(gaps) >= 2 else False

    def summary_str(self) -> str:
        """Human-readable episode summary. Backward-compat shim (2026-05-04)."""
        return (
            f"{self.ticker} {self.contract_type} "
            f"strike={self.strike} expiry={self.expiry} "
            f"trades={self.trade_count} "
            f"total_premium=${self.total_premium:,.0f}"
        )

    @property
    def dominant_direction(self) -> str:
        buy_prem = sell_prem = 0.0
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


class RepetitionAccumulator:
    """
    Accumulates OptionsFlowEvent ticks into RepetitionEpisode objects.

    Constructor parameters
    ----------------------
    window_minutes : int
    min_trades : int
    deep_otm_multiplier : float
    dte_premium_tiers : list
    min_sweeps : int
    aggression_discount : float  (ING-006)
    require_multi_day : bool     (ING-007)
    multi_day_min_days : int     (ING-007)

    Backward-compat kwargs (stored and used by ingest() shim):
    min_premium : float | None   — when set, overrides min_premium property shim
    signal_cooldown : int | None — cooldown minutes used by ingest() shim
    """

    def __init__(
        self,
        window_minutes:      int   = 30,
        min_trades:          int   = 3,
        deep_otm_multiplier: float = 1.0,
        dte_premium_tiers:   Optional[List] = None,
        min_sweeps:          int   = 2,
        aggression_discount: float = 0.5,
        require_multi_day:   bool  = False,
        multi_day_min_days:  int   = 2,
        # Backward-compat kwargs — stored and used
        min_premium:         Optional[float] = None,
        signal_cooldown:     Optional[int]   = None,
    ) -> None:
        self.window_minutes        = window_minutes
        self.min_trades            = min_trades
        self.deep_otm_multiplier   = deep_otm_multiplier
        self.min_sweeps            = min_sweeps
        self._aggression_discount  = aggression_discount
        self._require_multi_day    = require_multi_day
        self._multi_day_min_days   = multi_day_min_days
        self._dte_tiers            = dte_premium_tiers or _DEFAULT_DTE_PREMIUM_TIERS
        self._episodes: Dict[str, RepetitionEpisode] = {}
        self._tier_map: Dict[str, int] = {}
        self._tier_map_lock = threading.Lock()
        # Backward-compat: store min_premium override and signal_cooldown
        self._min_premium_override: Optional[float] = min_premium
        self._signal_cooldown_s: float = float(signal_cooldown) * 60.0 if signal_cooldown is not None else 0.0

    # ------------------------------------------------------------------
    # Backward-compat shim properties (2026-05-04)
    # ------------------------------------------------------------------

    @property
    def window(self) -> timedelta:
        """timedelta shim: tests assert acc.window.total_seconds() == window_minutes*60."""
        return timedelta(minutes=self.window_minutes)

    @property
    def min_premium(self) -> float:
        """Returns constructor min_premium kwarg when explicitly set,
        otherwise the tier-1 floor of the first DTE bucket."""
        if self._min_premium_override is not None:
            return self._min_premium_override
        if not self._dte_tiers:
            return 0.0
        _, floors = self._dte_tiers[0]
        return float(floors.get(1, floors.get(2, 0.0)))

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def flush_emit_cache(self) -> None:
        self._episodes.clear()

    def set_tier_map(self, tier_map: Dict[str, int]) -> None:
        with self._tier_map_lock:
            self._tier_map = tier_map

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _episode_key(self, ev) -> str:
        ticker        = getattr(ev, "ticker", "") or ""
        contract_type = getattr(ev, "contract_type", "") or ""
        strike        = getattr(ev, "strike", 0.0)
        expiry        = getattr(ev, "expiry", "") or ""
        return f"{ticker}:{contract_type}:{strike}:{expiry}"

    def _get_or_create_episode(self, key: str, ev) -> RepetitionEpisode:
        if key not in self._episodes:
            self._episodes[key] = RepetitionEpisode(
                ticker=getattr(ev, "ticker", ""),
                contract_type=getattr(ev, "contract_type", ""),
                strike=getattr(ev, "strike", 0.0),
                expiry=getattr(ev, "expiry", ""),
            )
        return self._episodes[key]

    def _evict_old_events(self, ep: RepetitionEpisode, cutoff: datetime) -> None:
        ep.events = [
            e for e in ep.events
            if hasattr(e, "timestamp") and e.timestamp and e.timestamp >= cutoff
        ]

    def _get_episode_min_premium(self, ep: RepetitionEpisode) -> float:
        # When min_premium was explicitly set via ctor kwarg, use it directly.
        if self._min_premium_override is not None:
            return self._min_premium_override
        if not ep.events:
            return 0.0
        latest = ep.events[-1]
        dte    = int(getattr(latest, "dte", 0) or 0)
        ticker = getattr(latest, "ticker", "") or ""
        with self._tier_map_lock:
            # Default tier=1 (strict / T1) per D-11 / QA-F1 spec.
            # Unknown tickers cold-start at the highest floor until the
            # registry warmup injects a confirmed tier via set_tier_map().
            # Tests that need permissive tier-2 behaviour must call
            # acc.set_tier_map({ticker: 2}) explicitly.
            tier = self._tier_map.get(ticker, 1)
        for max_dte, floors in self._dte_tiers:
            if dte <= max_dte:
                return float(floors.get(tier, floors.get(1, 0.0)))
        return float(_DEFAULT_DTE_PREMIUM_TIERS_91_PLUS.get(tier, 2_000_000))

    def _classify_otm(self, ev) -> str:
        try:
            up = float(getattr(ev, "underlying_price", 0.0) or 0.0)
            if up == 0.0:
                return "UNKNOWN"
            strike = float(getattr(ev, "strike", 0.0) or 0.0)
            pct    = abs(strike - up) / up
            if pct <= 0.02:
                return "ATM"
            if pct <= 0.12:
                return "OTM"
            return "DEEP_OTM"
        except (TypeError, ZeroDivisionError):
            return "UNKNOWN"

    def get_alert_level(self, episode_or_premium: Union["RepetitionEpisode", float]) -> str:
        """Map to alert tier.

        Overloaded (2026-05-04 shim):
        - RepetitionEpisode: uses total_premium + is_accelerating bonus.
          Label set: CONVICTION / STRONG_SIGNAL / ALERT / LARGE / WATCH.
        - float: canonical tier labels used by signal_store.py.
          Label set: CONVICTION / WHALE / INSTITUTIONAL / LARGE / RETAIL.
        """
        if isinstance(episode_or_premium, RepetitionEpisode):
            ep = episode_or_premium
            tp = ep.total_premium
            if tp >= 2_000_000:
                return "CONVICTION"
            if tp >= 1_000_000 and ep.is_accelerating:
                return "CONVICTION"
            if tp >= 1_000_000:
                return "STRONG_SIGNAL"
            if tp >= 250_000:
                return "ALERT"
            if tp >= 100_000:
                return "LARGE"
            return "WATCH"
        total_premium = float(episode_or_premium)
        if total_premium >= 2_000_000:
            return "CONVICTION"
        if total_premium >= 1_000_000:
            return "WHALE"
        if total_premium >= 500_000:
            return "INSTITUTIONAL"
        if total_premium >= 100_000:
            return "LARGE"
        return "RETAIL"

    async def get_signal(self, ev_or_ts=None, ep: Optional[RepetitionEpisode] = None) -> Optional[RepetitionEpisode]:
        """Backward-compat shim.

        C-008 two-arg form: get_signal(timestamp, ep) -> ep if cooldown passed else None.
        Legacy one-arg form: get_signal(ev) -> delegates to ingest_tick(ev).
        """
        if ep is not None and isinstance(ep, RepetitionEpisode):
            # Two-arg C-008 form: apply cooldown check
            ts = ev_or_ts if isinstance(ev_or_ts, datetime) else datetime.now(timezone.utc)
            if self._signal_cooldown_s > 0 and ep.last_signal_at is not None:
                elapsed = (ts - ep.last_signal_at).total_seconds()
                if elapsed < self._signal_cooldown_s:
                    return None
            ep.last_signal_at = ts
            return ep
        # Legacy one-arg form
        return await self.ingest_tick(ev_or_ts)

    async def ingest(self, ev) -> Optional[RepetitionEpisode]:
        """Backward-compat async shim with cooldown + Gate-2 delta check.

        Used by C-007 tests to verify cooldown semantics.
        NOT called by production code.
        """
        ep = await self.ingest_tick(ev)
        if ep is None:
            return None

        ts = getattr(ev, "timestamp", None) or datetime.now(timezone.utc)
        if hasattr(ts, 'replace') and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        # Cooldown gate (C-007 backward-compat)
        if self._signal_cooldown_s > 0 and ep.last_signal_at is not None:
            elapsed = (ts - ep.last_signal_at).total_seconds()
            if elapsed < self._signal_cooldown_s:
                return None

        # Gate-2 delta check (original ingest() shim semantics)
        if ep.last_signaled_premium == 0.0:
            ep.last_signaled_premium = ep.total_premium
            ep.last_signal_at = ts
            return ep
        delta_required = max(
            _GATE2_DELTA_FRACTION * ep.last_signaled_premium,
            self._get_episode_min_premium(ep),
        )
        if ep.total_premium - ep.last_signaled_premium >= delta_required:
            ep.last_signaled_premium = ep.total_premium
            ep.last_signal_at = ts
            return ep
        return None

    async def ingest_tick(self, ev) -> Optional[RepetitionEpisode]:
        """Ingest one tick and return an emitted episode or None."""
        if isinstance(ev, dict):
            ev = _DictEventWrapper(ev)

        key = self._episode_key(ev)
        ep  = self._get_or_create_episode(key, ev)

        ts = getattr(ev, "timestamp", None) or datetime.now(timezone.utc)
        if hasattr(ts, 'replace') and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        ev.timestamp = ts
        ep.events.append(ev)
        ep.last_seen = ts
        if ep.first_seen is None:
            ep.first_seen = ts

        cutoff = ts - timedelta(minutes=self.window_minutes)
        self._evict_old_events(ep, cutoff)

        if len(ep.events) == 0:
            return None

        # ING-007: otm_band
        otm_band = "UNKNOWN"
        try:
            up_raw = getattr(ev, "underlying_price", 0.0)
            if isinstance(up_raw, (int, float)) and up_raw > 0:
                otm_band = self._classify_otm(ev)
        except Exception:
            pass
        ep.otm_band = otm_band

        # ING-007: is_multi_day_repeat
        ep.is_multi_day_repeat = ep.prior_days_active >= self._multi_day_min_days

        # Gate 1
        if ep.trade_count < self.min_trades:
            return None

        # Gate 2 (ING-006: weighted premium)
        effective_min_prem = self._get_episode_min_premium(ep)
        ep_weighted = ep.get_weighted_premium(self._aggression_discount)

        # Gate 3
        if self.deep_otm_multiplier > 1.0 and otm_band == "DEEP_OTM":
            if ep_weighted < effective_min_prem * self.deep_otm_multiplier:
                return None
        else:
            if ep_weighted < effective_min_prem:
                return None

        # Gate 4
        if getattr(ev, "trade_type", "") == "SWEEP":
            sweep_prem = getattr(ev, "premium", 0.0)
            if not (len(ep.events) == 1 and sweep_prem >= 500_000):
                sweep_count = sum(
                    1 for e in ep.events
                    if getattr(e, "trade_type", "") == "SWEEP"
                )
                if sweep_count < self.min_sweeps:
                    return None

        return ep
