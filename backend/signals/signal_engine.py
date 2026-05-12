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
#                       before the midpoint lookup.
#              Fix 4b: evaluate() now treats ALL gates as hard gates.
#                      Any single gate failure -> passed=False regardless
#                      of steamroom_score.
#              Fix 5:  Expose get_effective_premium_threshold at module level
#                      so unittest.mock.patch("signals.signal_engine.
#                      get_effective_premium_threshold") resolves correctly.
#              Fix 6:  Remap compute_conviction_score D1-D5 to match test spec.
#                      D1 = ask_side_pct dominance (None -> no point).
#                      D2 = vol_oi_signal.
#                      D3 = notional_tier in _QUALIFYING_TIERS.
#                      D4 = dte_bucket not None and not disqualifying.
#                      D5 = trade_count >= min_trade_count.
#                      The watch_floor/premium check removed from this function
#                      — SignalEngine._eval_gate_1 handles the premium hard gate
#                      separately.
#              Fix 7:  build_signal_row signal_ts: accept str passthrough.
#              Fix 9:  _derive_recommendation: canonical 3-param signature
#                      (score, direction, ask_side_confirmed). Vocab:
#                        score==5 + confirmed          -> FOLLOW_SWEEP
#                        score==5 + not confirmed      -> WATCH
#                        score>=3 + confirmed + BULLISH -> BUY_CALLS
#                        score>=3 + confirmed + BEARISH -> BUY_PUTS
#                        score>=3 + not confirmed       -> WATCH
#                        1<=score<=2                    -> WATCH
#                        score==0 or not BULLISH/BEARISH -> NO_ACTION
#                      Removed STRONG_BUY / STRONG_SELL.
#                      Added FOLLOW_SWEEP to _VALID_RECOMMENDATIONS.
#                      build_signal_row derives ask_side_confirmed from
#                      episode.ask_side_pct vs cfg floor.
#              SA-01:  build_signal_row: treat disabled D2 gate as
#                      auto-confirmed for recommendation scoring.
#                      ask_side_confirmed = (not require_ask_side)
#                                           OR (pct >= floor)
#                      Without this fix, require_ask_side=False silently
#                      downgraded all FOLLOW_SWEEP/BUY_CALLS/BUY_PUTS
#                      signals to WATCH with no log warning.
#                      Added WARNING log when kill-switch is active.
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

# Public module-level alias so patch("signals.signal_engine.get_effective_premium_threshold")
# resolves correctly in unit tests.
get_effective_premium_threshold = _global_get_effective_premium_threshold

log = logging.getLogger("signal_engine")

# ---------------------------------------------------------------------------
# Alert level key constants
# ---------------------------------------------------------------------------
_ALERT_GOLDEN     = "sig.golden_sweep_premium"
_ALERT_BLOCK      = "sig.block_premium"
_ALERT_NOTEWORTHY = "sig.noteworthy_premium"

_STORE_KEY_GOLDEN     = "golden_sweep_premium"
_STORE_KEY_BLOCK      = "block_premium"
_STORE_KEY_NOTEWORTHY = "noteworthy_premium"

_ALERT_LEVEL_KEYS: tuple[tuple[str, str, str], ...] = (
    (_ALERT_GOLDEN,     _STORE_KEY_GOLDEN,     "GOLDEN"),
    (_ALERT_BLOCK,      _STORE_KEY_BLOCK,      "BLOCK"),
    (_ALERT_NOTEWORTHY, _STORE_KEY_NOTEWORTHY, "NOTEWORTHY"),
)

# Gate name constants
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
# Chunk 4 — vocab sets
# ---------------------------------------------------------------------------

# Tiers that satisfy D3 (premium quality tier) in the conviction score
_QUALIFYING_TIERS: frozenset[str] = frozenset({"NOTEWORTHY", "BLOCK", "GOLDEN"})

# DTE buckets that FAIL D4
_DISQUALIFYING_DTE_BUCKETS: frozenset[str] = frozenset({"0-7", "90+"})

