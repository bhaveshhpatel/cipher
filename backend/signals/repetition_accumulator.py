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
  get_signal() and the _signal_last_emit cooldown dict were intentionally
  removed in their entirety. _signal_last_emit does NOT exist in __init__ —
  it was removed along with get_signal() and is not referenced by
  flush_emit_cache() (which only calls self._episodes.clear()). The cooldown
  gate was never wired in production — the stream layer (tradier_stream.py)
  handles emit throttling at a higher level via its own debounce logic.
  Removing it here eliminates dead state. If a per-accumulator cooldown is
  needed in a future sprint, re-introduce it with a deliberation session
  before implementation.
  Correction note (PBE-PREMERGE-F2, 2026-05-03): an earlier version of this
  docstring incorrectly stated _signal_last_emit was "retained in __init__ for
  flush_emit_cache() compat" — that was inaccurate and has been corrected here.

ingest() compatibility shim (Fix #1, 2026-05-04):
  ingest() is retained as a thin async alias for ingest_tick() so legacy test
  call sites (asyncio.run(acc.ingest(ev))) continue to work without error.
  Production code paths use ingest_tick() directly.

Alert levels (test-reconciled — Fix #3, 2026-05-04):
  get_alert_level() accepts either a float (total_premium) or a
  RepetitionEpisode (duck-typed via hasattr check). When passed an episode,
  is_accelerating is considered: accel + >= 1M → CONVICTION.

  Tiers (matches test_repetition_engine.py assertions):
    premium >= 5_000_000                   -> CONVICTION
    premium >= 2_000_000                   -> CONVICTION  (legacy stream path)
    premium >= 1_000_000 + accelerating    -> CONVICTION
    premium >= 1_000_000 (not accelerating)-> STRONG_SIGNAL
    premium >= 250_000                     -> ALERT
    < 250_000                              -> WATCH
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
# Used by RepetitionEpisode.weighted_premium as a module-level default when
# no RepetitionAccumulator instance is in scope (e.g. direct episode tests).
# RepetitionAccumulator passes self._aggression_discount into weighted_premium
# via get_weighted_premium(discount) — see PBE-F1 deliberation fix 2026-05-03.
# Default 0.5 — hardcoded now; wire through ingestion_config in ING-002-CONFIG.
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
        ING-006: Aggression-weighted cumulative premium using module-level
        _AGGRESSION_DISCOUNT (0.5). For accumulator-controlled discount,
        call get_weighted_premium(discount) directly.

        Aggressive events (is_aggressive=True) contribute full premium.
        Passive events (is_aggressive=False) contribute premium * _AGGRESSION_DISCOUNT.

        Gate 2 (DTE-adjusted floor) in ingest_tick() calls
        get_weighted_premium(self._aggression_discount) so the constructor
        param takes effect (PBE-F1 fix, 2026-05-03).

        Example: 2 aggressive @ $40k + 2 passive @ $40k
          weighted = (40k*1.0 + 40k*1.0) + (40k*0.5 + 40k*0.5) = 80k + 40k = 120k
          total    = 160k
        """
        return self.get_weighted_premium(_AGGRESSION_DISCOUNT)

    def get_weighted_premium(self, discount: float) -> float:
        """
        ING-006 / PBE-F1: Aggression-weighted premium with caller-supplied discount.

        Called by RepetitionAccumulator.ingest_tick() with
        self._aggression_discount so the constructor parameter takes effect.
        The weighted_premium property uses the module constant as a
        convenience default for direct episode tests.
        """
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
        """Human-readable episode summary for logging and test assertions."""
        return (
            f"{self.ticker} {self.contract_type} "
            f"strike={self.strike} expiry={self.expiry} "
            f"trades={self.trade_count} premium=${self.total_premium:,.0f}"
        )

    @property
    def dominant_direction(self) -> str:
        """
        Premium-weighted direction across all events in this episode.

        An episode dominated by SELL PUT premium resolves REPEAT_BUY even
        if the last tick was a passive mid-print (Session 10 resolution).

        SA-PREMERGE-F1 fix (2026-05-03): fallback for missing contract_type
        on an individual event uses self.contract_type (the episode's own
        contract_type), not a hardcoded string. Using a hardcoded "CALL"
        would misclassify premium contribution in a PUT episode where an
        event is missing its contract_type attribute.
        """
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

    An episode represents repeated conviction activity on the same
    (ticker, contract_type, strike, expiry) key within the signal window.

    Constructor parameters
    ----------------------
    window_minutes : int
        Sliding window for episode event retention (signal path).
    min_trades : int
        Gate 1 — minimum event count before an episode emits.
    min_premium : float
        Stored for test introspection (acc.min_premium). Does NOT govern
        Gate 2 — DTE-adjusted tiers govern Gate 2. Provided for backward
        compatibility with legacy test constructors that pass min_premium.
        Default 50_000 (matches test_default_min_premium_50k assertion).
    deep_otm_multiplier : float
        Gate 3 — premium floor multiplier for deep-OTM (>12%) contracts.
        Default 1.0 (dormant). Activate in ING-005 config.
    dte_premium_tiers : list
        Gate 2 tier table. Each entry: (max_dte, {tier: floor}).
        See _DEFAULT_DTE_PREMIUM_TIERS.
    min_sweeps : int
        Gate 4 — minimum sweep events required. Whale-sweep bypass applies
        when len(ep.events) == 1 AND trade_type=SWEEP AND premium >= 500k.
        Default changed from 0 (disabled) to 2 in ING-006. See DEPLOY NOTE.
    aggression_discount : float
        ING-006 / PBE-F1 — passive event premium discount for Gate 2.
        Default 0.5: passive events contribute premium * 0.5 to
        weighted_premium. Aggressive events contribute full premium * 1.0.
        Wire through ingestion_config in ING-002-CONFIG (future sprint).
        Deliberation: PBE-Q2 + PBE-F1 (2026-05-03).

    Band configurability note (ING-005):
    The old _otm_bands constructor parameter accepted a dict but was
    never read — a silent no-op that would mislead callers into thinking
    the bands were configurable. Removed. If band configurability is needed
    in a future sprint, _classify_otm must be updated to consume it.

    ING-006: Gate 2 (DTE-adjusted floor) now evaluates
    ep.get_weighted_premium(self._aggression_discount) instead of
    ep.total_premium. Passive events are discounted by aggression_discount
    (default 0.5). See RepetitionEpisode.get_weighted_premium().
    See DEPLOY NOTE at the top of this file for production impact.

    threading note (PBE-F2 deliberation fix 2026-05-03):
    _tier_map_lock (threading.Lock) protects _tier_map and _dte_tiers.
    set_tier_map() is callable from concurrent stream workers; CPython GIL
    makes a pointer swap technically safe, but the lock is retained for
    correctness under all interpreters and future GIL removal (S4-POST-4
    deliberation rationale). Lock reuse for _dte_tiers is safe — no holder
    calls back into the other.
    """

    def __init__(
        self,
        window_minutes: int = 30,
        min_trades: int = 3,
        min_premium: float = 50_000,   # Fix #1: stored for test introspection; does not govern Gate 2
        deep_otm_multiplier: float = 1.0,
        dte_premium_tiers: Optional[List] = None,
        min_sweeps: int = 2,
        aggression_discount: float = 0.5,  # ING-006 / PBE-F1
    ) -> None:
        self.window_minutes        = window_minutes
        self.min_trades            = min_trades
        self.min_premium           = min_premium   # Fix #1: test introspection only
        self.deep_otm_multiplier   = deep_otm_multiplier
        self.min_sweeps            = min_sweeps
        self._aggression_discount  = aggression_discount  # ING-006 / PBE-F1
        self._dte_tiers            = dte_premium_tiers or _DEFAULT_DTE_PREMIUM_TIERS
        self._episodes: Dict[str, RepetitionEpisode] = {}
        self._tier_map: Dict[str, int] = {}
        # PBE-F2: lock protects _tier_map and _dte_tiers (S4-POST-4 pattern).
        self._tier_map_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Fix #1: window property — exposes window_minutes as a timedelta so
    # tests can call acc.window.total_seconds() == window_minutes * 60.
    # ------------------------------------------------------------------
    @property
    def window(self) -> timedelta:
        """Return the signal window as a timedelta for test introspection."""
        return timedelta(minutes=self.window_minutes)

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def flush_emit_cache(self) -> None:
        """Clear episode state. Call at stream startup (S1 spec)."""
        self._episodes.clear()

    def set_tier_map(self, tier_map: Dict[str, int]) -> None:
        """Inject symbol -> tier (1/2/3) map for DTE floor selection.

        PBE-F2 (2026-05-03): threading.Lock restored per S4-POST-4 deliberation.
        set_tier_map() is called from the registry warmup path which may run
        concurrently with stream worker ticks. Lock ensures _tier_map pointer
        swap is visible across threads under all interpreters (CPython GIL
        makes pointer swap atomic in practice, but lock is correct regardless).
        """
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

    def _get_episode_min_premium(
        self, ep: RepetitionEpisode
    ) -> float:
        """
        Return the DTE-adjusted premium floor for this episode.

        Uses the latest event's DTE to select from _dte_tiers.
        Tier is resolved from _tier_map; defaults to tier 2 if unknown.

        PBE-F2 (2026-05-03): reads _tier_map under _tier_map_lock (same lock
        as set_tier_map) so concurrent registry warmup cannot produce a
        torn read of the tier map pointer.
        """
        if not ep.events:
            return 0.0
        latest = ep.events[-1]
        dte    = int(getattr(latest, "dte", 0) or 0)
        ticker = getattr(latest, "ticker", "") or ""
        with self._tier_map_lock:
            tier = self._tier_map.get(ticker, 2)

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

    def get_alert_level(self, ep_or_premium: Union["RepetitionEpisode", float]) -> str:
        """
        Map episode (or raw premium float) to alert tier.

        Fix #3 (2026-05-04): accepts either a RepetitionEpisode or a float.
        When passed an episode, is_accelerating is factored in:
          accel + total_premium >= 1_000_000 -> CONVICTION

        Tier names (test-reconciled):
          >= 5_000_000                          -> CONVICTION
          >= 2_000_000                          -> CONVICTION  (high-premium path)
          >= 1_000_000 AND accelerating         -> CONVICTION
          >= 1_000_000 (not accelerating)       -> STRONG_SIGNAL
          >= 250_000                            -> ALERT
          < 250_000                             -> WATCH
        """
        # Duck-type: RepetitionEpisode has .total_premium; float does not
        if hasattr(ep_or_premium, "total_premium"):
            ep: RepetitionEpisode = ep_or_premium  # type: ignore[assignment]
            total_premium = ep.total_premium
            accelerating  = ep.is_accelerating
        else:
            total_premium = float(ep_or_premium)  # type: ignore[arg-type]
            accelerating  = False

        if total_premium >= 2_000_000:
            return "CONVICTION"
        if total_premium >= 1_000_000:
            return "CONVICTION" if accelerating else "STRONG_SIGNAL"
        if total_premium >= 250_000:
            return "ALERT"
        return "WATCH"

    # ------------------------------------------------------------------
    # Core ingest
    # ------------------------------------------------------------------

    async def ingest_tick(self, ev) -> Optional[RepetitionEpisode]:
        """
        Ingest one OptionsFlowEvent tick and return an emitted episode or None.

        The event is wrapped in _DictEventWrapper if it arrives as a dict.
        Old events outside the signal window are evicted before gate evaluation.

        PBE-F3 (2026-05-03): ingest() backward-compat shim was removed, then
        re-added as a thin alias in Fix #1 (2026-05-04) so legacy test call
        sites (asyncio.run(acc.ingest(ev))) continue to work.

        Gates applied (in order):
          1. min_trades         — episode event count >= min_trades
          2. DTE-adjusted floor — ep.get_weighted_premium(self._aggression_discount)
                                  >= _get_episode_min_premium(ep)
                                  ING-006 / PBE-F1: evaluates WEIGHTED premium
                                  using self._aggression_discount (constructor
                                  param, default 0.5). Passive events discounted.
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

        # ── Gate 1: min_trades ───────────────────────────────────────────────────────
        if ep.trade_count < self.min_trades:
            return None

        # ── Gate 2: DTE-adjusted premium floor (ING-006 / PBE-F1: weighted) ──
        effective_min_prem = self._get_episode_min_premium(ep)
        # Use constructor-param discount (self._aggression_discount) so callers
        # can override for backtesting without monkey-patching module constants.
        ep_weighted = ep.get_weighted_premium(self._aggression_discount)

        # ── Gate 3: Deep OTM multiplier ──────────────────────────────────────────────
        otm_band = "UNKNOWN"
        try:
            up_raw = getattr(ev, "underlying_price", 0.0)
            if isinstance(up_raw, (int, float)) and up_raw > 0:
                otm_band = self._classify_otm(ev)
        except Exception:  # pragma: no cover
            pass

        if self.deep_otm_multiplier > 1.0 and otm_band == "DEEP_OTM":  # dormant at default 1.0 — ING-005/SA-Q1
            deep_floor = effective_min_prem * self.deep_otm_multiplier
            if ep_weighted < deep_floor:
                return None
        else:
            if ep_weighted < effective_min_prem:
                return None

        # ── Gate 4: min_sweeps (with whale-sweep bypass) ─────────────────────────────
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

    async def ingest(self, ev) -> Optional[RepetitionEpisode]:
        """Backward-compat alias for ingest_tick().

        Fix #1 (2026-05-04): re-added so legacy test call sites that use
        asyncio.run(acc.ingest(ev)) continue to work without AttributeError.
        Production code paths use ingest_tick() directly.
        """
        return await self.ingest_tick(ev)
