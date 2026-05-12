"""
signal_engine.py — REARCH-006: Steamroom 5-Dimension Signal Engine

Evaluates a completed flow_episodes row against the 5 Steamroom conviction
dimensions to determine whether the episode qualifies for a signal_history
write, and at what alert level.

Architecture
------------
The signal engine is a pure function over enriched episode data.  It reads
all thresholds from SignalConfigStore (REARCH-005) so every gate is live-
editable without a redeploy.  It never touches the DB directly — callers
(signal_store._bus_signal_listener) own the write path.

5 Dimensions (evaluated in order — first failure is the reported gate)
----------------------------------------------------------------------
D1  Premium Threshold   episode["notional_tier"] + tier-scaled dollar gate
D2  Ask-Side Execution  episode["ask_side_pct"] >= config ask_side_pct_floor
D3  Vol > OI            episode vol_oi_signal flag (from flow_events aggregate)
D4  DTE Quality         episode["dte_bucket"] maps to numeric range
D5  Repetition          episode["trade_count"] >= config min_trade_count

Alert Level Resolution
-----------------------
GOLDEN   — all 5 dimensions pass AND premium >= golden threshold for tier
BLOCK    — all 5 pass AND premium >= block threshold for tier
NOTEWORTHY — all 5 pass AND premium >= noteworthy threshold for tier
WATCH    — all 5 pass (minimum qualifying premium cleared)
FAIL     — one or more dimensions failed (no signal written)

Public API
----------
  evaluate_episode(episode: dict) -> EpisodeEvalResult
  get_engine() -> SignalEngine          # module-level singleton
  SignalEngine(config_store)            # injectable for tests

Notes
-----
- DTE bucket strings ("SHORT", "MID", "LONG", "XLONG") are defined in
  _compute_dte_bucket() in ingestion/processor.py (REARCH-003).
  The engine maps these back to representative DTE midpoints for the
  min_dte / max_dte gate because storing the raw bucket is sufficient for
  the gate — we don't need exact DTE at signal time.
- vol_oi_signal on the EPISODE is the majority-vote aggregate of constituent
  flow_events.vol_oi_signal booleans, computed by the episode accumulator.
  A NULL value (cache-miss sentinel from REARCH-003) is treated as False
  when require_vol_gt_oi=True.
- notional_tier on episodes is locked at episode open (seed-event-only
  semantics, SA-3 from REARCH-004).  It is one of: "T1", "T2", "T3".
- All gate decisions are logged at DEBUG level so the audit trail is
  available in Railway logs without noise at INFO.
"""
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from services.signal_config_store import SignalConfigStore, get_signal_config_store

log = logging.getLogger("signal_engine")

# ---------------------------------------------------------------------------
# DTE bucket → representative midpoint (days)
# Bucket definitions mirror _compute_dte_bucket() in ingestion/processor.py:
#   SHORT : 1–7   → midpoint 4
#   MID   : 8–30  → midpoint 19
#   LONG  : 31–60 → midpoint 45
#   XLONG : 61–90 → midpoint 75
# ---------------------------------------------------------------------------
_DTE_BUCKET_MIDPOINTS = {
    "SHORT": 4,
    "MID":   19,
    "LONG":  45,
    "XLONG": 75,
}

# Alert level hierarchy — order matters for threshold comparison
_ALERT_LEVELS_ORDERED = ["GOLDEN", "BLOCK", "NOTEWORTHY", "WATCH"]

# Config key names (must match keys seeded in migration 030)
_CFG_REQUIRE_ASK_SIDE      = "require_ask_side"
_CFG_ASK_SIDE_PCT_FLOOR    = "ask_side_pct_floor"
_CFG_REQUIRE_VOL_GT_OI     = "require_vol_gt_oi"
_CFG_MIN_DTE               = "min_dte"
_CFG_MAX_DTE               = "max_dte"
_CFG_MIN_TRADE_COUNT       = "min_trade_count"
_CFG_GOLDEN_PREMIUM        = "golden_sweep_premium"
_CFG_BLOCK_PREMIUM         = "block_premium"
_CFG_NOTEWORTHY_PREMIUM    = "noteworthy_premium"


