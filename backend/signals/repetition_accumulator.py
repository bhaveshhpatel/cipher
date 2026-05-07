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
#   BACKWARD-COMPAT SHIM (ING-011b fix — 2026-05-07):
#     RepetitionAccumulator._classify_moneyness_band(ev) retained as an
#     instance-method shim that delegates to the module-level function.
#     Required for test_accumulator_s4_coverage.py which calls
#     acc._classify_moneyness_band(ev) under the pre-D3 contract.
#     Shim is a pure passthrough — no logic duplication.
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
#
# DEPLOY NOTE (ING-010 Gate 2 — 2026-05-07):
#   _get_episode_min_premium() now reads a live floor from gate_config_store
#   in addition to the DTE-tier table. Resolution order:
#     1. Compute dte_floor from self._dte_tiers as before.
#     2. Read live_floor = gate_config_store.get("min_premium", tier_int).
#     3. effective_floor = max(dte_floor, live_floor) — store can only RAISE
#        the floor (tighten the gate), never silently lower it below the
#        static DTE table.
#   When gate_config_store returns None (cold start, key not loaded from DB),
#   behaviour is identical to before — dte_floor stands alone.
#   _min_premium_override constructor kwarg semantics UNCHANGED.
#   aggression_discount wiring deferred to ING-002-CONFIG (future sprint).
#
# DEPLOY NOTE (ING-010-OI — 2026-05-07):
#   ingest_tick() now enforces a per-tier open_interest floor (Gate 3 / OI gate).
#   Resolution:
#     tier_int = self._tier_map.get(ticker, 1)  (same path as _get_episode_min_premium)
#     oi_floor = gate_config_store.get("require_oi", tier_int)  (default 0.0 all tiers)
#   When oi_floor > 0, any event whose open_interest < oi_floor is dropped
#   BEFORE the event is appended to the episode — the tick never influences
#   trade_count, weighted_premium, or episode state.
#   When oi_floor == 0 (default), the gate is a no-op — zero behaviour change
#   at default config.
#   ev.open_interest is None-safe: None / missing OI is treated as 0 for
#   comparison purposes. A floor of 0 always passes, so missing OI is only
#   rejected when an operator explicitly sets require_oi > 0.
#   Falls back gracefully when _gate_cfg is None (unit-test environments,
#   no-db mode, cold start).
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

  Instance-method shim retained on RepetitionAccumulator for backward-compat
  with test_accumulator_s4_coverage.py (pre-D3 call contract).

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

ING-010 Gate 2 (2026-05-07):
  _get_episode_min_premium() now merges the static DTE-tier floor with a live
  floor from gate_config_store.get("min_premium", tier_int). The effective
  floor is max(dte_floor, live_floor) — the store can only tighten the gate,
  never lower it below the DTE table. Falls back gracefully when the store
  has not yet loaded (cold start). No constructor params changed.

ING-010-OI (2026-05-07):
  ingest_tick() enforces a per-tier open_interest floor (require_oi gate).
  Default is 0 for all tiers — gate is a no-op at default config.
  Resolution: tier_int = self._tier_map.get(ticker, 1),
              oi_floor = gate_config_store.get("require_oi", tier_int).
  When oi_floor > 0, events with ev.open_interest < oi_floor are dropped
  BEFORE the event is appended — tick never touches episode state.
  ev.open_interest None → treated as 0 (conservative: rejects if floor > 0).
  Falls back to oi_floor = 0.0 when _gate_cfg is None.
