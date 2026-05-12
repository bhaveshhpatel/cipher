# ============================================================================
# signals/signal_engine.py
#
# REARCH-006 — Chunk 2: SignalEngine skeleton + Gate 1–5 evaluators.
#              Chunk 4: compute_conviction_score() + build_signal_row()
#              Chunk 5: _derive_recommendation() (SA/PBE/QA deliberation)
#              Fix 1/4: Collapse dual engine — this file is the single
#                       authority.  services/signal_engine.py is deleted.
#                       Added EpisodeEvalResult, get_engine(), and
#                       evaluate_episode() bridge so signal_store imports
#                       work with zero changes to that file.
#
# Implements the WSJ Steamroom 5-dimension conviction gate over enriched
# RepetitionEpisode objects.  Called once per episode close from the stream
# worker (REARCH-006 Chunk 3 wires the call site).
#
# Gate layout (matches STEAMROOM_REARCH_ROADMAP.md § REARCH-006):
#
#   Gate 1 — Premium Threshold
#     ep.weighted_premium >= get_effective_premium_threshold(alert_level, notional_tier)
#     Tier-aware: notional_tier from REARCH-004 episode enrichment drives the
#     PBE multiplier applied by SignalConfigStore.
#
#   Gate 2 — Ask-Side Execution
#     When sig.require_ask_side == True:
#       ep.ask_side_pct >= sig.ask_side_pct_floor
#     Reads ep.ask_side_pct (float, 0.0–1.0) added by REARCH-004.
#
#   Gate 3 — Vol > OI
#     When sig.require_vol_gt_oi == True:
#       ep.vol_oi_signal == True (episode-aggregate from REARCH-003/004)
#       Fallback: any event in ep.events has vol_oi_signal=True.
#
#   Gate 4 — DTE Quality
#     sig.min_dte <= ep.dte <= sig.max_dte
#     ep.dte is the representative DTE for the episode (first event's DTE or
#     the episode-level field if present).
#
#   Gate 5 — Repetition / Clustering
#     ep.trade_count >= sig.min_trade_count
#
#   Post-gate scoring:
#     steamroom_score = count of passed gates (0–5).
#     Alert level is determined by the highest-tier premium threshold cleared.
#     Episode only emits when steamroom_score >= sig.steamroom_score_floor (default 3).
#
# Chunk 4 additions (module-level pure functions):
#
#   compute_conviction_score(episode, cfg) -> int [0-5]
#     Independent of SignalEngine.evaluate() — used by build_signal_row() and
#     any caller that needs a bare integer score without the full GateResult
#     overhead.  Mirrors the 5 gate dimensions but reads REARCH-004 episode
#     attributes directly (total_premium, ask_side_pct, vol_oi_signal,
#     dte_bucket, trade_count) rather than going through the per-gate methods.
#
#   build_signal_row(episode, alert_level, direction, cfg, **kwargs) -> dict
#     Assembles the exact insert dict for signal_history keyed to the
#     post-REARCH-010 schema (migration 024_rearch010_schema_purge.sql).
#     Validates vocab, normalises types, snapshots Steamroom quality columns,
#     and intentionally omits all retired columns.
#
# Chunk 5 addition:
#
#   _derive_recommendation(conviction_score, direction, ask_side_confirmed) -> str
#     Private pure function.  Returns one of the 5 machine-readable Steamroom
#     recommendation enum values:
#       BUY_CALLS    — Bullish, conviction >= 3, ask-side confirmed
#       BUY_PUTS     — Bearish, conviction >= 3, ask-side confirmed
#       FOLLOW_SWEEP — conviction == 5 (GOLDEN), ask-side confirmed
#       WATCH        — conviction 1–2, OR conviction >= 3 but ask-side failed
#       NO_ACTION    — conviction 0, or NEUTRAL/ambiguous direction
#
#     Ask-side confirmation is a HARD GATE for BUY_* recommendations:
#     conviction=4 with ask_side failed → WATCH (not BUY_*).  A partial-score
#     episode without confirmed execution quality is a known Steamroom
#     disqualifier (QA deliberation consensus).
#
#     FOLLOW_SWEEP takes precedence and is checked first: conviction==5
#     with ask-side confirmed overrides the directional BUY_* path.
#
# Authoritative engine boundary note (Fix 1/4 — REARCH-006 pre-merge):
#   This file (signals/signal_engine.py) is the SOLE authority for all gate
#   evaluation logic.  services/signal_engine.py has been deleted.  Any
#   future gate param changes belong here exclusively.
#
#   Two public APIs are exposed for historical callers:
#     evaluate(ep)          — object-based API, returns GateResult
#     evaluate_episode(ep)  — dict-based API, returns EpisodeEvalResult
#                             (used by signal_store._bus_signal_listener)
#   Both delegate to the same internal _eval_gate_N() methods.
#
# Deploy notes:
#   - No DB I/O in evaluate() — all config via get_param() snapshot reads.
#   - One SignalEngine instance shared across workers; evaluate() is thread-safe
#     (no mutable instance state touched during a call).
#   - vol_oi_signal fallback scan is O(n) over ep.events; for typical episode
#     sizes (2–15 events) this is negligible.  Do not use for batch replay over
#     thousands of episodes without profiling.
# ============================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional

from signals.signal_gate import GateVerdict
from services.signal_config_store import get_param, get_effective_premium_threshold

log = logging.getLogger("signal_engine")

# ---------------------------------------------------------------------------
# Alert level key constants — match keys in signal_config_store.SIGNAL_CONFIG_TYPES
# ---------------------------------------------------------------------------
_ALERT_GOLDEN     = "sig.golden_sweep_premium"
_ALERT_BLOCK      = "sig.block_premium"
_ALERT_NOTEWORTHY = "sig.noteworthy_premium"

# Evaluation order: highest conviction first so the returned alert_level
# reflects the best label the episode qualifies for.
_ALERT_LEVEL_KEYS: tuple[tuple[str, str], ...] = (
    (_ALERT_GOLDEN,     "GOLDEN"),
    (_ALERT_BLOCK,      "BLOCK"),
    (_ALERT_NOTEWORTHY, "NOTEWORTHY"),
)

# Gate name constants (used as keys in GateResult.gates dict)
_GATE_PREMIUM    = "gate_1_premium"
_GATE_ASK_SIDE   = "gate_2_ask_side"
_GATE_VOL_OI     = "gate_3_vol_oi"
_GATE_DTE        = "gate_4_dte"
_GATE_REPETITION = "gate_5_repetition"

_ALL_GATE_NAMES: tuple[str, ...] = (
    _GATE_PREMIUM,
    _GATE_ASK_SIDE,
    _GATE_VOL_OI,
    _GATE_DTE,
    _GATE_REPETITION,
)

# ---------------------------------------------------------------------------
# Chunk 4 — vocab sets (shared by compute_conviction_score + build_signal_row)
# ---------------------------------------------------------------------------

# Tiers that satisfy D3 (premium quality) — kept for _eval_gate_1 internal use
_QUALIFYING_TIERS: frozenset[str] = frozenset({"NOTEWORTHY", "BLOCK", "GOLDEN"})

# DTE buckets that FAIL D4 (too short-dated or too far out)
_DISQUALIFYING_DTE_BUCKETS: frozenset[str] = frozenset({"0-7", "90+"})

# Valid REARCH-010 vocab for signal_history columns
_VALID_ALERT_LEVELS: frozenset[str] = frozenset({"WATCH", "NOTEWORTHY", "BLOCK", "GOLDEN"})
_VALID_DIRECTIONS:   frozenset[str] = frozenset({"BULLISH", "BEARISH", "NEUTRAL"})

# ---------------------------------------------------------------------------
# Chunk 5 — recommendation enum vocab
# ---------------------------------------------------------------------------

# The 5-value Steamroom recommendation enum (machine-readable verdict).
# CHECK constraint in migration 033 enforces this set in the DB.
_RECOMMENDATION_BUY_CALLS    = "BUY_CALLS"
_RECOMMENDATION_BUY_PUTS     = "BUY_PUTS"
_RECOMMENDATION_FOLLOW_SWEEP = "FOLLOW_SWEEP"
_RECOMMENDATION_WATCH        = "WATCH"
_RECOMMENDATION_NO_ACTION    = "NO_ACTION"

_VALID_RECOMMENDATIONS: frozenset[str] = frozenset({
    _RECOMMENDATION_BUY_CALLS,
    _RECOMMENDATION_BUY_PUTS,
    _RECOMMENDATION_FOLLOW_SWEEP,
    _RECOMMENDATION_WATCH,
    _RECOMMENDATION_NO_ACTION,
})

# Conviction floor for directional buy recommendations.
_BUY_CONVICTION_FLOOR = 3

# DTE bucket → representative midpoint (days) — mirrors ingestion/processor.py
_DTE_BUCKET_MIDPOINTS = {
    "SHORT": 4,
    "MID":   19,
    "LONG":  45,
    "XLONG": 75,
}