# ---------------------------------------------------------------------------
# Result type
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
    failing_dimensions  Non-empty list of dimension names when passed=False.
                        Empty list when passed=True.
    effective_threshold The tier-adjusted dollar threshold that was applied for
                        the Dimension-1 gate (useful for logging / backtest).
    premium             Raw total_premium from the episode (for caller logging).
    ticker              Episode ticker (for caller logging).
    """
    passed:               bool
    alert_level:          str
    failing_dimensions:   List[str] = field(default_factory=list)
    effective_threshold:  float = 0.0
    premium:              float = 0.0
    ticker:               str = ""


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class SignalEngine:
    """
    Stateless evaluator over enriched episode dicts.

    The config_store is injected so tests can pass a pre-seeded stub without
    touching the DB or hitting the 30s TTL cache.

    Production code uses the module-level singleton via get_engine().
    """

    def __init__(self, config_store: SignalConfigStore) -> None:
        self._cfg = config_store

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def evaluate_episode(self, episode: dict) -> EpisodeEvalResult:
        """
        Evaluate *episode* against all 5 Steamroom conviction dimensions.

        Parameters
        ----------
        episode : dict
            A flow_episodes row (or equivalent dict) containing at minimum:
              ticker, total_premium, notional_tier, ask_side_pct,
              ask_side_count, vol_oi_signal, dte_bucket, trade_count.

        Returns
        -------
        EpisodeEvalResult
        """
        ticker    = episode.get("ticker", "UNKNOWN")
        premium   = float(episode.get("total_premium") or 0)
        tier      = episode.get("notional_tier") or "T1"
        failing:  List[str] = []

        cfg = self._cfg.get_all()

        # --- D1: Premium threshold ----------------------------------------
        # Resolve the effective threshold for the NOTEWORTHY level (minimum
        # qualifying bar) using the tier multiplier.  The alert level itself
        # is resolved after all gates pass.
        noteworthy_threshold = self._cfg.get_effective_premium_threshold(
            _CFG_NOTEWORTHY_PREMIUM, tier
        )
        if noteworthy_threshold is None:
            noteworthy_threshold = 50_000.0  # hard-coded fallback if config missing

        if premium < noteworthy_threshold:
            failing.append("D1_PREMIUM")
            log.debug(
                "[signal_engine] %s FAIL D1_PREMIUM premium=%.0f < threshold=%.0f (tier=%s)",
                ticker, premium, noteworthy_threshold, tier,
            )

        # --- D2: Ask-side execution ----------------------------------------
        require_ask = _coerce_bool(cfg.get(_CFG_REQUIRE_ASK_SIDE, True))
        if require_ask:
            ask_side_pct       = float(episode.get("ask_side_pct") or 0)
            ask_side_pct_floor = float(cfg.get(_CFG_ASK_SIDE_PCT_FLOOR, 0.6))
            if ask_side_pct < ask_side_pct_floor:
                failing.append("D2_ASK_SIDE")
                log.debug(
                    "[signal_engine] %s FAIL D2_ASK_SIDE ask_side_pct=%.4f < floor=%.4f",
                    ticker, ask_side_pct, ask_side_pct_floor,
                )

        # --- D3: Vol > OI -------------------------------------------------
        require_vol_gt_oi = _coerce_bool(cfg.get(_CFG_REQUIRE_VOL_GT_OI, True))
        if require_vol_gt_oi:
            vol_oi_signal = episode.get("vol_oi_signal")  # bool | None
            if not vol_oi_signal:  # False or None both fail
                failing.append("D3_VOL_GT_OI")
                log.debug(
                    "[signal_engine] %s FAIL D3_VOL_GT_OI vol_oi_signal=%r",
                    ticker, vol_oi_signal,
                )

        # --- D4: DTE quality ----------------------------------------------
        dte_bucket = (episode.get("dte_bucket") or "").upper()
        dte_mid    = _DTE_BUCKET_MIDPOINTS.get(dte_bucket)
        min_dte    = int(cfg.get(_CFG_MIN_DTE, 5))
        max_dte    = int(cfg.get(_CFG_MAX_DTE, 60))

        if dte_mid is None:
            # Unknown or NULL bucket — treat as DTE outside acceptable range
            failing.append("D4_DTE")
            log.debug(
                "[signal_engine] %s FAIL D4_DTE dte_bucket=%r not recognised",
                ticker, dte_bucket,
            )
        elif not (min_dte <= dte_mid <= max_dte):
            failing.append("D4_DTE")
            log.debug(
                "[signal_engine] %s FAIL D4_DTE dte_mid=%d not in [%d, %d]",
                ticker, dte_mid, min_dte, max_dte,
            )

        # --- D5: Repetition / clustering -----------------------------------
        trade_count     = int(episode.get("trade_count") or 0)
        min_trade_count = int(cfg.get(_CFG_MIN_TRADE_COUNT, 2))
        if trade_count < min_trade_count:
            failing.append("D5_REPETITION")
            log.debug(
                "[signal_engine] %s FAIL D5_REPETITION trade_count=%d < min=%d",
                ticker, trade_count, min_trade_count,
            )

        # --- Resolution ---------------------------------------------------
        if failing:
            return EpisodeEvalResult(
                passed=False,
                alert_level="FAIL",
                failing_dimensions=failing,
                effective_threshold=noteworthy_threshold,
                premium=premium,
                ticker=ticker,
            )

        # All gates passed — resolve alert level from premium vs tier thresholds
        alert_level = self._resolve_alert_level(premium, tier, cfg)

        log.debug(
            "[signal_engine] %s PASS alert=%s premium=%.0f tier=%s trades=%d",
            ticker, alert_level, premium, tier, trade_count,
        )

        return EpisodeEvalResult(
            passed=True,
            alert_level=alert_level,
            failing_dimensions=[],
            effective_threshold=noteworthy_threshold,
            premium=premium,
            ticker=ticker,
        )

    # ------------------------------------------------------------------
    # Alert level resolution
    # ------------------------------------------------------------------

    def _resolve_alert_level(self, premium: float, tier: str, cfg: dict) -> str:
        """
        Walk GOLDEN → BLOCK → NOTEWORTHY → WATCH and return the highest level
        whose tier-adjusted threshold the premium clears.

        Thresholds are fetched from signal_config via get_effective_premium_threshold()
        which applies the T2/T3 multipliers from migration 031.
        """
        for level_key, cfg_key in (
            ("GOLDEN",      _CFG_GOLDEN_PREMIUM),
            ("BLOCK",       _CFG_BLOCK_PREMIUM),
            ("NOTEWORTHY",  _CFG_NOTEWORTHY_PREMIUM),
        ):
            threshold = self._cfg.get_effective_premium_threshold(cfg_key, tier)
            if threshold is None:
                continue
            if premium >= threshold:
                return level_key

        # Premium cleared the NOTEWORTHY floor (D1 gate passed) but is below
        # NOTEWORTHY threshold — this means WATCH is the floor label.
        # This branch is reached when the D1 gate uses a custom low threshold.
        return "WATCH"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_bool(value) -> bool:
    """
    Coerce a config value (str | bool | int | None) to bool.

    signal_config stores values as TEXT in the DB, so "true"/"false" strings
    must be handled alongside native Python bools and integers.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_engine: Optional[SignalEngine] = None


def get_engine() -> SignalEngine:
    """
    Return the module-level SignalEngine singleton.

    The engine is created on first call using the production SignalConfigStore
    singleton (get_signal_config_store()).  Subsequent calls return the cached
    instance.

    For tests, instantiate SignalEngine(config_store) directly and do not call
    this function — the singleton will not be initialised in the test process
    and tests should not rely on it.
    """
    global _engine
    if _engine is None:
        _engine = SignalEngine(get_signal_config_store())
        log.info("[signal_engine] singleton initialised")
    return _engine
