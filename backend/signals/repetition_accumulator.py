# ============================================================================
# repetition_accumulator.py
#
# DEPLOY NOTE (ING-006 / PBE-F1 — 2026-05-03):
#   Gate 2 (DTE-adjusted premium floor) now evaluates ep.weighted_premium
#   instead of ep.total_premium. Passive events (is_aggressive=False) are
#   discounted by _AGGRESSION_DISCOUNT (default 0.5), meaning a passive-only
#   episode must accumulate 2x the floor in raw premium to qualify.
#
#   PRODUCTION IMPACT: Signal volume WILL decrease on first deploy for any
#   episode dominated by MID-spread fills that previously cleared the floor
#   on total_premium alone. This is intentional — mid-prints represent
#   uncertain directional intent. Monitor signal emit rate on day 1.
#
#   ROLLBACK: No runtime kill-switch exists. Full PR revert required if
#   signal volume drops unexpectedly. _AGGRESSION_DISCOUNT will be wired
#   through ingestion_config in ING-002-CONFIG (future sprint) at which
#   point a config-level rollback will be available.
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

Alert level thresholds (S1 reconciliation):
  Thresholds were reconciled from pre-S1 state. See get_alert_level() for
  the full change-log from the old values.

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
  >= 1_000_000                            -> WHALE
  >= 500_000                              -> INSTITUTIONAL
  >= 100_000                              -> LARGE
  < 100_000                               -> RETAIL
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

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
# Only fields actually consumed by the accumulator are exposed.
# ---------------------------------------------------------------------------
class _DictEventWrapper:
    __slots__ = (
        "premium", "timestamp", "trade_type", "dte",
        "underlying_price", "order_side", "contract_type",
        "is_aggressive",   # ING-006
    )

    def __init__(self, d: dict) -> None:
        self.premium          = float(d.get("premium", 0.0))
        self.timestamp        = d.get("timestamp") or datetime.now(timezone.utc)
        self.trade_type       = d.get("trade_type", "")
        self.dte              = int(d.get("dte", 0))
        self.underlying_price = d.get("underlying_price", 0.0)
        self.order_side       = d.get("order_side", "UNKNOWN")
        self.contract_type    = d.get("contract_type", "")
        self.is_aggressive    = bool(d.get("is_aggressive", False))  # ING-006


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
        not total_premium, so a passive-only episode must accumulate 2x the
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

    @property
    def dominant_direction(self) -> str:
        """
        Premium-weighted direction across all events in this episode.

        An episode dominated by SELL PUT premium resolves REPEAT_BUY even
        if the last tick was a passive mid-print (Session 10 resolution).
        """
        buy_prem = sell_prem = 0.0
        for e in self.events:
            d = order_side_to_direction(
                getattr(e, "order_side", "UNKNOWN"),
                getattr(e, "contract_type", "CALL"),
            )
            if d == "REPEAT_BUY":
                buy_prem += getattr(e, "premium", 0.0)
            else:
                sell_prem += getattr(e, "premium", 0.0)
        return "REPEAT_BUY" if buy_prem >= sell_prem else "REPEAT_SELL"


