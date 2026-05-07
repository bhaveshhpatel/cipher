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
#
# DEPLOY NOTE (ING-011 — 2026-05-06):
#   _classify_otm() replaced by _classify_moneyness_band() which covers the
#   full moneyness spectrum:
#     DEEP_ITM | ITM | ATM | OTM | DEEP_OTM | UNKNOWN
#   Thresholds (symmetric with ING-005 ATM band — D1 deliberation 2026-05-06):
#     DEEP_ITM: PUT strike > underlying * 1.10  |  CALL strike < underlying * 0.90
#     ITM:      PUT strike > underlying * 1.02  |  CALL strike < underlying * 0.98
#     ATM:      abs(strike - underlying) / underlying <= 0.02
#     OTM:      2% < pct <= 12%
#     DEEP_OTM: pct > 12%
#     UNKNOWN:  underlying_price == 0 or any error
#
#   dominant_direction override for ITM puts (D2 deliberation 2026-05-06):
#     When contract_type == PUT and otm_band in ('ITM', 'DEEP_ITM'):
#       AT_BID fill = buyer paying near-intrinsic in wide spread, NOT put writer
#       Force direction = REPEAT_BUY, sentiment = BEARISH regardless of
#       order_side_to_direction() result.
#     ITM CALL AT_BID is unchanged — call seller = bearish already correct.
#
#   otm_band enum extended (D3 deliberation 2026-05-06):
#     Values: 'ATM' | 'OTM' | 'DEEP_OTM' | 'ITM' | 'DEEP_ITM' | 'UNKNOWN'
#     No DB migration required — flow_events.otm_band is TEXT (not a PG enum).
#     Existing consumers checking for 'OTM' / 'DEEP_OTM' are unaffected.
#
#   SA-6 note (panel deliberation 2026-05-06):
#     ep.otm_band reflects the classification of the LAST tick in the episode
#     window, not a representative or majority band across all events. This is
#     the same per-tick approach used by the former _classify_otm(). Accepted
#     for Phase 1 — do NOT treat ep.otm_band as an episode-aggregate field.
#     A future story may introduce majority-band aggregation if drift within a
#     30-minute window proves material.
#
#   SA-F1 fix (panel finding 2026-05-06):
#     dominant_direction ITM override gate now uses a MAJORITY band computed
#     inline across all episode events, NOT self.otm_band (last-tick only).
#     self.otm_band is preserved as the reported episode field (per SA-6),
#     but the override gate itself uses _majority_itm_band() so that a final
#     tick with underlying_price == 0 (UNKNOWN) cannot silently suppress the
#     override when prior ticks established a clear ITM/DEEP_ITM majority.
#
#   SA-F2 fix (panel finding 2026-05-06):
#     bid_ask_class is stamped on every event dict by bid_ask_classifier.py
#     at parse time in the ING-006 production path (tradier_stream.py).
#     _DictEventWrapper.bid_ask_class defaults to 'UNKNOWN' when the field is
#     absent — override will not fire on events missing the field. This is
#     correct safe-by-default behaviour; no production events should reach
#     the accumulator without bid_ask_class set.
#
#   PBE-F2 fix (panel finding 2026-05-06):
#     self.contract_type in dominant_direction is populated at episode
#     creation time by _get_or_create_episode() from the first event's
#     contract_type field. It is never None or empty for a valid episode.
#     See RepetitionEpisode dataclass field: contract_type: str.
#
# DEPLOY NOTE (ING-011b — 2026-05-07):
#   _classify_moneyness_band() promoted from RepetitionAccumulator instance
#   method to MODULE-LEVEL function (D3 deliberation 2026-05-06).
#   Zero self-state dependencies — pure arithmetic over event fields.
#   RepetitionAccumulator.ingest_tick() call site updated accordingly.
#   RepetitionEpisode._majority_itm_band() updated to call module-level
#   function — eliminates the inline threshold re-implementation noted in
#   PBE-6 / QA-F1 (ING-011 panel 2026-05-06).
#
#   get_weighted_premium() updated (D1 Option B — deliberation 2026-05-06):
#     is_aggressive is set moneyness-blind at parse time (ING-006). ITM PUT
#     AT_BID / BELOW_BID fills arrive with is_aggressive=True even though
#     they represent buyers paying near-intrinsic value, not aggressive put
#     writers. Receiving full weight (×1.0) overstated weighted_premium by up
#     to 2× for ITM-put-buyer episodes, allowing false Gate-2 clears.
#
#     Fix: per-event call to module-level _classify_moneyness_band(e) inside
#     the get_weighted_premium() loop. When contract_type==PUT and
#     bid_ask_class in ('AT_BID', 'BELOW_BID') and band in _ITM_BANDS,
#     apply aggression_discount regardless of is_aggressive flag.
#
#     D5 fallback: UNKNOWN band (underlying_price==0) — no discount, full
#     weight. When moneyness cannot be determined, do not discount.
#
#     PRODUCTION IMPACT: weighted_premium will decrease for ITM-put-buyer
#     episodes. Some episodes that previously cleared Gate 2 solely on
#     overstated ITM-put-buyer premium will no longer emit. This is
#     intentional — those were false positives. Monitor ITM-put signal
#     volume on day 1.
#
#     ROLLBACK: No runtime kill-switch. Full PR revert required.
#     OTM PUT AT_BID writer behaviour UNCHANGED (W-1/W-11 ING-006 regression).
#     ITM CALL AT_BID call-writer behaviour UNCHANGED (W-5).
#     Passive mid-fill discount UNCHANGED (W-6).
#
#   Deliberation: D1–D5 (SA/PBE/QA 2026-05-06). Issue #80.
#   Sprint doc: commit 5ba7e7d.
#   Test matrix: W-1 through W-12 in test_ing011b_itm_aggression_weight.py.
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

