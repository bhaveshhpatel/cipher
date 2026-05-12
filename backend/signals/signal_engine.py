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
#              Fix 4/4: Accept optional config_store injection so tests can
#                       pass StubConfigStore without hitting the live DB.
#                       Gate 1 hard floor corrected to noteworthy_threshold
#                       (not watch_floor) — WATCH is an alert label only.
#                       Tier normalisation: episode dicts may carry "tier1"/
#                       "tier2"/"tier3" (DB form); StubConfigStore and
#                       get_effective_premium_threshold expect "T1"/"T2"/"T3".
#                       evaluate_episode() now normalises before forwarding.
#                       _EpisodeProxy.notional_tier also normalises so the
#                       object-based evaluate() path receives canonical keys.
#                       _eval_gate_4 extended to reject None / empty-string /
#                       "EXPIRED" / "UNKNOWN" dte_bucket values explicitly
#                       before the midpoint lookup (previously only None
#                       from the midpoint map triggered a fail; an empty
#                       string or "EXPIRED" would raise KeyError → None →
#                       fail, but only accidentally).
#              Fix 4b: evaluate() now treats ALL gates as hard gates.
#                      Any single gate failure → passed=False regardless
#                      of steamroom_score.  The score_floor / steamroom_score
#                      path was causing D2–D5 failures to be silently absorbed
#                      when 3+ other gates passed (score=4 >= floor=3).
#                      evaluate_episode() inherits the fix transparently.
#
# Implements the WSJ Steamroom 5-dimension conviction gate over enriched
# RepetitionEpisode objects.  Called once per episode close from the stream
# worker (REARCH-006 Chunk 3 wires the call site).
#
# Gate layout (matches STEAMROOM_REARCH_ROADMAP.md § REARCH-006)
# ... (rest of header unchanged)
# ============================================================================

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, List, Optional

from signals.signal_gate import GateVerdict
from services.signal_config_store import (
    get_param,
    get_effective_premium_threshold as _global_get_effective_premium_threshold,
)

log = logging.getLogger("signal_engine")

# ---------------------------------------------------------------------------
# Alert level key constants — match keys in signal_config_store.SIGNAL_CONFIG_TYPES
# ---------------------------------------------------------------------------
_ALERT_GOLDEN     = "sig.golden_sweep_premium"
_ALERT_BLOCK      = "sig.block_premium"
_ALERT_NOTEWORTHY = "sig.noteworthy_premium"

# Keys as stored in StubConfigStore / store.get_all() (no "sig." prefix)
_STORE_KEY_GOLDEN     = "golden_sweep_premium"
_STORE_KEY_BLOCK      = "block_premium"
_STORE_KEY_NOTEWORTHY = "noteworthy_premium"