# Valid REARCH-010 vocab
_VALID_ALERT_LEVELS: frozenset[str] = frozenset({"WATCH", "NOTEWORTHY", "BLOCK", "GOLDEN"})
_VALID_DIRECTIONS:   frozenset[str] = frozenset({"BULLISH", "BEARISH", "NEUTRAL"})

# ---------------------------------------------------------------------------
# Chunk 5 — recommendation enum vocab
# ---------------------------------------------------------------------------

_RECOMMENDATION_FOLLOW_SWEEP = "FOLLOW_SWEEP"
_RECOMMENDATION_BUY_CALLS    = "BUY_CALLS"
_RECOMMENDATION_BUY_PUTS     = "BUY_PUTS"
_RECOMMENDATION_WATCH        = "WATCH"
_RECOMMENDATION_NO_ACTION    = "NO_ACTION"

_VALID_RECOMMENDATIONS: frozenset[str] = frozenset({
    _RECOMMENDATION_FOLLOW_SWEEP,
    _RECOMMENDATION_BUY_CALLS,
    _RECOMMENDATION_BUY_PUTS,
    _RECOMMENDATION_WATCH,
    _RECOMMENDATION_NO_ACTION,
})

_BUY_CONVICTION_FLOOR = 3

# DTE bucket -> representative midpoint (days)
_DTE_BUCKET_MIDPOINTS = {
    "SHORT": 4,
    "MID":   19,
    "LONG":  45,
    "XLONG": 75,
}

_KNOWN_DTE_BUCKETS: frozenset[str] = frozenset(_DTE_BUCKET_MIDPOINTS.keys())

_WATCH_FLOOR_FACTOR = 0.5

# ---------------------------------------------------------------------------
# Tier normalisation helper
# ---------------------------------------------------------------------------

def _normalise_tier(tier: str | None) -> str:
    """Normalise notional_tier to canonical "T1"/"T2"/"T3" form."""
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
# EpisodeEvalResult
# ---------------------------------------------------------------------------

@dataclass
class EpisodeEvalResult:
    """
    Output of SignalEngine.evaluate_episode().

    Attributes
    ----------
    passed              True when all enabled dimensions cleared.
    alert_level         GOLDEN / BLOCK / NOTEWORTHY / WATCH / FAIL.
    failing_dimensions  Non-empty list when passed=False.
    effective_threshold Tier-adjusted dollar threshold applied for the D1 gate.
    premium             Raw total_premium from the episode.
    ticker              Episode ticker.
    """
    passed:              bool
    alert_level:         str
    failing_dimensions:  List[str] = field(default_factory=list)
    effective_threshold: float = 0.0
    premium:             float = 0.0
    ticker:              str = ""