Moneyness classification (ING-011 — replaces OTM-only classification from S4/ING-005):
  Full spectrum: DEEP_ITM | ITM | ATM | OTM | DEEP_OTM | UNKNOWN
  Symmetric thresholds (mirrors ING-005 ATM ±2% band):
    DEEP_ITM: PUT strike > underlying * 1.10  |  CALL strike < underlying * 0.90
    ITM:      PUT strike > underlying * 1.02  |  CALL strike < underlying * 0.98
    ATM:      abs(strike - underlying) / underlying <= 0.02
    OTM:      2% < moneyness_pct <= 12%
    DEEP_OTM: moneyness_pct > 12%
    UNKNOWN:  underlying_price == 0 or any calculation error

  ING-011b (D3 deliberation 2026-05-06): _classify_moneyness_band() promoted
  to module-level function. Zero self-state dependencies — pure arithmetic.
  Callable from RepetitionEpisode.get_weighted_premium() and
  RepetitionAccumulator.ingest_tick() without circular dependency.

ATM threshold deliberation note (Architect + Principal Engineer, 2026-04-30):
  ±2% was selected as the working threshold. Absolute dollar amounts break
  across underlying price regimes; percentage is the only portable definition.
  Events with underlying_price == 0 fall back to UNKNOWN with no classification.

ITM override in dominant_direction (ING-011 D2 — 2026-05-06):
  Deeply ITM puts filling AT_BID represent a buyer paying near-intrinsic value
  in a wide spread — NOT a put writer initiating a sell. The existing
  order_side_to_direction() mapping was designed for OTM puts where AT_BID
  reliably signals writer intent. For ITM puts the spread dynamics are
  fundamentally different. Override: PUT in ITM/DEEP_ITM band with AT_BID fill
  -> REPEAT_BUY (bearish). ITM CALL AT_BID is unchanged (call seller = bearish
  already correct per existing logic).

  SA-F1 (panel finding 2026-05-06): The ITM override gate uses a majority
  band computed inline from all episode events — not self.otm_band (last-tick).
  This prevents a final UNKNOWN tick from silently suppressing the override
  when the majority of premium-weighted events are ITM/DEEP_ITM.

