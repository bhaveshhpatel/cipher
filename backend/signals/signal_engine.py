# ============================================================================
# signals/signal_engine.py
#
# REARCH-006 — Chunk 2: SignalEngine skeleton + Gate 1–5 evaluators.
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