# Evaluation order: highest conviction first so the returned alert_level
# reflects the best label the episode qualifies for.
_ALERT_LEVEL_KEYS: tuple[tuple[str, str, str], ...] = (
    (_ALERT_GOLDEN,     _STORE_KEY_GOLDEN,     "GOLDEN"),
    (_ALERT_BLOCK,      _STORE_KEY_BLOCK,      "BLOCK"),
    (_ALERT_NOTEWORTHY, _STORE_KEY_NOTEWORTHY, "NOTEWORTHY"),
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

_BUY_CONVICTION_FLOOR = 3

# DTE bucket → representative midpoint (days) — mirrors ingestion/processor.py
_DTE_BUCKET_MIDPOINTS = {
    "SHORT": 4,
    "MID":   19,
    "LONG":  45,
    "XLONG": 75,
}

# Recognised DTE bucket names (case-normalised uppercase).
# Any bucket not in this set is treated as unknown and fails D4.
_KNOWN_DTE_BUCKETS: frozenset[str] = frozenset(_DTE_BUCKET_MIDPOINTS.keys())

_WATCH_FLOOR_FACTOR = 0.5

# ---------------------------------------------------------------------------
# Tier normalisation helper
# ---------------------------------------------------------------------------

def _normalise_tier(tier: str | None) -> str:
    """Normalise notional_tier to canonical "T1"/"T2"/"T3" form.

    DB rows and episode dicts may carry "tier1"/"tier2"/"tier3" (lowercase
    with word "tier").  StubConfigStore and get_effective_premium_threshold
    expect the short uppercase form "T1"/"T2"/"T3".  If the value is already
    in canonical form or is unrecognised, it is returned unchanged so the
    threshold lookup falls back to the base multiplier (1.0) rather than
    crashing.
    """
    if not tier:
        return "T1"
    _MAP = {
        "tier1": "T1",
        "tier2": "T2",
        "tier3": "T3",
        "t1": "T1",
        "t2": "T2",
        "t3": "T3",
    }
    return _MAP.get(tier.lower(), tier)


# ---------------------------------------------------------------------------
# EpisodeEvalResult — dict-caller API result type (used by signal_store)
# ---------------------------------------------------------------------------

@dataclass
class EpisodeEvalResult:
    """
    Output of SignalEngine.evaluate_episode().

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
    """Full evaluation result for a single RepetitionEpisode."""
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

    Parameters
    ----------
    config_store:
        Optional config store instance.  Must implement:
          - get_all() -> dict  (returns unprefixed keys)
          - get_effective_premium_threshold(alert_level_key: str, tier: str) -> float
        When provided, all config reads use the store instead of the global
        signal_config_store singletons.  Pass StubConfigStore in tests.
        Defaults to None → uses live signal_config_store globals.

    strict_gate_1:
        Retained for API compatibility.  Has no effect — all gates are now
        hard gates (any failure → passed=False).  Will be removed in a
        future cleanup pass.
    """

    def __init__(self, config_store=None, strict_gate_1: bool = True) -> None:
        self._config_store = config_store
        self._strict_gate_1 = strict_gate_1  # kept for API compat; unused

    # ------------------------------------------------------------------
    # Internal helpers for config_store vs. global dispatch
    # ------------------------------------------------------------------

    def _get_effective_threshold(self, level_key: str, tier: str) -> float:
        """Return tier-adjusted threshold, using injected store when available.

        tier is expected in canonical "T1"/"T2"/"T3" form (call _normalise_tier
        before this method if the raw value comes from an episode dict).
        """
        if self._config_store is not None:
            bare_key = level_key.replace("sig.", "", 1)
            result = self._config_store.get_effective_premium_threshold(bare_key, tier)
            return float(result) if result is not None else 0.0
        return float(_global_get_effective_premium_threshold(level_key, tier) or 0.0)

    # ------------------------------------------------------------------
    # Public API — object-based
    # ------------------------------------------------------------------

    def evaluate(self, ep) -> GateResult:
        """Evaluate *ep* against all five Steamroom conviction gates.

        All gates are hard gates: any single failure sets passed=False.
        The steamroom_score field still counts how many gates passed (useful
        for logging / debugging) but is NOT used for the pass/fail decision.
        """
        cfg = self._read_config_snapshot()
        gates: dict[str, GateVerdict] = {}

        g1, alert_level = self._eval_gate_1(ep, cfg)
        gates[_GATE_PREMIUM]    = g1
        gates[_GATE_ASK_SIDE]   = self._eval_gate_2(ep, cfg)
        gates[_GATE_VOL_OI]     = self._eval_gate_3(ep, cfg)
        gates[_GATE_DTE]        = self._eval_gate_4(ep, cfg)
        gates[_GATE_REPETITION] = self._eval_gate_5(ep, cfg)

        steamroom_score = sum(1 for g in gates.values() if g.passed)

        # All gates are hard: episode passes only when every gate passes.
        passed = all(g.passed for g in gates.values())

        if not passed:
            log.debug(
                "[signal_engine] Episode rejected: score=%d/%d  ticker=%s  failing=%s",
                steamroom_score, len(_ALL_GATE_NAMES),
                getattr(ep, "ticker", "?"),
                [name for name, g in gates.items() if not g.passed],
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
        """Dict-based evaluation bridge — used by signal_store._bus_signal_listener."""
        ticker  = episode.get("ticker", "UNKNOWN")
        premium = float(episode.get("total_premium") or 0)

        # Normalise tier from DB "tier1/tier2/tier3" → canonical "T1/T2/T3"
        raw_tier = episode.get("notional_tier") or "T1"
        tier = _normalise_tier(raw_tier)

        # Wrap dict in a proxy so evaluate()'s getattr() calls work.
        # Inject the normalised tier so _eval_gate_1 receives the correct form.
        proxy = _EpisodeProxy(episode, normalised_tier=tier)
        result = self.evaluate(proxy)

        # Compute effective noteworthy threshold for EpisodeEvalResult.
        effective_threshold = self._get_effective_threshold(_ALERT_NOTEWORTHY, tier)
        if not effective_threshold:
            effective_threshold = 50_000.0

        if not result.passed:
            failing = _gate_names_to_dimensions(result.gates)
            return EpisodeEvalResult(
                passed=False,
                alert_level="FAIL",
                failing_dimensions=failing,
                effective_threshold=effective_threshold,
                premium=premium,
                ticker=ticker,
            )

        alert_level = result.alert_level or "WATCH"
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

    def _eval_gate_1(self, ep, cfg: dict) -> tuple[GateVerdict, Optional[str]]:
        """Gate 1 — Premium Threshold (tier-aware).

        Hard floor = noteworthy_threshold (tier-adjusted).
        """
        premium: float = getattr(ep, "weighted_premium", 0.0) or 0.0
        if premium == 0.0:
            premium = float(getattr(ep, "total_premium", 0.0) or 0.0)

        # notional_tier arriving here is already normalised to T1/T2/T3
        # (either via _EpisodeProxy normalised_tier injection or by a
        # RepetitionEpisode that already carries the canonical form).
        notional_tier: str = _normalise_tier(getattr(ep, "notional_tier", "T1") or "T1")

        for level_key, _store_key, level_name in _ALERT_LEVEL_KEYS:
            threshold = self._get_effective_threshold(level_key, notional_tier)
            if threshold > 0 and premium >= threshold:
                return (
                    GateVerdict(True, f"premium_cleared_{level_name.lower()}"),
                    level_name,
                )

        noteworthy_threshold = self._get_effective_threshold(_ALERT_NOTEWORTHY, notional_tier)

        if noteworthy_threshold == 0:
            return (
                GateVerdict(True, f"premium_cleared_watch (noteworthy=0): {premium:,.0f}"),
                "WATCH",
            )

        return (
            GateVerdict(
                False,
                f"premium_below_noteworthy: {premium:,.0f} < {noteworthy_threshold:,.0f}"
                f" (tier={notional_tier})",
            ),
            None,
        )

    @staticmethod
    def _eval_gate_2(ep, cfg: dict) -> GateVerdict:
        """Gate 2 — Ask-Side Execution."""
        require: bool = cfg.get("sig.require_ask_side", cfg.get("require_ask_side", True))
        if not require:
            return GateVerdict(True, "ask_side_not_required")

        floor: float = cfg.get("sig.ask_side_pct_floor", cfg.get("ask_side_pct_floor", 0.6))
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
        """Gate 3 — Vol > OI."""
        require: bool = cfg.get("sig.require_vol_gt_oi", cfg.get("require_vol_gt_oi", True))
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

        Reads ep.dte first; falls back to dte_bucket midpoint mapping if the
        episode-level field is absent.  An unrecognised, empty, None, or
        disqualifying bucket name always fails the gate.
        """
        min_dte: int = cfg.get("sig.min_dte", cfg.get("min_dte", 5))
        max_dte: int = cfg.get("sig.max_dte", cfg.get("max_dte", 60))

        dte: Optional[int] = getattr(ep, "dte", None)
        if dte is None:
            events = getattr(ep, "events", []) or []
            if events:
                dte = getattr(events[0], "dte", None)

        if dte is None:
            # Fallback: dte_bucket midpoint — but only for recognised buckets.
            raw_bucket = getattr(ep, "dte_bucket", None)
            # Normalise: strip whitespace, uppercase
            dte_bucket = (raw_bucket or "").strip().upper() if raw_bucket is not None else ""

            if not dte_bucket:
                # None or empty string — unknown bucket
                return GateVerdict(False, "dte_unknown: dte_bucket is None or empty")

            if dte_bucket not in _KNOWN_DTE_BUCKETS:
                # Includes "UNKNOWN", "EXPIRED", and any future unrecognised values
                return GateVerdict(False, f"dte_unknown: unrecognised bucket={raw_bucket!r}")

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
        min_trades: int = cfg.get("sig.min_trade_count", cfg.get("min_trade_count", 2))
        trade_count: int = getattr(ep, "trade_count", 0) or 0

        if trade_count >= min_trades:
            return GateVerdict(True, f"trade_count={trade_count}>={min_trades}")

        return GateVerdict(False, f"trade_count={trade_count} < min_trade_count={min_trades}")

    # ------------------------------------------------------------------
    # Config snapshot builder
    # ------------------------------------------------------------------

    def _read_config_snapshot(self) -> dict[str, Any]:
        """Pull all signal-engine config keys from the live snapshot."""
        if self._config_store is not None:
            raw = self._config_store.get_all()
            snapshot: dict[str, Any] = {}
            for bare_key, value in raw.items():
                snapshot[bare_key] = value
                snapshot[f"sig.{bare_key}"] = value
            if "sig.steamroom_score_floor" not in snapshot:
                snapshot["sig.steamroom_score_floor"] = 3
                snapshot["steamroom_score_floor"] = 3
            return snapshot

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
    """Return the module-level SignalEngine singleton."""
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = SignalEngine()
        log.info("[signal_engine] singleton initialised (signals/ authority)")
    return _engine_singleton


engine = get_engine()


# ---------------------------------------------------------------------------
# _EpisodeProxy — adapts a dict to the attribute-access contract of evaluate()
# ---------------------------------------------------------------------------

class _EpisodeProxy:
    """Thin wrapper that exposes episode dict keys as attributes.

    Parameters
    ----------
    d : dict
        Raw episode dict.
    normalised_tier : str
        Pre-normalised notional_tier ("T1"/"T2"/"T3").  Injected by
        evaluate_episode() so that _eval_gate_1 always receives the
        canonical tier string regardless of how the dict was populated.
    """
    __slots__ = ("_d", "_tier")

    def __init__(self, d: dict, normalised_tier: str = "T1") -> None:
        object.__setattr__(self, "_d", d)
        object.__setattr__(self, "_tier", normalised_tier)

    def __getattr__(self, name: str):
        d = object.__getattribute__(self, "_d")
        # Return the normalised tier for Gate 1 without touching the raw dict.
        if name == "notional_tier":
            return object.__getattribute__(self, "_tier")
        if name in d:
            return d[name]
        if name == "weighted_premium":
            return float(d.get("total_premium") or 0.0)
        if name == "events":
            return []
        if name == "dte":
            raise AttributeError(name)
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
    """Compute the WSJ Steamroom conviction score (0–5) for a RepetitionEpisode."""
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

    # D1 — Premium meets watch-band floor
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