# ---------------------------------------------------------------------------
# GateResult
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
        Defaults to None -> uses live signal_config_store globals.

    strict_gate_1:
        Retained for API compatibility.  Has no effect — all gates are now
        hard gates.  Will be removed in a future cleanup pass.
    """

    def __init__(self, config_store=None, strict_gate_1: bool = True) -> None:
        self._config_store = config_store
        self._strict_gate_1 = strict_gate_1  # kept for API compat; unused

    def _get_effective_threshold(self, level_key: str, tier: str) -> float:
        if self._config_store is not None:
            bare_key = level_key.replace("sig.", "", 1)
            result = self._config_store.get_effective_premium_threshold(bare_key, tier)
            return float(result) if result is not None else 0.0
        return float(_global_get_effective_premium_threshold(level_key, tier) or 0.0)

    def evaluate(self, ep) -> GateResult:
        """Evaluate *ep* against all five Steamroom conviction gates.

        All gates are hard gates: any single failure sets passed=False.
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

    def evaluate_episode(self, episode: dict) -> EpisodeEvalResult:
        """Dict-based evaluation bridge — used by signal_store._bus_signal_listener."""
        ticker  = episode.get("ticker", "UNKNOWN")
        premium = float(episode.get("total_premium") or 0)

        raw_tier = episode.get("notional_tier") or "T1"
        tier = _normalise_tier(raw_tier)

        proxy = _EpisodeProxy(episode, normalised_tier=tier)
        result = self.evaluate(proxy)

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

    def _eval_gate_1(self, ep, cfg: dict) -> tuple[GateVerdict, Optional[str]]:
        """Gate 1 — Premium Threshold (tier-aware)."""
        premium: float = getattr(ep, "weighted_premium", 0.0) or 0.0
        if premium == 0.0:
            premium = float(getattr(ep, "total_premium", 0.0) or 0.0)

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
        """Gate 4 — DTE Quality."""
        min_dte: int = cfg.get("sig.min_dte", cfg.get("min_dte", 5))
        max_dte: int = cfg.get("sig.max_dte", cfg.get("max_dte", 60))

        dte: Optional[int] = getattr(ep, "dte", None)
        if dte is None:
            events = getattr(ep, "events", []) or []
            if events:
                dte = getattr(events[0], "dte", None)

        if dte is None:
            raw_bucket = getattr(ep, "dte_bucket", None)
            dte_bucket = (raw_bucket or "").strip().upper() if raw_bucket is not None else ""

            if not dte_bucket:
                return GateVerdict(False, "dte_unknown: dte_bucket is None or empty")

            if dte_bucket not in _KNOWN_DTE_BUCKETS:
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

    def _read_config_snapshot(self) -> dict[str, Any]:
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
    global _engine_singleton
    if _engine_singleton is None:
        _engine_singleton = SignalEngine()
        log.info("[signal_engine] singleton initialised (signals/ authority)")
    return _engine_singleton


engine = get_engine()


# ---------------------------------------------------------------------------
# _EpisodeProxy
# ---------------------------------------------------------------------------

class _EpisodeProxy:
    """Thin wrapper that exposes episode dict keys as attributes."""
    __slots__ = ("_d", "_tier")

    def __init__(self, d: dict, normalised_tier: str = "T1") -> None:
        object.__setattr__(self, "_d", d)
        object.__setattr__(self, "_tier", normalised_tier)

    def __getattr__(self, name: str):
        d = object.__getattribute__(self, "_d")
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
# _gate_names_to_dimensions
# ---------------------------------------------------------------------------

_GATE_TO_DIMENSION: dict[str, str] = {
    _GATE_PREMIUM:    "D1_PREMIUM",
    _GATE_ASK_SIDE:   "D2_ASK_SIDE",
    _GATE_VOL_OI:     "D3_VOL_GT_OI",
    _GATE_DTE:        "D4_DTE",
    _GATE_REPETITION: "D5_REPETITION",
}


def _gate_names_to_dimensions(gates: dict[str, GateVerdict]) -> list[str]:
    return [
        _GATE_TO_DIMENSION[gate_name]
        for gate_name, verdict in gates.items()
        if not verdict.passed and gate_name in _GATE_TO_DIMENSION
    ]


# ============================================================================
# CHUNK 4-A  compute_conviction_score()
# ============================================================================