# Fraction of noteworthy_threshold used as the D1 WATCH band floor.
# Premiums in [noteworthy_threshold * _WATCH_FLOOR_FACTOR, noteworthy_threshold)
# pass D1 via watch_floor and resolve to WATCH alert level.
_WATCH_FLOOR_FACTOR = 0.5


# ---------------------------------------------------------------------------
# EpisodeEvalResult — dict-caller API result type (used by signal_store)
# ---------------------------------------------------------------------------

@dataclass
class EpisodeEvalResult:
    """
    Output of SignalEngine.evaluate_episode().

    This is the result type used by dict-based callers (signal_store) that
    pass episode dicts rather than RepetitionEpisode objects.  It mirrors the
    former services/signal_engine.EpisodeEvalResult contract exactly so
    signal_store requires zero changes.

    Attributes
    ----------
    passed              True when all enabled dimensions cleared.
    alert_level         GOLDEN / BLOCK / NOTEWORTHY / WATCH / FAIL.
                        FAIL means passed=False (no signal should be written).
    failing_dimensions  Non-empty list of dimension names ("D1_PREMIUM" etc)
                        when passed=False.  Empty list when passed=True.
    effective_threshold The tier-adjusted dollar threshold applied for the
                        D1 gate (useful for logging / backtest).
    premium             Raw total_premium from the episode (for caller logging).
    ticker              Episode ticker (for caller logging).
    """
    passed:              bool
    alert_level:         str
    failing_dimensions:  List[str] = field(default_factory=list)
    effective_threshold: float = 0.0
    premium:             float = 0.0
    ticker:              str = ""


# ---------------------------------------------------------------------------
# GateResult — returned by SignalEngine.evaluate()
# ---------------------------------------------------------------------------

@dataclass
class GateResult:
    """Full evaluation result for a single RepetitionEpisode.

    Attributes
    ----------
    passed:
        True only when steamroom_score >= sig.steamroom_score_floor AND
        Gate 1 (premium) passed.  Gate 1 is a mandatory gate — an episode
        that scores 5/5 on everything except premium is not a signal.
    gates:
        Per-gate GateVerdict keyed by gate name constant (``gate_1_premium``
        through ``gate_5_repetition``).  Always contains all five keys
        regardless of evaluation short-circuit so callers can inspect which
        gates failed without branching on key presence.
    steamroom_score:
        Integer 0–5 counting how many gates passed.  Used for the score-floor
        gate and surfaced in signal_history.detail JSONB (REARCH-010).
    alert_level:
        Highest premium tier the episode cleared: ``"GOLDEN"`` | ``"BLOCK"``
        | ``"NOTEWORTHY"`` | ``None``.  None when Gate 1 failed entirely.
    config_snapshot:
        Copy of the config values consumed during this evaluation.  Stored
        in signal_history.detail so the emit is deterministically replayable
        without re-querying signal_config at analysis time.
    """
    passed:          bool
    gates:           dict[str, GateVerdict]
    steamroom_score: int
    alert_level:     Optional[str]
    config_snapshot: dict[str, Any]


# ---------------------------------------------------------------------------
# SignalEngine
# ---------------------------------------------------------------------------