ITM-buyer discount in get_weighted_premium() (ING-011b D1 Option B — 2026-05-06):
  is_aggressive is moneyness-blind (ING-006). ITM PUT AT_BID fills arrive with
  is_aggressive=True regardless of band. These are buyers paying near-intrinsic
  value — not aggressive writers. Full weight overstates weighted_premium by
  up to 2×. Fix: per-event _classify_moneyness_band(e) inside the loop.
  D5 fallback: UNKNOWN → no discount (full weight, safe default).
  OTM PUT AT_BID writer behaviour unchanged (W-1/W-11 ING-006 regression).

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
  ING-011: ITM put override applied post-resolution — see property docstring.

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

  Emit decision logic (2026-05-06 fix — C007-3 / C007-6):
    1. Cooldown expired (elapsed >= cooldown_s): always emit. Reset
       last_signaled_premium to current total for a fresh Gate-2 baseline.
    2. First emit (last_signaled_premium == 0): emit, set baseline.
    3. Incremental within first cooldown window: Gate-2 delta applies.
  Gate-2 delta must not block post-cooldown re-fires; it exists only to
  suppress continuous sub-threshold premium spam within a single cooldown window.

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

get_signal() call forms (C-008-7 fix — 2026-05-06):
  get_signal() now accepts an optional cooldown timedelta as a second
  positional argument:
    get_signal(ev)                       # legacy one-arg: delegates ingest_tick
    get_signal(timestamp, ep=ep)         # two-arg kw: C-008 original form
    get_signal(timestamp, cooldown, ep)  # three-arg positional: C-008-7 form
  When cooldown is a timedelta, its total_seconds() overrides
  self._signal_cooldown_s for this call only. When cooldown is None,
  self._signal_cooldown_s is used (full backward-compat).

_min_premium_override semantics (2026-05-06 fix — C008-5/6 + E05/E07 regression):
  _min_premium_override is a FLOOR BASELINE.
  When _tiers_explicit is False (caller did not pass dte_premium_tiers),
  the override is used directly — the default tier table is not consulted.
  When _tiers_explicit is True, effective floor = max(override, dte_tier_floor).
  This preserves the max() behaviour for callers that explicitly pass tiers
  while allowing shim tests that set only min_premium to get the override they expect.

is_accelerating semantics (2026-05-06 fix):
  True when the last 3 events all occur within a 60-second window.
  Measured as: (recent[-1].timestamp - recent[0].timestamp).total_seconds() <= 60
  Prior implementation checked gaps[-1] < gaps[0] (strictly shrinking gaps),
  which incorrectly rejected uniform cadence (e.g. 0s, 20s, 40s = span 40s).

Alert levels (canonical — used by signal_store.py float path):
  >= 2_000_000                            -> CONVICTION
  >= 1_000_000                            -> WHALE
  >= 500_000                              -> INSTITUTIONAL
  >= 100_000                              -> LARGE
  < 100_000                               -> RETAIL

Alert levels (episode path):
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

# is_accelerating: max span of last 3 events to qualify as accelerating.
_ACCELERATING_SPAN_S: float = 60.0

# ---------------------------------------------------------------------------
# ING-011: Moneyness band thresholds (symmetric with ING-005 ATM ±2% band).
# D1 deliberation 2026-05-06: reuse ING-005 thresholds exactly.
# ---------------------------------------------------------------------------
_ITM_THRESHOLD: float = 0.02   # >2% in-the-money -> ITM
_DEEP_ITM_THRESHOLD: float = 0.10  # >10% in-the-money -> DEEP_ITM

# ITM band set used by dominant_direction override gate, _majority_itm_band(),
# and get_weighted_premium() ITM-buyer discount (ING-011b D1).
_ITM_BANDS: frozenset = frozenset({"ITM", "DEEP_ITM"})