def compute_conviction_score(episode: Any, cfg: Any) -> int:
    """Compute the WSJ Steamroom conviction score (0-5) for a RepetitionEpisode.

    Pure additive score (0-5) used for recommendation derivation and
    composite_score in signal_history.  NOT the hard gate — premium threshold
    enforcement lives in SignalEngine._eval_gate_1.

    Dimension mapping
    -----------------
    D1 — Ask-side execution dominance  (ask_side_pct >= floor; None -> 0)
    D2 — Volume > Open Interest        (vol_oi_signal is True)
    D3 — Qualifying notional tier      (notional_tier in _QUALIFYING_TIERS)
    D4 — DTE in signal window          (dte_bucket not None and not disqualifying)
    D5 — Repetition / clustering       (trade_count >= min_trade_count)
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

    # D1 — Ask-side execution dominance
    # None means the field is absent — no point awarded (graceful degrade).
    ask_pct: float | None = getattr(episode, "ask_side_pct", None)
    floor: float = float(_get("ask_side_pct_floor", _get("sig.ask_side_pct_floor", 0.6)))
    if ask_pct is not None and ask_pct >= floor:
        score += 1

    # D2 — Volume > Open Interest
    if getattr(episode, "vol_oi_signal", False):
        score += 1

    # D3 — Qualifying notional tier
    notional_tier: str | None = getattr(episode, "notional_tier", None)
    if notional_tier in _QUALIFYING_TIERS:
        score += 1

    # D4 — DTE in signal window (not 0-7 days, not 90+ days, not None)
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
    ask_side_confirmed: bool = False,
) -> str:
    """Derive the Steamroom recommendation from score, direction, and ask confirmation.

    Score / direction / confirmed -> recommendation mapping
    -------------------------------------------------------
    score==0 or direction not BULLISH/BEARISH -> NO_ACTION
    score==5 + confirmed                      -> FOLLOW_SWEEP  (both directions)
    score==5 + not confirmed                  -> WATCH
    score>=3 + confirmed + BULLISH            -> BUY_CALLS
    score>=3 + confirmed + BEARISH            -> BUY_PUTS
    score>=3 + not confirmed                  -> WATCH
    1 <= score <= 2                           -> WATCH
    """
    if conviction_score == 0 or direction not in ("BULLISH", "BEARISH"):
        return _RECOMMENDATION_NO_ACTION

    if conviction_score == 5:
        if ask_side_confirmed:
            return _RECOMMENDATION_FOLLOW_SWEEP
        return _RECOMMENDATION_WATCH

    if conviction_score >= _BUY_CONVICTION_FLOOR:
        if ask_side_confirmed:
            if direction == "BULLISH":
                return _RECOMMENDATION_BUY_CALLS
            return _RECOMMENDATION_BUY_PUTS
        return _RECOMMENDATION_WATCH

    # score 1 or 2
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
    signal_ts: datetime | str | None = None,
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

    # ---------------------------------------------------------------------------
    # SA-01 fix: derive ask_side_confirmed honoring the require_ask_side kill-switch.
    #
    # When require_ask_side=False the gate auto-passes for hard-gate scoring, so
    # recommendation derivation must also treat D2 as confirmed — otherwise a
    # score=5 episode with a low ask_side_pct would silently resolve to WATCH
    # instead of FOLLOW_SWEEP/BUY_CALLS/BUY_PUTS with no log warning.
    # ---------------------------------------------------------------------------
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

    require_ask_side: bool = bool(
        _get_cfg("require_ask_side", _get_cfg("sig.require_ask_side", True))
    )
    ask_floor: float = float(
        _get_cfg("ask_side_pct_floor", _get_cfg("sig.ask_side_pct_floor", 0.6))
    )

    if not require_ask_side:
        # Gate is disabled — treat as auto-confirmed so recommendation is not
        # silently downgraded.  Emit a WARNING so operators know the kill-switch
        # is active; this is intentional noise to prevent silent surprises.
        log.warning(
            "[signal_engine] SA-01: require_ask_side=False (kill-switch active) — "
            "D2 treated as confirmed for recommendation on ticker=%s. "
            "ask_side_pct=%.2f (floor=%.2f ignored).",
            ticker,
            ask_side_pct if ask_side_pct is not None else 0.0,
            ask_floor,
        )
        ask_side_confirmed: bool = True
    else:
        ask_side_confirmed = (
            ask_side_pct is not None and ask_side_pct >= ask_floor
        )

    recommendation: str = _derive_recommendation(
        conviction_score=conviction_score,
        direction=direction,
        ask_side_confirmed=ask_side_confirmed,
    )

    # signal_ts: accept datetime (generate isoformat) or str (write verbatim).
    if signal_ts is None:
        ts_str: str = datetime.now(tz=timezone.utc).isoformat()
    elif isinstance(signal_ts, str):
        ts_str = signal_ts
    else:
        ts_str = signal_ts.isoformat()

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
        "signal_ts":                ts_str,
        "recommendation":           recommendation,
    }