class SignalEngine:
    """Stateless evaluator for the WSJ Steamroom 5-dimension conviction gate.

    Instantiate once at startup; call evaluate() per episode close.
    All configuration is read from the live signal_config_store snapshot at
    call time — no constructor arguments required for runtime tuning.

    Two evaluation APIs are provided:

    evaluate(ep) -> GateResult
        Object-based API.  ep must be a RepetitionEpisode with object
        attributes (weighted_premium, ask_side_pct, etc.).
        Used by stream_worker and any future pipeline stage that has the
        episode object directly.

    evaluate_episode(ep: dict) -> EpisodeEvalResult
        Dict-based bridge API.  ep is a plain dict with the same keys as
        a flow_episodes DB row.  Adapts to the object-based path internally
        and returns EpisodeEvalResult so signal_store callers work unchanged.
        This is the API formerly exported by services/signal_engine.py.

    Parameters
    ----------
    strict_gate_1:
        When True (default), passed=False if Gate 1 fails regardless of
        steamroom_score.  This enforces premium as a mandatory gate.
        Set to False only in backtest/test contexts where premium-bypass
        scenarios need to be exercised.
    """

    def __init__(self, strict_gate_1: bool = True) -> None:
        self._strict_gate_1 = strict_gate_1

    # ------------------------------------------------------------------
    # Public API — object-based
    # ------------------------------------------------------------------

    def evaluate(self, ep) -> GateResult:
        """Evaluate *ep* against all five Steamroom conviction gates.

        Parameters
        ----------
        ep:
            A ``RepetitionEpisode`` instance produced by
            ``repetition_accumulator.py``.  Must expose:
              - ``weighted_premium`` (float)
              - ``trade_count`` (int)
              - ``ask_side_pct`` (float, 0.0–1.0) — added by REARCH-004
              - ``notional_tier`` (str: "tier1" | "tier2" | "tier3") — REARCH-004
              - ``vol_oi_signal`` (bool, optional) — episode-aggregate REARCH-004;
                fallback to per-event scan if absent
              - ``dte`` (int, optional) — episode-level DTE; fallback to
                first event's dte field if absent
              - ``events`` (list) — raw event objects (for fallback reads)

        Returns
        -------
        GateResult
            Full verdict with per-gate breakdown, steamroom_score, alert_level,
            and the config snapshot consumed during evaluation.
        """
        cfg = self._read_config_snapshot()
        gates: dict[str, GateVerdict] = {}

        # ── Gate 1: Premium Threshold ────────────────────────────────────────
        g1, alert_level = self._eval_gate_1(ep, cfg)
        gates[_GATE_PREMIUM] = g1

        # ── Gate 2: Ask-Side Execution ───────────────────────────────────────
        gates[_GATE_ASK_SIDE] = self._eval_gate_2(ep, cfg)

        # ── Gate 3: Vol > OI ─────────────────────────────────────────────────
        gates[_GATE_VOL_OI] = self._eval_gate_3(ep, cfg)

        # ── Gate 4: DTE Quality ──────────────────────────────────────────────
        gates[_GATE_DTE] = self._eval_gate_4(ep, cfg)

        # ── Gate 5: Repetition / Clustering ─────────────────────────────────
        gates[_GATE_REPETITION] = self._eval_gate_5(ep, cfg)

        # ── Steamroom score ──────────────────────────────────────────────────
        steamroom_score = sum(1 for g in gates.values() if g.passed)

        # ── Final pass/fail decision ─────────────────────────────────────────
        score_floor: int = cfg.get("sig.steamroom_score_floor", 3)

        passed = steamroom_score >= score_floor

        # Gate 1 is mandatory — premium failure overrides score regardless.
        if self._strict_gate_1 and not gates[_GATE_PREMIUM].passed:
            passed = False

        if not passed and gates[_GATE_PREMIUM].passed and steamroom_score < score_floor:
            log.debug(
                "[signal_engine] Episode rejected: score=%d/%d below floor=%d  ticker=%s",
                steamroom_score, len(_ALL_GATE_NAMES), score_floor,
                getattr(ep, "ticker", "?"),
            )

        return GateResult(
            passed=passed,
            gates=gates,
            steamroom_score=steamroom_score,
            alert_level=alert_level if gates[_GATE_PREMIUM].passed else None,
            config_snapshot=cfg,
        )

    # ------------------------------------------------------------------
    # Public API — dict-based bridge (EpisodeEvalResult)
    # ------------------------------------------------------------------

    def evaluate_episode(self, episode: dict) -> EpisodeEvalResult:
        """Dict-based evaluation bridge — used by signal_store._bus_signal_listener.

        Accepts the same dict schema as a flow_episodes DB row (or bus message
        payload) and returns an EpisodeEvalResult with the same field contract
        as the former services/signal_engine.SignalEngine.evaluate_episode().

        Internally wraps the episode dict in a lightweight _EpisodeProxy so
        the object-attribute-based evaluate() path can be reused unchanged.
        This is the single code path for all gate evaluation — no duplication.

        Parameters
        ----------
        episode : dict
            Must contain at minimum:
              ticker, total_premium, notional_tier, ask_side_pct,
              vol_oi_signal, dte_bucket, trade_count.

        Returns
        -------
        EpisodeEvalResult
            passed, alert_level, failing_dimensions, effective_threshold,
            premium, ticker.
        """
        ticker  = episode.get("ticker", "UNKNOWN")
        premium = float(episode.get("total_premium") or 0)
        tier    = episode.get("notional_tier") or "tier1"

        # Wrap dict in a proxy so evaluate()'s getattr() calls work.
        proxy = _EpisodeProxy(episode)
        result = self.evaluate(proxy)

        if not result.passed:
            # Map GateResult gate names back to D-dimension labels for
            # EpisodeEvalResult.failing_dimensions (signal_store log format).
            failing = _gate_names_to_dimensions(result.gates)

            # Compute effective noteworthy threshold for logging parity.
            effective_threshold = get_effective_premium_threshold(
                _ALERT_NOTEWORTHY, tier
            ) or 50_000.0

            return EpisodeEvalResult(
                passed=False,
                alert_level="FAIL",
                failing_dimensions=failing,
                effective_threshold=effective_threshold,
                premium=premium,
                ticker=ticker,
            )

        alert_level = result.alert_level or "WATCH"
        effective_threshold = get_effective_premium_threshold(
            _ALERT_NOTEWORTHY, tier
        ) or 50_000.0

        return EpisodeEvalResult(
            passed=True,
            alert_level=alert_level,
            failing_dimensions=[],
            effective_threshold=effective_threshold,
            premium=premium,
            ticker=ticker,
        )

    # ------------------------------------------------------------------
    # Per-gate evaluators (private)
    # ------------------------------------------------------------------

    @staticmethod
    def _eval_gate_1(ep, cfg: dict) -> tuple[GateVerdict, Optional[str]]:
        """Gate 1 — Premium Threshold (tier-aware).

        Uses watch_floor (noteworthy_threshold * _WATCH_FLOOR_FACTOR) as the
        D1 hard pass floor, not noteworthy_threshold directly.  This means
        premiums in [watch_floor, noteworthy_threshold) pass D1 and resolve
        to WATCH alert level — consistent with the docstring contract.

        Returns
        -------
        (GateVerdict, alert_level | None)
        """
        premium: float = getattr(ep, "weighted_premium", 0.0) or 0.0
        # Dict proxy: also check total_premium for evaluate_episode() path.
        if premium == 0.0:
            premium = float(getattr(ep, "total_premium", 0.0) or 0.0)
        notional_tier: str = getattr(ep, "notional_tier", "tier1") or "tier1"

        # Walk GOLDEN → BLOCK → NOTEWORTHY — return first cleared tier.
        for level_key, level_name in _ALERT_LEVEL_KEYS:
            threshold = get_effective_premium_threshold(level_key, notional_tier)
            if premium >= threshold:
                return (
                    GateVerdict(True, f"premium_cleared_{level_name.lower()}"),
                    level_name,
                )

        # Below NOTEWORTHY — check WATCH band floor.
        noteworthy_floor = get_effective_premium_threshold(_ALERT_NOTEWORTHY, notional_tier)
        watch_floor = noteworthy_floor * _WATCH_FLOOR_FACTOR

        if premium >= watch_floor:
            return (
                GateVerdict(True, f"premium_cleared_watch: {premium:,.0f} >= watch_floor={watch_floor:,.0f}"),
                "WATCH",
            )

        # Below watch_floor — Gate 1 hard fail.
        return (
            GateVerdict(
                False,
                f"premium_below_watch_floor: {premium:,.0f} < {watch_floor:,.0f}"
                f" (noteworthy={noteworthy_floor:,.0f}, tier={notional_tier})",
            ),
            None,
        )

    @staticmethod
    def _eval_gate_2(ep, cfg: dict) -> GateVerdict:
        """Gate 2 — Ask-Side Execution.

        Skipped (passes vacuously) when ``sig.require_ask_side`` is False.
        """
        require: bool = cfg.get("sig.require_ask_side", True)
        if not require:
            return GateVerdict(True, "ask_side_not_required")

        floor: float = cfg.get("sig.ask_side_pct_floor", 0.6)
        ask_side_pct: float = getattr(ep, "ask_side_pct", None)

        if ask_side_pct is None:
            log.debug(
                "[signal_engine] Gate 2: ep.ask_side_pct missing on ticker=%s — treating as 0.0",
                getattr(ep, "ticker", "?"),
            )
            ask_side_pct = 0.0

        if ask_side_pct >= floor:
            return GateVerdict(True, f"ask_side_pct={ask_side_pct:.2f}>={floor:.2f}")

        return GateVerdict(
            False,
            f"ask_side_pct={ask_side_pct:.2f} < floor={floor:.2f}",
        )

    @staticmethod
    def _eval_gate_3(ep, cfg: dict) -> GateVerdict:
        """Gate 3 — Vol > OI.

        Skipped (passes vacuously) when ``sig.require_vol_gt_oi`` is False.

        Primary source: ``ep.vol_oi_signal`` (bool) added by REARCH-004.
        Fallback: scan ``ep.events`` for any event with ``vol_oi_signal=True``.
        """
        require: bool = cfg.get("sig.require_vol_gt_oi", True)
        if not require:
            return GateVerdict(True, "vol_oi_not_required")

        ep_vol_oi = getattr(ep, "vol_oi_signal", None)
        if ep_vol_oi is not None:
            if ep_vol_oi:
                return GateVerdict(True, "vol_oi_signal=True (episode-aggregate)")
            return GateVerdict(False, "vol_oi_signal=False (episode-aggregate)")

        events = getattr(ep, "events", []) or []
        for ev in events:
            if getattr(ev, "vol_oi_signal", False):
                return GateVerdict(True, "vol_oi_signal=True (event-level fallback)")

        return GateVerdict(False, "vol_oi_signal=False (no qualifying event found)")

    @staticmethod
    def _eval_gate_4(ep, cfg: dict) -> GateVerdict:
        """Gate 4 — DTE Quality.

        Reads ``ep.dte`` first; falls back to dte_bucket midpoint mapping if
        the episode-level field is absent (dict-based evaluate_episode path).
        """
        min_dte: int = cfg.get("sig.min_dte", 5)
        max_dte: int = cfg.get("sig.max_dte", 60)

        dte: Optional[int] = getattr(ep, "dte", None)
        if dte is None:
            # Fallback 1: first event's DTE.
            events = getattr(ep, "events", []) or []
            if events:
                dte = getattr(events[0], "dte", None)

        if dte is None:
            # Fallback 2: dte_bucket midpoint (dict path from evaluate_episode).
            dte_bucket = (getattr(ep, "dte_bucket", "") or "").upper()
            dte = _DTE_BUCKET_MIDPOINTS.get(dte_bucket)

        if dte is None:
            return GateVerdict(False, "dte_unknown: cannot evaluate DTE gate")

        dte = int(dte)

        if dte < min_dte:
            return GateVerdict(False, f"dte={dte} < min_dte={min_dte}")
        if dte > max_dte:
            return GateVerdict(False, f"dte={dte} > max_dte={max_dte}")

        return GateVerdict(True, f"dte={dte} in [{min_dte}, {max_dte}]")

    @staticmethod
    def _eval_gate_5(ep, cfg: dict) -> GateVerdict:
        """Gate 5 — Repetition / Clustering."""
        min_trades: int = cfg.get("sig.min_trade_count", 2)
        trade_count: int = getattr(ep, "trade_count", 0) or 0

        if trade_count >= min_trades:
            return GateVerdict(True, f"trade_count={trade_count}>={min_trades}")

        return GateVerdict(False, f"trade_count={trade_count} < min_trade_count={min_trades}")

    # ------------------------------------------------------------------
    # Config snapshot builder
    # ------------------------------------------------------------------

    @staticmethod
    def _read_config_snapshot() -> dict[str, Any]:
        """Pull all signal-engine config keys from the live snapshot."""
        return {
            "sig.golden_sweep_premium":          get_param("sig.golden_sweep_premium",         1_000_000.0),
            "sig.block_premium":                 get_param("sig.block_premium",                 500_000.0),
            "sig.noteworthy_premium":            get_param("sig.noteworthy_premium",            50_000.0),
            "sig.golden_sweep_premium_t2_mult":  get_param("sig.golden_sweep_premium_t2_mult",  0.5),
            "sig.golden_sweep_premium_t3_mult":  get_param("sig.golden_sweep_premium_t3_mult",  0.2),
            "sig.block_premium_t2_mult":         get_param("sig.block_premium_t2_mult",         0.5),
            "sig.block_premium_t3_mult":         get_param("sig.block_premium_t3_mult",         0.2),
            "sig.noteworthy_premium_t2_mult":    get_param("sig.noteworthy_premium_t2_mult",    0.5),
            "sig.noteworthy_premium_t3_mult":    get_param("sig.noteworthy_premium_t3_mult",    0.2),
            "sig.require_ask_side":              get_param("sig.require_ask_side",              True),
            "sig.ask_side_pct_floor":            get_param("sig.ask_side_pct_floor",            0.6),
            "sig.require_vol_gt_oi":             get_param("sig.require_vol_gt_oi",             True),
            "sig.min_dte":                       get_param("sig.min_dte",                       5),
            "sig.max_dte":                       get_param("sig.max_dte",                       60),
            "sig.min_trade_count":               get_param("sig.min_trade_count",               2),
            "sig.steamroom_score_floor":         get_param("sig.steamroom_score_floor",         3),
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_engine_singleton: Optional[SignalEngine] = None


def get_engine() -> SignalEngine:
    """Return the module-level SignalEngine singleton.

    Equivalent to the former services/signal_engine.get_engine() — same
    contract, same lazy-init pattern.  Production callers (signal_store,
    stream_worker) call this; tests should instantiate SignalEngine() directly
    and patch as needed.

    The singleton does not hold any DB connection — it is safe to call from
    any thread or async context.
    """
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = SignalEngine()
        log.info("[signal_engine] singleton initialised (signals/ authority)")
    return _engine_singleton


# module-level alias — stream_worker imports `engine` directly
engine = get_engine()


# ---------------------------------------------------------------------------
# _EpisodeProxy — adapts a dict to the attribute-access contract of evaluate()
# ---------------------------------------------------------------------------

class _EpisodeProxy:
    """Thin wrapper that exposes episode dict keys as attributes.

    Used internally by evaluate_episode() so the dict-based API can reuse
    evaluate()'s object-attribute path without copying logic.
    """
    __slots__ = ("_d",)

    def __init__(self, d: dict) -> None:
        object.__setattr__(self, "_d", d)

    def __getattr__(self, name: str):
        d = object.__getattribute__(self, "_d")
        if name in d:
            return d[name]
        # Attributes evaluate() depends on that dict keys may not have:
        # weighted_premium → total_premium fallback
        if name == "weighted_premium":
            return float(d.get("total_premium") or 0.0)
        # events — Gate 3/4 fallback; dict episodes typically don't carry it
        if name == "events":
            return []
        # dte — Gate 4 falls back to dte_bucket midpoint internally
        raise AttributeError(name)


# ---------------------------------------------------------------------------
# _gate_names_to_dimensions — maps GateResult gate keys → D-label strings
# ---------------------------------------------------------------------------

_GATE_TO_DIMENSION: dict[str, str] = {
    _GATE_PREMIUM:    "D1_PREMIUM",
    _GATE_ASK_SIDE:   "D2_ASK_SIDE",
    _GATE_VOL_OI:     "D3_VOL_GT_OI",
    _GATE_DTE:        "D4_DTE",
    _GATE_REPETITION: "D5_REPETITION",
}


def _gate_names_to_dimensions(gates: dict[str, GateVerdict]) -> list[str]:
    """Return failing D-dimension labels for EpisodeEvalResult.failing_dimensions."""
    return [
        _GATE_TO_DIMENSION[gate_name]
        for gate_name, verdict in gates.items()
        if not verdict.passed and gate_name in _GATE_TO_DIMENSION
    ]


# ============================================================================
# CHUNK 4-A  compute_conviction_score()
# ============================================================================

def compute_conviction_score(episode: Any, cfg: Any) -> int:
    """Compute the WSJ Steamroom conviction score (0–5) for a RepetitionEpisode.

    Each of the five Steamroom dimensions contributes one point.

    D-dimension mapping (aligned with gate numbering in _eval_gate_N and the
    STEAMROOM_REARCH_ROADMAP.md gate spec):

      D1 — Premium floor          total_premium >= noteworthy_floor * _WATCH_FLOOR_FACTOR
                                  (mirrors _eval_gate_1 watch-band check exactly)
      D2 — Ask-side execution     ask_side_pct >= cfg.ask_side_pct_floor
      D3 — Volume > Open Interest vol_oi_signal == True
      D4 — DTE in signal window   dte_bucket NOT in {0-7, 90+}
      D5 — Repetition / cluster   trade_count >= cfg.min_trade_count
    """
    def _get(key: str, default):
        try:
            return getattr(cfg, key)
        except AttributeError:
            pass
        try:
            return cfg[key]
        except (KeyError, TypeError):
            pass
        return default

    score = 0

    # D1 — Premium meets watch-band floor (consistent with _eval_gate_1)
    # watch_floor = noteworthy_premium * _WATCH_FLOOR_FACTOR
    noteworthy_floor: float = float(
        _get("noteworthy_premium", _get("sig.noteworthy_premium", 50_000.0))
    )
    watch_floor: float = noteworthy_floor * _WATCH_FLOOR_FACTOR
    total_premium: float = float(getattr(episode, "total_premium", 0.0) or 0.0)
    if total_premium >= watch_floor:
        score += 1

    # D2 — Ask-side execution dominance
    ask_pct: float | None = getattr(episode, "ask_side_pct", None)
    floor: float = float(_get("ask_side_pct_floor", _get("sig.ask_side_pct_floor", 0.6)))
    if ask_pct is not None and ask_pct >= floor:
        score += 1

    # D3 — Volume > Open Interest
    if getattr(episode, "vol_oi_signal", False):
        score += 1

    # D4 — DTE in signal window (not 0-7 days, not 90+ days)
    dte_bucket: str | None = getattr(episode, "dte_bucket", None)
    if dte_bucket is not None and dte_bucket not in _DISQUALIFYING_DTE_BUCKETS:
        score += 1

    # D5 — Repetition / clustering
    trade_count: int = int(getattr(episode, "trade_count", 0) or 0)
    min_trades: int = int(_get("min_trade_count", _get("sig.min_trade_count", 3)))
    if trade_count >= min_trades:
        score += 1

    return score


# ============================================================================
# CHUNK 5  _derive_recommendation()
# ============================================================================

def _derive_recommendation(
    conviction_score: int,
    direction: str,
    ask_side_confirmed: bool,
) -> str:
    """Derive the Steamroom recommendation enum from score + direction + ask-side."""
    if conviction_score == 0 or direction not in ("BULLISH", "BEARISH"):
        return _RECOMMENDATION_NO_ACTION

    if conviction_score == 5 and ask_side_confirmed:
        return _RECOMMENDATION_FOLLOW_SWEEP

    if conviction_score >= _BUY_CONVICTION_FLOOR and ask_side_confirmed:
        if direction == "BULLISH":
            return _RECOMMENDATION_BUY_CALLS
        if direction == "BEARISH":
            return _RECOMMENDATION_BUY_PUTS

    return _RECOMMENDATION_WATCH


# ============================================================================
# CHUNK 4-B  build_signal_row()
# ============================================================================

def build_signal_row(
    episode: Any,
    alert_level: str,
    direction: str,
    cfg: Any,
    *,
    conviction_score: int | None = None,
    backtest_score: float = 0.0,
    reasoning: str | None = None,
    is_accelerating: bool = False,
    signal_ts: datetime | None = None,
) -> dict[str, Any]:
    """Build the signal_history insert dict from an enriched RepetitionEpisode."""
    if alert_level not in _VALID_ALERT_LEVELS:
        raise ValueError(
            f"build_signal_row: invalid alert_level={alert_level!r}. "
            f"Must be one of {sorted(_VALID_ALERT_LEVELS)}"
        )
    if direction not in _VALID_DIRECTIONS:
        raise ValueError(
            f"build_signal_row: invalid direction={direction!r}. "
            f"Must be one of {sorted(_VALID_DIRECTIONS)}"
        )

    ticker: str | None = getattr(episode, "symbol", None) or getattr(episode, "ticker", None)
    if not ticker:
        raise ValueError(
            "build_signal_row: episode has no symbol/ticker attribute"
        )

    if conviction_score is None:
        conviction_score = compute_conviction_score(episode, cfg)

    composite_score: float = round(conviction_score / 5.0, 3)

    total_premium: float | None = getattr(episode, "total_premium", None)
    trade_count:   int   | None = getattr(episode, "trade_count", None)
    ask_side_pct:  float | None = getattr(episode, "ask_side_pct", None)
    episode_id:    str   | None = getattr(episode, "episode_id", None)

    vol_oi_ratio: float | None = getattr(episode, "vol_oi_ratio", None)
    if vol_oi_ratio is None:
        vol = getattr(episode, "contract_volume_at_close", None)
        oi  = getattr(episode, "contract_oi_at_open", None)
        if vol is not None and oi and oi > 0:
            vol_oi_ratio = round(float(vol) / float(oi), 4)

    raw_contract_type: str | None = getattr(episode, "contract_type", None)
    contract_type: str | None = raw_contract_type.upper() if raw_contract_type else None

    def _get_cfg(key: str, default):
        try:
            return getattr(cfg, key)
        except AttributeError:
            pass
        try:
            return cfg[key]
        except (KeyError, TypeError):
            pass
        return default

    ask_floor: float = float(_get_cfg("ask_side_pct_floor", _get_cfg("sig.ask_side_pct_floor", 0.6)))
    ask_side_confirmed: bool = (
        ask_side_pct is not None and ask_side_pct >= ask_floor
    )

    recommendation: str = _derive_recommendation(
        conviction_score=conviction_score,
        direction=direction,
        ask_side_confirmed=ask_side_confirmed,
    )

    if signal_ts is None:
        signal_ts = datetime.now(tz=timezone.utc)

    return {
        "ticker":                   ticker,
        "alert_level":              alert_level,
        "direction":                direction,
        "composite_score":          composite_score,
        "backtest_score":           round(float(backtest_score), 3),
        "total_premium":            float(total_premium) if total_premium is not None else None,
        "trade_count":              int(trade_count) if trade_count is not None else None,
        "contract_type":            contract_type,
        "episode_steamroom_score":  conviction_score,
        "ask_side_pct":             round(float(ask_side_pct), 4) if ask_side_pct is not None else None,
        "vol_oi_ratio":             float(vol_oi_ratio) if vol_oi_ratio is not None else None,
        "episode_id":               str(episode_id) if episode_id is not None else None,
        "reasoning":                reasoning,
        "is_accelerating":          is_accelerating,
        "signal_ts":                signal_ts.isoformat(),
        "recommendation":           recommendation,
    }
