# ============================================================================
# signals/signal_engine.py
#
# REARCH-006 — Chunk 2: SignalEngine skeleton + Gate 1–5 evaluators.
#              Chunk 4: compute_conviction_score() + build_signal_row()
#              Chunk 5: _derive_recommendation() (SA/PBE/QA deliberation)
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
#     attributes directly (ask_side_pct, vol_oi_signal, notional_tier,
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
from typing import Any, Optional

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

# Tiers that satisfy D3 (premium quality)
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
    # Public API
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
    # Per-gate evaluators (private)
    # ------------------------------------------------------------------

    @staticmethod
    def _eval_gate_1(ep, cfg: dict) -> tuple[GateVerdict, Optional[str]]:
        """Gate 1 — Premium Threshold (tier-aware).

        Determines both pass/fail and the alert_level label in one pass so
        we only iterate _ALERT_LEVEL_KEYS once.

        Returns
        -------
        (GateVerdict, alert_level | None)
        """
        premium: float = getattr(ep, "weighted_premium", 0.0) or 0.0
        notional_tier: str = getattr(ep, "notional_tier", "tier1") or "tier1"

        for level_key, level_name in _ALERT_LEVEL_KEYS:
            threshold = get_effective_premium_threshold(level_key, notional_tier)
            if premium >= threshold:
                return (
                    GateVerdict(True, f"premium_cleared_{level_name.lower()}"),
                    level_name,
                )

        # Below NOTEWORTHY floor — Gate 1 fails.
        noteworthy_floor = get_effective_premium_threshold(_ALERT_NOTEWORTHY, notional_tier)
        return (
            GateVerdict(
                False,
                f"premium_below_floor: {premium:,.0f} < {noteworthy_floor:,.0f}"
                f" (tier={notional_tier})",
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
            # REARCH-004 field absent — degrade gracefully: treat as 0.0.
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
        The fallback handles cold-start windows where the episode object was
        created before REARCH-004 enrichment was deployed.
        """
        require: bool = cfg.get("sig.require_vol_gt_oi", True)
        if not require:
            return GateVerdict(True, "vol_oi_not_required")

        # Primary: episode-level aggregate from REARCH-004.
        ep_vol_oi = getattr(ep, "vol_oi_signal", None)
        if ep_vol_oi is not None:
            if ep_vol_oi:
                return GateVerdict(True, "vol_oi_signal=True (episode-aggregate)")
            return GateVerdict(False, "vol_oi_signal=False (episode-aggregate)")

        # Fallback: per-event scan (pre-REARCH-004 episode objects).
        events = getattr(ep, "events", []) or []
        for ev in events:
            if getattr(ev, "vol_oi_signal", False):
                return GateVerdict(True, "vol_oi_signal=True (event-level fallback)")

        return GateVerdict(False, "vol_oi_signal=False (no qualifying event found)")

    @staticmethod
    def _eval_gate_4(ep, cfg: dict) -> GateVerdict:
        """Gate 4 — DTE Quality.

        Reads ``ep.dte`` first; falls back to ``ep.events[0].dte`` if the
        episode-level field is absent.  0DTE is handled: min_dte default is 5
        but can be set to 0 via the admin UI to include 0DTE sweeps.
        """
        min_dte: int = cfg.get("sig.min_dte", 5)
        max_dte: int = cfg.get("sig.max_dte", 60)

        dte: Optional[int] = getattr(ep, "dte", None)
        if dte is None:
            # Fallback to first event's DTE.
            events = getattr(ep, "events", []) or []
            if events:
                dte = getattr(events[0], "dte", None)

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
        """Gate 5 — Repetition / Clustering.

        Compares ``ep.trade_count`` against ``sig.min_trade_count``.
        trade_count is a property on RepetitionEpisode returning len(ep.events).
        """
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
        """Pull all signal-engine config keys from the live snapshot.

        Returns a plain dict so GateResult.config_snapshot is self-contained
        and can be serialised into signal_history.detail JSONB without any
        further processing.

        Reads are all get_param() calls — no DB I/O, GIL-safe, O(1) per key.
        """
        return {
            # Dimension 1 — base thresholds (tier multipliers applied inside
            # get_effective_premium_threshold; stored here for audit trail)
            "sig.golden_sweep_premium":          get_param("sig.golden_sweep_premium",         1_000_000.0),
            "sig.block_premium":                 get_param("sig.block_premium",                 500_000.0),
            "sig.noteworthy_premium":            get_param("sig.noteworthy_premium",            50_000.0),
            "sig.golden_sweep_premium_t2_mult":  get_param("sig.golden_sweep_premium_t2_mult",  0.5),
            "sig.golden_sweep_premium_t3_mult":  get_param("sig.golden_sweep_premium_t3_mult",  0.2),
            "sig.block_premium_t2_mult":         get_param("sig.block_premium_t2_mult",         0.5),
            "sig.block_premium_t3_mult":         get_param("sig.block_premium_t3_mult",         0.2),
            "sig.noteworthy_premium_t2_mult":    get_param("sig.noteworthy_premium_t2_mult",    0.5),
            "sig.noteworthy_premium_t3_mult":    get_param("sig.noteworthy_premium_t3_mult",    0.2),
            # Dimension 2
            "sig.require_ask_side":              get_param("sig.require_ask_side",              True),
            "sig.ask_side_pct_floor":            get_param("sig.ask_side_pct_floor",            0.6),
            # Dimension 3
            "sig.require_vol_gt_oi":             get_param("sig.require_vol_gt_oi",             True),
            # Dimension 4
            "sig.min_dte":                       get_param("sig.min_dte",                       5),
            "sig.max_dte":                       get_param("sig.max_dte",                       60),
            # Dimension 5
            "sig.min_trade_count":               get_param("sig.min_trade_count",               2),
            # Scoring
            "sig.steamroom_score_floor":         get_param("sig.steamroom_score_floor",         3),
        }


# ---------------------------------------------------------------------------
# Module-level singleton — shared by stream_worker and any other consumer.
# Backtest contexts should instantiate their own SignalEngine(strict_gate_1=False)
# rather than importing this singleton.
# ---------------------------------------------------------------------------
engine = SignalEngine()


# ============================================================================
# CHUNK 4-A  compute_conviction_score()
#
# Pure function — no I/O, no side effects.  Returns int [0, 5].
#
# Mirrors the 5 Steamroom gate dimensions but reads REARCH-004 episode
# attributes directly (ask_side_pct, vol_oi_signal, notional_tier,
# dte_bucket, trade_count) rather than going through the per-gate class
# methods.  This keeps the function dependency-free so it can be called
# from build_signal_row(), tests, and backtest replay without needing a
# SignalEngine instance or a cfg dict that's fully populated.
#
# Dimension mapping (parallel to SignalEngine._eval_gate_N):
#   D1 — Ask-side execution    ask_side_pct >= cfg.ask_side_pct_floor
#   D2 — Volume > Open Interest vol_oi_signal == True
#   D3 — Premium tier          notional_tier in {NOTEWORTHY, BLOCK, GOLDEN}
#   D4 — DTE in signal window  dte_bucket NOT in {0-7, 90+}
#   D5 — Repetition / cluster  trade_count >= cfg.min_trade_count
# ============================================================================

def compute_conviction_score(episode: Any, cfg: Any) -> int:
    """Compute the WSJ Steamroom conviction score (0–5) for a RepetitionEpisode.

    Each of the five Steamroom dimensions contributes one point.  The caller
    decides the minimum pass threshold (typically ``cfg.min_conviction_score``
    or the ``sig.steamroom_score_floor`` config param, default 3).

    Parameters
    ----------
    episode:
        Enriched RepetitionEpisode (REARCH-004 attributes expected).
        Missing attributes degrade gracefully to dimension-fail rather than
        raising — safe against pre-REARCH-004 episode objects in cold-start.
    cfg:
        Any object or dict-like that exposes ``ask_side_pct_floor`` and
        ``min_trade_count``.  A ``SignalConfig`` namedtuple or the plain dict
        returned by ``SignalEngine._read_config_snapshot()`` both work.
        Attribute access is tried first; dict key access is the fallback.

    Returns
    -------
    int
        Score in [0, 5].
    """
    def _get(key: str, default):
        # Support both object attributes (SignalConfig) and dict keys
        # (config snapshot dict from _read_config_snapshot).
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

    # D1 — Ask-side execution dominance
    ask_pct: float | None = getattr(episode, "ask_side_pct", None)
    floor: float = float(_get("ask_side_pct_floor", _get("sig.ask_side_pct_floor", 0.6)))
    if ask_pct is not None and ask_pct >= floor:
        score += 1

    # D2 — Volume > Open Interest
    if getattr(episode, "vol_oi_signal", False):
        score += 1

    # D3 — Premium tier qualifies (NOTEWORTHY, BLOCK, or GOLDEN)
    notional_tier: str | None = getattr(episode, "notional_tier", None)
    if notional_tier in _QUALIFYING_TIERS:
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
#
# Private pure function — no I/O, no side effects.
#
# Derives the machine-readable Steamroom recommendation enum from three
# inputs: conviction_score [0–5], direction string, and a boolean flag
# indicating whether ask-side execution was confirmed.
#
# Decision tree (evaluated top-to-bottom; first match wins):
#
#   1. conviction == 0, OR direction == NEUTRAL
#         → NO_ACTION
#         Rationale: zero-conviction or ambiguous-direction episodes carry no
#         actionable information.
#
#   2. conviction == 5 AND ask_side_confirmed
#         → FOLLOW_SWEEP
#         Rationale: perfect-score + confirmed execution = "follow the big
#         money directly" (WSJ Steamroom highest-conviction signal).
#         NOTE: conviction==5 WITHOUT ask_side_confirmed falls through to
#         the BUY_* / WATCH branches below — FOLLOW_SWEEP requires both.
#
#   3. conviction >= 3 AND ask_side_confirmed AND direction == BULLISH
#         → BUY_CALLS
#
#   4. conviction >= 3 AND ask_side_confirmed AND direction == BEARISH
#         → BUY_PUTS
#
#   5. All remaining cases (conviction 1–2, OR conviction >= 3 with
#      ask_side failed, OR direction outside BULLISH/BEARISH)
#         → WATCH
#         Rationale: flow is emerging but execution quality or score is
#         insufficient for a directional recommendation.  Ask-side is a hard
#         gate — conviction=4 without confirmed execution → WATCH, not BUY_*.
#
# The reasoning column carries the narrative; recommendation is the verdict.
# ============================================================================

def _derive_recommendation(
    conviction_score: int,
    direction: str,
    ask_side_confirmed: bool,
) -> str:
    """Derive the Steamroom recommendation enum from score + direction + ask-side.

    Parameters
    ----------
    conviction_score:
        Integer in [0, 5] from ``compute_conviction_score()``.
    direction:
        One of ``"BULLISH"`` | ``"BEARISH"`` | ``"NEUTRAL"``.  Any other
        value is treated as NEUTRAL (→ NO_ACTION).
    ask_side_confirmed:
        True when the ask-side execution gate passed (Gate 2 cleared or
        ask_side_pct >= floor).  This is a hard gate for BUY_* and
        FOLLOW_SWEEP — unconfirmed execution at any conviction level yields
        WATCH, not a buy recommendation.

    Returns
    -------
    str
        One of: ``BUY_CALLS`` | ``BUY_PUTS`` | ``FOLLOW_SWEEP`` |
        ``WATCH`` | ``NO_ACTION``.
        Always a member of ``_VALID_RECOMMENDATIONS``.
    """
    # ── Branch 1: No conviction or neutral direction → NO_ACTION ─────────────
    if conviction_score == 0 or direction not in ("BULLISH", "BEARISH"):
        return _RECOMMENDATION_NO_ACTION

    # ── Branch 2: FOLLOW_SWEEP — perfect score + confirmed execution ─────────
    # Must be checked before the BUY_* branches because conviction==5 with
    # ask-side confirmed supersedes the directional path.
    if conviction_score == 5 and ask_side_confirmed:
        return _RECOMMENDATION_FOLLOW_SWEEP

    # ── Branch 3 & 4: Directional BUY — conviction floor + confirmed ask-side ─
    # Ask-side is a hard gate: conviction >= 3 without confirmed execution
    # falls through to WATCH (QA deliberation consensus).
    if conviction_score >= _BUY_CONVICTION_FLOOR and ask_side_confirmed:
        if direction == "BULLISH":
            return _RECOMMENDATION_BUY_CALLS
        if direction == "BEARISH":
            return _RECOMMENDATION_BUY_PUTS

    # ── Branch 5: All remaining cases → WATCH ────────────────────────────────
    # Covers:
    #   - conviction 1–2 (any direction, any ask-side)
    #   - conviction >= 3 but ask_side_confirmed == False
    #   - conviction == 5 but ask_side_confirmed == False
    return _RECOMMENDATION_WATCH


# ============================================================================
# CHUNK 4-B  build_signal_row()
#
# Assembles the insert dict for signal_history.
#
# Column set is authoritative against migration 024_rearch010_schema_purge.sql
# (post-REARCH-010 schema) and migration 033_rearch006_recommendation_enum.sql
# (recommendation enum hardening).
#
# Columns written
# ───────────────
#   ticker, alert_level, direction
#   composite_score, backtest_score
#   reasoning, contract_type
#   total_premium, trade_count
#   is_accelerating, signal_ts
#   episode_steamroom_score           ← migration 024 Section 9
#   ask_side_pct                      ← migration 024 Section 9
#   vol_oi_ratio                      ← migration 024 Section 9
#   episode_id                        ← FK to flow_episodes.id
#   recommendation                    ← enum via _derive_recommendation()
#                                        DEFAULT 'NO_ACTION' (migration 033)
#
# Columns intentionally NOT written (retired by REARCH-010)
# ─────────────────────────────────────────────────────────
#   flow_score              (dropped in migration 024 Section 6)
#   influence_tier          (dropped in migration 024 Section 6)
#   volume_premium_factor   (dropped in migration 024 Section 6)
#   swarm_*                 (dropped in migration 024 Section 6)
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
    """Build the signal_history insert dict from an enriched RepetitionEpisode.

    Parameters
    ----------
    episode:
        Enriched RepetitionEpisode (REARCH-003/004 attributes populated).
    alert_level:
        One of ``WATCH`` | ``NOTEWORTHY`` | ``BLOCK`` | ``GOLDEN``.
        Raises ``ValueError`` on unrecognised values — protects the DB
        CHECK constraint added in migration 024 Section 7.
    direction:
        One of ``BULLISH`` | ``BEARISH`` | ``NEUTRAL``.
        Raises ``ValueError`` on unrecognised values — protects the DB
        CHECK constraint added in migration 024 Section 7.
    cfg:
        Live config snapshot from SignalConfigStore (or a SignalConfig object).
        Passed through to ``compute_conviction_score`` if ``conviction_score``
        is not pre-supplied.
    conviction_score:
        Pre-computed score [0-5].  If None, computed here via
        ``compute_conviction_score()``.  Pass it in when the caller already ran
        that function to avoid double computation.
    backtest_score:
        Backtest quality score in [0.0, 1.0].  Defaults to 0.0.
    reasoning:
        Human-readable rationale string for the signal.  Optional.
    is_accelerating:
        True when episode trade velocity is increasing within the observation
        window.  Defaults to False.
    signal_ts:
        Emission timestamp (timezone-aware).  Defaults to ``utcnow()`` when
        not provided.

    Returns
    -------
    dict[str, Any]
        Ready for ``supabase.table("signal_history").insert(row).execute()``.

    Raises
    ------
    ValueError
        ``alert_level`` or ``direction`` are not valid REARCH-010 vocab.
    ValueError
        ``episode`` has no ``symbol`` or ``ticker`` attribute.
    """
    # ── Vocab guard — fail loudly before touching the DB ────────────────────
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

    # ── Ticker — required ────────────────────────────────────────────────────
    ticker: str | None = getattr(episode, "symbol", None) or getattr(episode, "ticker", None)
    if not ticker:
        raise ValueError(
            "build_signal_row: episode has no symbol/ticker attribute — "
            "cannot build a signal_history row without a ticker"
        )

    # ── Conviction score ─────────────────────────────────────────────────────
    if conviction_score is None:
        conviction_score = compute_conviction_score(episode, cfg)

    # ── composite_score — conviction normalised to [0.000, 1.000] ───────────
    # Rounded to 3dp to fit NUMERIC(5,3) column precision.
    # Intentional simplification: conviction is the sole scoring dimension for
    # REARCH-006.  When REARCH-011 (backtest integration) lands, this becomes:
    #   composite_score = round(0.7 * conviction_norm + 0.3 * backtest_score, 3)
    composite_score: float = round(conviction_score / 5.0, 3)

    # ── Snapshot fields pulled from episode ─────────────────────────────────
    total_premium: float | None = getattr(episode, "total_premium", None)
    trade_count:   int   | None = getattr(episode, "trade_count", None)
    ask_side_pct:  float | None = getattr(episode, "ask_side_pct", None)
    episode_id:    str   | None = getattr(episode, "episode_id", None)

    # vol_oi_ratio: prefer explicit attribute; derive from raw vol/oi if absent.
    vol_oi_ratio: float | None = getattr(episode, "vol_oi_ratio", None)
    if vol_oi_ratio is None:
        vol = getattr(episode, "contract_volume_at_close", None)
        oi  = getattr(episode, "contract_oi_at_open", None)
        if vol is not None and oi and oi > 0:
            vol_oi_ratio = round(float(vol) / float(oi), 4)

    # contract_type: normalise to uppercase or None
    raw_contract_type: str | None = getattr(episode, "contract_type", None)
    contract_type: str | None = raw_contract_type.upper() if raw_contract_type else None

    # ── Ask-side confirmed flag (used by _derive_recommendation) ────────────
    # Mirror the Gate 2 logic: ask_side_pct must meet the floor from cfg.
    # Absent ask_side_pct degrades to unconfirmed (False), matching Gate 2.
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

    # ── Recommendation — machine-readable Steamroom verdict ─────────────────
    recommendation: str = _derive_recommendation(
        conviction_score=conviction_score,
        direction=direction,
        ask_side_confirmed=ask_side_confirmed,
    )

    # ── Emission timestamp ───────────────────────────────────────────────────
    if signal_ts is None:
        signal_ts = datetime.now(tz=timezone.utc)

    # ── Assemble row ─────────────────────────────────────────────────────────
    row: dict[str, Any] = {
        # Core identity
        "ticker":                   ticker,
        "alert_level":              alert_level,
        "direction":                direction,
        # Scores
        "composite_score":          composite_score,
        "backtest_score":           round(float(backtest_score), 3),
        # Episode fields
        "total_premium":            float(total_premium) if total_premium is not None else None,
        "trade_count":              int(trade_count) if trade_count is not None else None,
        "contract_type":            contract_type,
        # Steamroom quality snapshot (migration 024 Section 9 columns)
        "episode_steamroom_score":  conviction_score,
        "ask_side_pct":             round(float(ask_side_pct), 4) if ask_side_pct is not None else None,
        "vol_oi_ratio":             float(vol_oi_ratio) if vol_oi_ratio is not None else None,
        # Episode FK
        "episode_id":               str(episode_id) if episode_id is not None else None,
        # Metadata
        "reasoning":                reasoning,
        "is_accelerating":          is_accelerating,
        "signal_ts":                signal_ts.isoformat(),
        # Recommendation — enum via _derive_recommendation() (migration 033)
        # CHECK constraint: BUY_CALLS | BUY_PUTS | FOLLOW_SWEEP | WATCH | NO_ACTION
        "recommendation":           recommendation,
        # NOTE: created_at is omitted intentionally — owned by Postgres DEFAULT now().
        # NOTE: flow_score, influence_tier, volume_premium_factor, swarm_* all
        #       dropped in migration 024 Section 6 — do not add them back here.
    }

    return row