"""

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# ING-010: live gate config store for Gate 2 DTE floor enrichment + OI gate.
# ING-010-IMPORT fix: module exports `store`, not `gate_config_store`.
try:
    from services.gate_config_store import store as _gate_cfg
except Exception:  # pragma: no cover — guard for unit-test environments
    _gate_cfg = None  # type: ignore[assignment]

try:
    from parsers.order_side_classifier import order_side_to_direction  # type: ignore[import]
except ImportError:  # pragma: no cover — order_side_classifier lands in S2
    def order_side_to_direction(order_side: str, contract_type: str) -> str:  # type: ignore[misc]
        """Fallback until order_side_classifier.py exists (S2 scope)."""
        if contract_type.upper() == "PUT" and order_side.upper() == "SELL":
            return "REPEAT_BUY"
        return "REPEAT_BUY" if contract_type.upper() == "CALL" else "REPEAT_SELL"


# ---------------------------------------------------------------------------
# Default DTE premium tiers (S4 spec) — COLD-START FALLBACK.
# Live min_premium floor is read from gate_config_store in
# _get_episode_min_premium() and applied as max(dte_floor, live_floor).
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
# ---------------------------------------------------------------------------
def _classify_moneyness_band(ev) -> str:
    """Classify a single event's contract into the full moneyness spectrum.

    ING-011 (D1 deliberation 2026-05-06) — replaces _classify_otm().
    ING-011b (D3 deliberation 2026-05-06) — promoted to module-level function.

    Returns one of: 'DEEP_ITM' | 'ITM' | 'ATM' | 'OTM' | 'DEEP_OTM' | 'UNKNOWN'
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
        "bid_ask_class", "open_interest",
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
        self.bid_ask_class    = d.get("bid_ask_class", "UNKNOWN")
        self.open_interest    = d.get("open_interest")  # None if absent


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
    last_signal_at:       Optional[datetime] = None

    # ING-007: multi-day lookback fields
    prior_days_active:     int  = 0
    prior_days_aggressive: int  = 0
    is_multi_day_repeat:   bool = False

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
        to passive events AND to ITM PUT AT_BID buyer events (ING-011b D1)."""
        total = 0.0
        for e in self.events:
            prem = getattr(e, "premium", 0.0)
            if getattr(e, "is_aggressive", False):
                bac   = getattr(e, "bid_ask_class", "UNKNOWN")
                ctype = str(getattr(e, "contract_type", "") or "").upper()
                if (
                    ctype == "PUT"
                    and bac in ("AT_BID", "BELOW_BID")
                    and _classify_moneyness_band(e) in _ITM_BANDS
                ):
                    total += prem * discount
                else:
                    total += prem
            else:
                total += prem * discount
        return total

    @property
    def is_accelerating(self) -> bool:
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
        return (
            f"{self.ticker} {self.contract_type} "
            f"strike={self.strike} expiry={self.expiry} "
            f"trades={self.trade_count} "
            f"total_premium=${self.total_premium:,.0f}"
        )

    def _majority_itm_band(self) -> bool:
        itm_prem = 0.0
        non_itm_prem = 0.0
        for e in self.events:
            prem = getattr(e, "premium", 0.0)
            band = _classify_moneyness_band(e)
            if band == "UNKNOWN":
                continue
            if band in _ITM_BANDS:
                itm_prem += prem
            else:
                non_itm_prem += prem
        return itm_prem > non_itm_prem

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
        base_direction = "REPEAT_BUY" if buy_prem >= sell_prem else "REPEAT_SELL"

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

    def _classify_moneyness_band(self, ev) -> str:  # noqa: PLR6301
        """Backward-compat instance-method shim (ING-011b fix — 2026-05-07)."""
        return _classify_moneyness_band(ev)

    def _get_episode_min_premium(self, ep: RepetitionEpisode) -> float:
        """
        Return the effective premium floor for this episode.

        Resolution order (ING-010 Gate 2 addition):
          1. Compute dte_floor from self._dte_tiers as before.
          2. Read live_floor = gate_config_store.get("min_premium", tier_int).
          3. effective_floor = max(dte_floor, live_floor).
             The store can only RAISE the floor (tighten the gate).
          4. _min_premium_override semantics unchanged:
             - _tiers_explicit=False: override used directly, no tier table.
             - _tiers_explicit=True:  max(override, dte_floor) before step 3.

        Falls back gracefully when gate_config_store returns None (cold start).
        Never raises.
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
            dte_floor = max(float(self._min_premium_override), dte_floor)

        # ING-010: merge live gate_config_store floor.
        # effective_floor = max(dte_floor, live_floor) — store tightens only.
        if _gate_cfg is not None:
            try:
                live_floor = _gate_cfg.get("min_premium", tier)
                if live_floor is not None:
                    dte_floor = max(dte_floor, float(live_floor))
            except Exception:
                pass  # cold start or store error — dte_floor stands

        return dte_floor

    def get_alert_level(self, episode_or_premium: Union["RepetitionEpisode", float]) -> str:
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
        """Backward-compat shim."""
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
        NOT called by production code."""
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

        # ------------------------------------------------------------------
        # ING-010-OI: per-tier open_interest gate.
        # Evaluated BEFORE appending to episode so rejected ticks never
        # influence trade_count, weighted_premium, or episode state.
        # Default oi_floor == 0.0 (all tiers) — gate is a no-op by default.
        # ------------------------------------------------------------------
        if _gate_cfg is not None:
            try:
                ticker_for_oi = getattr(ev, "ticker", "") or ""
                with self._tier_map_lock:
                    tier_for_oi = self._tier_map.get(ticker_for_oi, 1)
                oi_floor = _gate_cfg.get("require_oi", tier_for_oi)
                if oi_floor > 0.0:
                    ev_oi = getattr(ev, "open_interest", None)
                    ev_oi_val = float(ev_oi) if ev_oi is not None else 0.0
                    if ev_oi_val < oi_floor:
                        logger.debug(
                            "[oi-gate] dropped %s %s $%.0f dte=%d "
                            "oi=%s < floor=%.0f (tier=%d)",
                            getattr(ev, "ticker", "?"),
                            getattr(ev, "contract_type", "?"),
                            getattr(ev, "strike", 0),
                            getattr(ev, "dte", 0),
                            ev_oi,
                            oi_floor,
                            tier_for_oi,
                        )
                        return None
            except Exception:
                pass  # cold start or store error — gate passes

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