class RepetitionAccumulator:
    """
    Accumulates OptionsFlowEvent ticks into RepetitionEpisode objects.

    An episode represents repeated conviction activity on the same
    (ticker, contract_type, strike, expiry) key within the signal window.

    Constructor parameters
    ----------------------
    window_minutes : int
        Sliding window for episode event retention (signal path).
    min_trades : int
        Gate 1 — minimum event count before an episode emits.
    deep_otm_multiplier : float
        Gate 3 — premium floor multiplier for deep-OTM (>12%) contracts.
        Default 1.0 (dormant). Activate in ING-005 config.
    dte_premium_tiers : list
        Gate 2 tier table. Each entry: (max_dte, {tier: floor}).
        See _DEFAULT_DTE_PREMIUM_TIERS.
    min_sweeps : int
        Gate 4 — minimum sweep events required. Whale-sweep bypass applies
        when len(ep.events) == 1 AND trade_type=SWEEP AND premium >= 500k.

    Band configurability note (ING-005):
    The old _otm_bands constructor parameter accepted a dict but was
    but never read — a silent no-op that would mislead callers into thinking
    the bands were configurable. Removed. If band configurability is needed
    in a future sprint, _classify_otm must be updated to consume it.

    ING-006: Gate 2 (DTE-adjusted floor) now evaluates ep.weighted_premium
    instead of ep.total_premium. Passive events are discounted by
    _AGGRESSION_DISCOUNT (default 0.5). See RepetitionEpisode.weighted_premium.
    See DEPLOY NOTE at the top of this file for production impact.
    """

    def __init__(
        self,
        window_minutes: int = 30,
        min_trades: int = 3,
        deep_otm_multiplier: float = 1.0,
        dte_premium_tiers: Optional[List] = None,
        min_sweeps: int = 2,
    ) -> None:
        self.window_minutes      = window_minutes
        self.min_trades          = min_trades
        self.deep_otm_multiplier = deep_otm_multiplier
        self.min_sweeps          = min_sweeps
        self._dte_tiers          = dte_premium_tiers or _DEFAULT_DTE_PREMIUM_TIERS
        self._episodes: Dict[str, RepetitionEpisode] = {}
        self._tier_map: Dict[str, int] = {}
        self._signal_last_emit: Dict[str, datetime] = {}

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def flush_emit_cache(self) -> None:
        """Clear debounce cache. Call at stream startup (S1 spec)."""
        self._signal_last_emit.clear()

    def set_tier_map(self, tier_map: Dict[str, int]) -> None:
        """Inject symbol -> tier (1/2/3) map for DTE floor selection."""
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

    def _get_episode_min_premium(
        self, ep: RepetitionEpisode
    ) -> float:
        """
        Return the DTE-adjusted premium floor for this episode.

        Uses the latest event's DTE to select from _dte_tiers.
        Tier is resolved from _tier_map; defaults to tier 2 if unknown.
        """
        if not ep.events:
            return 0.0
        latest = ep.events[-1]
        dte    = int(getattr(latest, "dte", 0) or 0)
        ticker = getattr(latest, "ticker", "") or ""
        tier   = self._tier_map.get(ticker, 2)

        for max_dte, floors in self._dte_tiers:
            if dte <= max_dte:
                return float(floors.get(tier, floors.get(2, 0.0)))
        # 91+ DTE
        return float(_DEFAULT_DTE_PREMIUM_TIERS_91_PLUS.get(tier, 1_000_000))

    def _classify_otm(self, ev) -> str:
        """
        Classify contract OTM band.

        Returns: ATM | OTM | DEEP_OTM | UNKNOWN

        ATM: abs(strike - underlying_price) / underlying_price <= 0.02
        OTM: 2–12%
        DEEP_OTM: > 12%
        UNKNOWN: underlying_price == 0 (no classification attempted)
        """
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

    def get_alert_level(self, total_premium: float) -> str:
        """Map episode total_premium to alert tier."""
        if total_premium >= 2_000_000:
            return "CONVICTION"
        if total_premium >= 1_000_000:
            return "WHALE"
        if total_premium >= 500_000:
            return "INSTITUTIONAL"
        if total_premium >= 100_000:
            return "LARGE"
        return "RETAIL"

    # ------------------------------------------------------------------
    # Core ingest
    # ------------------------------------------------------------------

    async def ingest_tick(self, ev) -> Optional[RepetitionEpisode]:
        """
        Ingest one OptionsFlowEvent tick and return an emitted episode or None.

        The event is wrapped in _DictEventWrapper if it arrives as a dict.
        Old events outside the signal window are evicted before gate evaluation.

        Gates applied (in order):
          1. min_trades         — episode event count >= min_trades
          2. DTE-adjusted floor — ep.weighted_premium >= _get_episode_min_premium(ep)
                                  ING-006: evaluates WEIGHTED premium (passive events
                                  discounted by _AGGRESSION_DISCOUNT=0.5), not total.
          3. Deep OTM multiplier— if OTM% > 12% AND deep_otm_multiplier > 1.0,
                                  floor is multiplied by deep_otm_multiplier.
                                  With default deep_otm_multiplier=1.0 (ING-005),
                                  Gate 3 is dormant.
          4. min_sweeps         — if trade_type=SWEEP, count must meet min_sweeps.
                                  Bypassed when len(ep.events)==1 AND
                                  trade_type=SWEEP AND premium >= 500k.
        """
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

        # ── Gate 1: min_trades ───────────────────────────────────────────────
        if ep.trade_count < self.min_trades:
            return None

        # ── Gate 2: DTE-adjusted premium floor (ING-006: weighted_premium) ─
        effective_min_prem = self._get_episode_min_premium(ep)

        # ── Gate 3: Deep OTM multiplier ──────────────────────────────────────
        # OTM classification uses the latest event's underlying_price.
        # Guard against non-numeric values (e.g. MagicMock in tests) by
        # using isinstance before float() to avoid TypeError in the hot path.
        otm_band = "UNKNOWN"
        try:
            up_raw = getattr(ev, "underlying_price", 0.0)
            if isinstance(up_raw, (int, float)) and up_raw > 0:
                otm_band = self._classify_otm(ev)
        except Exception:  # pragma: no cover
            pass

        if self.deep_otm_multiplier > 1.0 and otm_band == "DEEP_OTM":  # dormant at default 1.0 — ING-005/SA-Q1
            deep_floor = effective_min_prem * self.deep_otm_multiplier
            # ING-006: evaluate against weighted_premium
            if ep.weighted_premium < deep_floor:
                return None
        else:
            # ING-006: Gate 2 evaluates weighted_premium, not total_premium
            if ep.weighted_premium < effective_min_prem:
                return None

        # ── Gate 4: min_sweeps (with whale-sweep bypass) ─────────────────────
        # Sweep bypass: len(ep.events)==1 AND trade_type=SWEEP AND prem>=500k.
        # len(ep.events) is episode event count (OptionsFlowEvent objects),
        # NOT fill_count within a single tick (Issue 7 deliberation, 2026-04-30).
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