# ---------------------------------------------------------------------------
# ING-011b (D3 deliberation 2026-05-06): Module-level _classify_moneyness_band.
#
# Previously a method on RepetitionAccumulator. Promoted to module level
# because:
#   1. Zero self-state dependencies — pure arithmetic over event fields.
#   2. RepetitionEpisode.get_weighted_premium() (ING-011b D1) needs to call
#      it per-event. A method on RepetitionAccumulator cannot be called from
#      RepetitionEpisode without passing the accumulator instance — which
#      would create tight coupling. Module-level avoids this cleanly.
#   3. Eliminates the inline threshold re-implementation inside
#      _majority_itm_band() (noted in PBE-6 / QA-F1 ING-011 panel 2026-05-06).
#
# RepetitionAccumulator.ingest_tick() call site updated to call this directly.
# RepetitionEpisode._majority_itm_band() updated to delegate here.
# ---------------------------------------------------------------------------
def _classify_moneyness_band(ev) -> str:
    """Classify a single event's contract into the full moneyness spectrum.

    ING-011 (D1 deliberation 2026-05-06) — replaces _classify_otm().
    ING-011b (D3 deliberation 2026-05-06) — promoted to module-level function.

    Returns one of: 'DEEP_ITM' | 'ITM' | 'ATM' | 'OTM' | 'DEEP_OTM' | 'UNKNOWN'

    Thresholds (symmetric, mirror ING-005 ATM ±2% band):
      DEEP_ITM: PUT strike > underlying * 1.10  |  CALL strike < underlying * 0.90
      ITM:      PUT strike > underlying * 1.02  |  CALL strike < underlying * 0.98
      ATM:      abs(strike - underlying) / underlying <= 0.02
      OTM:      2% < moneyness_pct <= 12%
      DEEP_OTM: moneyness_pct > 12%
      UNKNOWN:  underlying_price == 0 or any calculation error

    underlying_price == 0: returns 'UNKNOWN', no classification attempted.
    """
    try:
        up = float(getattr(ev, "underlying_price", 0.0) or 0.0)
        if up == 0.0:
            return "UNKNOWN"
        strike = float(getattr(ev, "strike", 0.0) or 0.0)
        contract_type = str(getattr(ev, "contract_type", "") or "").upper()

        pct = abs(strike - up) / up

        if pct <= _ITM_THRESHOLD:
            return "ATM"

        if contract_type == "PUT":
            in_the_money = strike > up
        elif contract_type == "CALL":
            in_the_money = strike < up
        else:
            return "OTM" if pct <= 0.12 else "DEEP_OTM"

        if in_the_money:
            return "DEEP_ITM" if pct > _DEEP_ITM_THRESHOLD else "ITM"
        else:
            return "DEEP_OTM" if pct > 0.12 else "OTM"
    except (TypeError, ZeroDivisionError, ValueError):
        return "UNKNOWN"


# ---------------------------------------------------------------------------
# _DictEventWrapper
# ---------------------------------------------------------------------------
class _DictEventWrapper:
    __slots__ = (
        "premium", "timestamp", "trade_type", "dte",
        "underlying_price", "order_side", "contract_type",
        "is_aggressive", "ticker", "strike", "expiry",
        "bid_ask_class",
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
        # SA-F2 (panel finding 2026-05-06): bid_ask_class is stamped by
        # bid_ask_classifier.py at parse time in the ING-006 production path.
        # Defaulting to 'UNKNOWN' here is correct safe-by-default behaviour —
        # the ITM override in dominant_direction will not fire for events
        # missing this field, which is the safest possible fallback.
        self.bid_ask_class    = d.get("bid_ask_class", "UNKNOWN")


@dataclass
class RepetitionEpisode:
    """Active episode for a (ticker, contract_type, strike, expiry) key."""
    # PBE-F2 (panel finding 2026-05-06): contract_type is populated at episode
    # creation time by _get_or_create_episode() from the first event's
    # contract_type field. It is set before any caller can access
    # dominant_direction. See RepetitionAccumulator._get_or_create_episode().
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
    last_signal_at:       Optional[datetime] = None

    # ING-007: multi-day lookback fields
    prior_days_active:     int  = 0
    prior_days_aggressive: int  = 0
    is_multi_day_repeat:   bool = False

    # ING-007 / ING-005 deferred / ING-011 extended
    # Values: 'ATM' | 'OTM' | 'DEEP_OTM' | 'ITM' | 'DEEP_ITM' | 'UNKNOWN'
    # SA-6: reflects the LAST tick classification only (Phase 1 accepted limitation).
    # SA-F1: dominant_direction override uses _majority_itm_band(), not this field.
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
        """Return premium-weighted episode value, applying aggression_discount
        to passive events AND to ITM PUT AT_BID buyer events (ING-011b D1).

        ING-006 semantics (unchanged for non-ITM events):
          is_aggressive=False → premium * discount  (passive mid-fill)
          is_aggressive=True  → premium * 1.0       (aggressive writer/buyer)

        ING-011b correction (D1 Option B — deliberation 2026-05-06):
          is_aggressive is set moneyness-blind at parse time by
          is_directionally_aggressive() in bid_ask_classifier.py (ING-006).
          AT_BID PUT arrives as is_aggressive=True regardless of moneyness band.

          For ITM puts, AT_BID reflects a BUYER paying near-intrinsic value in
          a wide spread — NOT an aggressive put writer. Full weight (×1.0)
          overstates weighted_premium by up to 2× and can cause false Gate-2
          clears on ITM-put-buyer episodes.

          Fix: per-event call to module-level _classify_moneyness_band(e).
          When contract_type==PUT AND bid_ask_class in ('AT_BID','BELOW_BID')
          AND band in _ITM_BANDS ('ITM','DEEP_ITM'): apply discount regardless
          of is_aggressive flag.

          D5 fallback: UNKNOWN band (underlying_price==0) → no discount applied
          (full weight). When moneyness cannot be determined, do not discount.

        What is NOT changed:
          OTM PUT AT_BID writer (is_aggressive=True, band=OTM) → ×1.0 unchanged
          ITM CALL AT_BID writer (is_aggressive=True, band=ITM, ctype=CALL) → ×1.0 unchanged
          Passive mid-fill (is_aggressive=False) → ×discount unchanged
          AT_ASK buyer (is_aggressive=True, any band) → ×1.0 unchanged
        """
        total = 0.0
        for e in self.events:
            prem = getattr(e, "premium", 0.0)
            if getattr(e, "is_aggressive", False):
                # ING-011b: check if this is an ITM PUT AT_BID buyer event.
                # is_aggressive is moneyness-blind (ING-006) — must re-classify.
                bac   = getattr(e, "bid_ask_class", "UNKNOWN")
                ctype = str(getattr(e, "contract_type", "") or "").upper()
                if (
                    ctype == "PUT"
                    and bac in ("AT_BID", "BELOW_BID")
                    and _classify_moneyness_band(e) in _ITM_BANDS
                ):
                    total += prem * discount  # ITM put buyer — discount applies
                else:
                    total += prem             # Genuine aggressive writer/buyer — full weight
            else:
                total += prem * discount      # Passive fill — unchanged
        return total

    @property
    def is_accelerating(self) -> bool:
        """True when the last 3 events all fall within a 60-second window.

        Spec (test_repetition_engine.py docstring):
          "is_accelerating True when last 3 events within 60s"

        Implementation: span = last_ts - first_ts of the last 3 events.
        Uniform cadence (e.g. 0s, 20s, 40s -> span=40s) qualifies.
        """
        if len(self.events) < 3:
            return False
        recent = self.events[-3:]
        try:
            t0 = recent[0].timestamp
            t2 = recent[-1].timestamp
            if t0 is None or t2 is None:
                return False
            span = (t2 - t0).total_seconds()
            return span <= _ACCELERATING_SPAN_S
        except (AttributeError, TypeError):
            return False

    def summary_str(self) -> str:
        """Human-readable episode summary. Backward-compat shim (2026-05-04)."""
        return (
            f"{self.ticker} {self.contract_type} "
            f"strike={self.strike} expiry={self.expiry} "
            f"trades={self.trade_count} "
            f"total_premium=${self.total_premium:,.0f}"
        )

    def _majority_itm_band(self) -> bool:
        """Return True when the PREMIUM-WEIGHTED majority of episode events
        classify as ITM or DEEP_ITM.

        SA-F1 fix (panel finding 2026-05-06):
          self.otm_band reflects the LAST tick only (SA-6 Phase 1 accepted
          limitation). If that final tick has underlying_price == 0 (UNKNOWN),
          relying on self.otm_band would silently suppress the ITM override
          even when all prior ticks were clearly ITM/DEEP_ITM.

          This method accumulates per-event premium weight using the module-level
          _classify_moneyness_band() function (promoted ING-011b D3). The override
          gate in dominant_direction fires when itm_prem > non_itm_prem
          (majority by premium, not by count).

          UNKNOWN events (underlying_price == 0) return 'UNKNOWN' from
          _classify_moneyness_band() and contribute 0 weight to both sides —
          they are neutral and do not suppress or trigger the override.

        Returns:
          True  — ITM/DEEP_ITM premium dominates; override should be considered.
          False — OTM/ATM/UNKNOWN premium dominates; do not override.
        """
        itm_prem = 0.0
        non_itm_prem = 0.0
        for e in self.events:
            prem = getattr(e, "premium", 0.0)
            band = _classify_moneyness_band(e)
            if band == "UNKNOWN":
                continue  # neutral — contributes neither side
            if band in _ITM_BANDS:
                itm_prem += prem
            else:
                non_itm_prem += prem
        return itm_prem > non_itm_prem

    @property
    def dominant_direction(self) -> str:
        """Premium-weighted dominant direction for the episode.

        PBE-F2 (panel finding 2026-05-06):
          self.contract_type is set at episode creation time in
          _get_or_create_episode() from the first event's contract_type field.
          It is populated before ingest_tick() returns the episode to any
          caller — dominant_direction is never accessed on an uninitialised
          episode.

        ING-011 override (D2 deliberation 2026-05-06):
          When contract_type == PUT and the PREMIUM-WEIGHTED majority of
          episode events classify as ITM or DEEP_ITM (see _majority_itm_band()),
          an AT_BID fill reflects a buyer paying near-intrinsic value in a
          wide spread — NOT a put writer. Force REPEAT_BUY (bearish) for the
          entire episode regardless of what order_side_to_direction() resolves.

          SA-F1 fix (panel finding 2026-05-06):
            The ITM gate now calls self._majority_itm_band() instead of
            checking self.otm_band directly. self.otm_band is last-tick only
            (SA-6). A final tick with underlying_price == 0 (UNKNOWN) no
            longer suppresses the override when prior ticks were ITM/DEEP_ITM.

          Trigger condition:
            1. self.contract_type == 'PUT'
            2. _majority_itm_band() returns True (ITM premium dominates)
            3. bid_side_prem > ask_side_prem across episode events
          All three conditions must hold. Any one failing leaves base_direction
          unchanged.

          ITM CALL AT_BID is NOT overridden — call seller = bearish (REPEAT_SELL)
          is already the correct existing output from order_side_to_direction().
          The override block is structurally gated to PUT only (condition 1).

          underlying_price == 0 on ALL events: _majority_itm_band() returns
          False (itm_prem == non_itm_prem == 0.0, 0 > 0 is False). No
          override fires. Existing order_side_to_direction() result stands.

          SA-F2 (panel finding 2026-05-06):
            bid_ask_class is stamped by bid_ask_classifier.py at parse time.
            If absent, _DictEventWrapper defaults to 'UNKNOWN' — 'UNKNOWN'
            is not in ('AT_BID', 'BELOW_BID'), so bid_side_prem stays 0
            and condition 3 fails. Safe-by-default.

          PBE-6 note (panel deliberation 2026-05-06): self.events is iterated
          three times — premium weighting, majority band, bid/ask dominance.
          All three loops are O(N) over episode size (typically 3–20 events).
          The majority-band loop in _majority_itm_band() now delegates to
          module-level _classify_moneyness_band() (ING-011b D3) — no
          additional per-event arithmetic; duplication eliminated.
          Merging all three loops into one pass is a known optimisation
          deferred to a standalone perf issue.
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
        base_direction = "REPEAT_BUY" if buy_prem >= sell_prem else "REPEAT_SELL"

        # ING-011: ITM PUT override — AT_BID on ITM put = buyer, not writer.
        # SA-F1: use _majority_itm_band() (premium-weighted majority across
        # all events) rather than self.otm_band (last-tick only) so that a
        # final UNKNOWN tick cannot suppress the override.
        if (
            self.contract_type.upper() == "PUT"
            and self._majority_itm_band()
        ):
            bid_side_prem = 0.0
            ask_side_prem = 0.0
            for e in self.events:
                bac = getattr(e, "bid_ask_class", "UNKNOWN")
                prem = getattr(e, "premium", 0.0)
                if bac in ("AT_BID", "BELOW_BID"):
                    bid_side_prem += prem
                elif bac in ("AT_ASK", "ABOVE_ASK"):
                    ask_side_prem += prem
            if bid_side_prem > ask_side_prem:
                return "REPEAT_BUY"

        return base_direction


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
    min_premium : float | None   — floor baseline; when _tiers_explicit is False
                                    (caller did not pass dte_premium_tiers), the
                                    override is used directly as the floor.
                                    When _tiers_explicit is True, effective floor
                                    is max(min_premium, dte_tier_floor).
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
        self._tiers_explicit: bool = dte_premium_tiers is not None
        self._dte_tiers            = dte_premium_tiers or _DEFAULT_DTE_PREMIUM_TIERS
        self._episodes: Dict[str, RepetitionEpisode] = {}
        self._tier_map: Dict[str, int] = {}
        self._tier_map_lock = threading.Lock()
        self._min_premium_override: Optional[float] = min_premium
        self._signal_cooldown_s: float = float(signal_cooldown) * 60.0 if signal_cooldown is not None else 0.0

    @property
    def window(self) -> timedelta:
        return timedelta(minutes=self.window_minutes)

    @property
    def min_premium(self) -> float:
        if self._min_premium_override is not None:
            return self._min_premium_override
        if not self._dte_tiers:
            return 0.0
        _, floors = self._dte_tiers[0]
        return float(floors.get(1, floors.get(2, 0.0)))

    def flush_emit_cache(self) -> None:
        self._episodes.clear()

    def set_tier_map(self, tier_map: Dict[str, int]) -> None:
        with self._tier_map_lock:
            self._tier_map = tier_map

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
        """
        Return the effective premium floor for this episode.

        When _tiers_explicit is False (caller did not pass dte_premium_tiers)
        AND _min_premium_override is set: use the override directly.
        The default tier table is not consulted — shim callers that pass
        only min_premium must not have their floor silently raised by the
        default T2/T1 tier floors.

        When _tiers_explicit is True: effective floor = max(override, dte_tier_floor).
        This preserves the strict behaviour for production callers that wire tiers
        explicitly.
        """
        if not self._tiers_explicit and self._min_premium_override is not None:
            return float(self._min_premium_override)

        if not ep.events:
            return float(self._min_premium_override) if self._min_premium_override is not None else 0.0
        if not self._dte_tiers:
            return float(self._min_premium_override) if self._min_premium_override is not None else 0.0

        latest = ep.events[-1]
        dte    = int(getattr(latest, "dte", 0) or 0)
        ticker = getattr(latest, "ticker", "") or ""
        with self._tier_map_lock:
            tier = self._tier_map.get(ticker, 1)

        dte_floor: Optional[float] = None
        for max_dte, floors in self._dte_tiers:
            if dte <= max_dte:
                dte_floor = float(floors.get(tier, floors.get(1, 0.0)))
                break
        if dte_floor is None:
            dte_floor = float(_DEFAULT_DTE_PREMIUM_TIERS_91_PLUS.get(tier, 2_000_000))

        if self._min_premium_override is not None:
            return max(float(self._min_premium_override), dte_floor)
        return dte_floor

    def get_alert_level(self, episode_or_premium: Union["RepetitionEpisode", float]) -> str:
        """Map to alert tier.

        Episode path (RepetitionEpisode input):
          CONVICTION    >= 2_000_000, or accelerating >= 1_000_000
          STRONG_SIGNAL >= 1_000_000
          ALERT         >= 250_000
          LARGE         >= 100_000
          WATCH         < 100_000

        Float path (signal_store.py canonical):
          CONVICTION    >= 2_000_000
          WHALE         >= 1_000_000
          INSTITUTIONAL >= 500_000
          LARGE         >= 100_000
          RETAIL        < 100_000
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

    async def get_signal(
        self,
        ev_or_ts=None,
        cooldown: Optional[timedelta] = None,
        ep: Optional["RepetitionEpisode"] = None,
    ) -> Optional["RepetitionEpisode"]:
        """Backward-compat shim.

        Supported call forms:
          get_signal(ev)                        # legacy one-arg: delegates ingest_tick
          get_signal(timestamp, ep=ep)          # C-008 two-arg kw form
          get_signal(timestamp, cooldown, ep)   # C-008-7 three-arg positional form
        """
        if isinstance(cooldown, RepetitionEpisode):
            ep = cooldown
            cooldown = None

        cooldown_s: float
        if isinstance(cooldown, timedelta):
            cooldown_s = cooldown.total_seconds()
        else:
            cooldown_s = self._signal_cooldown_s

        if ep is not None and isinstance(ep, RepetitionEpisode):
            ts = ev_or_ts if isinstance(ev_or_ts, datetime) else datetime.now(timezone.utc)
            if cooldown_s > 0 and ep.last_signal_at is not None:
                elapsed = (ts - ep.last_signal_at).total_seconds()
                if elapsed < cooldown_s:
                    return None
            ep.last_signal_at = ts
            return ep

        return await self.ingest_tick(ev_or_ts)

    async def ingest(self, ev) -> Optional[RepetitionEpisode]:
        """Backward-compat async shim with cooldown + Gate-2 delta check.
        NOT called by production code.

        Emit decision (three mutually exclusive paths):
          1. Cooldown has elapsed since last signal: always emit, reset
             last_signaled_premium baseline to current total.
          2. First emit (last_signaled_premium == 0): emit, set baseline.
          3. Within first cooldown window: Gate-2 delta check applies.
        """
        ep = await self.ingest_tick(ev)
        if ep is None:
            return None

        ts = getattr(ev, "timestamp", None) or datetime.now(timezone.utc)
        if hasattr(ts, 'replace') and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        if self._signal_cooldown_s > 0 and ep.last_signal_at is not None:
            elapsed = (ts - ep.last_signal_at).total_seconds()
            if elapsed < self._signal_cooldown_s:
                return None
            ep.last_signaled_premium = ep.total_premium
            ep.last_signal_at = ts
            return ep

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

        try:
            ev.timestamp = ts
        except (AttributeError, TypeError):
            pass

        ep.events.append(ev)
        ep.last_seen = ts
        if ep.first_seen is None:
            ep.first_seen = ts

        cutoff = ts - timedelta(minutes=self.window_minutes)
        self._evict_old_events(ep, cutoff)

        if len(ep.events) == 0:
            return None

        # ING-011 / ING-011b: classify full moneyness spectrum.
        # ep.otm_band = last-tick classification (SA-6 Phase 1 accepted).
        # Uses module-level _classify_moneyness_band() (ING-011b D3).
        # dominant_direction uses _majority_itm_band() for the override gate
        # (SA-F1); ep.otm_band is the reported episode field only.
        moneyness_band = "UNKNOWN"
        try:
            up_raw = getattr(ev, "underlying_price", 0.0)
            if isinstance(up_raw, (int, float)) and up_raw > 0:
                moneyness_band = _classify_moneyness_band(ev)
        except Exception:
            pass
        ep.otm_band = moneyness_band

        ep.is_multi_day_repeat = ep.prior_days_active >= self._multi_day_min_days

        if ep.trade_count < self.min_trades:
            return None

        effective_min_prem = self._get_episode_min_premium(ep)
        ep_weighted = ep.get_weighted_premium(self._aggression_discount)

        if self.deep_otm_multiplier > 1.0 and moneyness_band == "DEEP_OTM":
            if ep_weighted < effective_min_prem * self.deep_otm_multiplier:
                return None
        else:
            if ep_weighted < effective_min_prem:
                return None

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
