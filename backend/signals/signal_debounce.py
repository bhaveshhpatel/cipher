# ============================================================================
# signals/signal_debounce.py
#
# REARCH-006 — Chunk 3: SIG-DEBOUNCE module.
#
# Provides SignalDebounce: an in-memory per-episode emit guard that prevents
# the same (symbol, contract_key, alert_level) from flooding signal_history
# within a configurable window.
#
# Design:
#   - State: dict[(symbol, contract_key, alert_level), datetime] — last emit time.
#   - should_emit() reads sig.debounce_window_seconds and sig.debounce_enabled
#     from signal_config_store on every call.  No cached config — any hot-reload
#     through the admin UI takes effect on the very next episode close.
#   - record_emit() is called by the caller immediately after a successful DB
#     write so the guard state is only committed on confirmed persistence.
#   - debounce_enabled=False bypasses the window entirely (returns True always).
#   - Thread-safe for single-threaded asyncio; _state mutations are not atomic
#     across concurrent callers — if multi-threaded use is ever introduced a
#     threading.Lock must be added.
#
# Key type:
#   (symbol: str, contract_key: str, alert_level: str)
#   contract_key is typically the OCC symbol (e.g. "AAPL250117C00150000").
#   Passing alert_level as a key dimension means an episode that upgrades from
#   NOTEWORTHY to BLOCK is allowed to emit even if NOTEWORTHY was recently
#   emitted — deliberate: a rising alert level is always meaningful.
# ============================================================================

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from services.signal_config_store import get_param

log = logging.getLogger("signal_debounce")

# ---------------------------------------------------------------------------
# Internal type alias for the debounce state dict key
# ---------------------------------------------------------------------------
_DebounceKey = tuple[str, str, str]  # (symbol, contract_key, alert_level)

# ---------------------------------------------------------------------------
# Cold-start fallback constants (used only when config store hasn't loaded yet)
# ---------------------------------------------------------------------------
_DEFAULT_DEBOUNCE_WINDOW_S: float = 300.0   # 5 minutes
_DEFAULT_DEBOUNCE_ENABLED: bool   = True


class SignalDebounce:
    """In-memory per-episode emit guard for signal_history flood prevention.

    Usage
    -----
    Instantiate once at module level (see ``debounce`` singleton below).
    The stream worker calls ``should_emit()`` before writing to signal_history
    and ``record_emit()`` immediately after a confirmed write.

    Parameters
    ----------
    None — all tuning is read live from signal_config_store.

    Thread safety
    -------------
    Safe for single-threaded asyncio event loops.  Not safe for concurrent
    multi-threaded access without an external lock.
    """

    def __init__(self) -> None:
        # Maps (symbol, contract_key, alert_level) -> UTC datetime of last emit.
        self._state: dict[_DebounceKey, datetime] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_emit(
        self,
        symbol: str,
        contract_key: str,
        alert_level: str,
    ) -> bool:
        """Return True if this combination is clear to emit.

        Reads ``sig.debounce_enabled`` and ``sig.debounce_window_seconds``
        from the live config store on every call — hot-reloadable.

        Parameters
        ----------
        symbol:
            Underlying ticker (e.g. ``"AAPL"``).
        contract_key:
            OCC symbol or equivalent unique contract identifier.
        alert_level:
            One of ``"GOLDEN"``, ``"BLOCK"``, ``"NOTEWORTHY"``.  Treated as
            a key dimension so upgrades always clear the debounce gate.

        Returns
        -------
        bool
            True  → emit allowed (first call, or window expired, or disabled).
            False → suppressed (within debounce window).
        """
        enabled = self._read_enabled()
        if not enabled:
            log.debug(
                "[debounce] bypass — sig.debounce_enabled=False  symbol=%s contract=%s",
                symbol, contract_key,
            )
            return True

        key: _DebounceKey = (symbol, contract_key, alert_level)
        last_emit: Optional[datetime] = self._state.get(key)

        if last_emit is None:
            log.debug(
                "[debounce] first emit — symbol=%s contract=%s alert=%s",
                symbol, contract_key, alert_level,
            )
            return True

        window_s: float = self._read_window_s()
        now_utc = datetime.now(timezone.utc)
        elapsed_s: float = (now_utc - last_emit).total_seconds()

        if elapsed_s >= window_s:
            log.debug(
                "[debounce] window expired — symbol=%s contract=%s alert=%s "
                "elapsed=%.1fs window=%.1fs",
                symbol, contract_key, alert_level, elapsed_s, window_s,
            )
            return True

        log.debug(
            "[debounce] suppressed — symbol=%s contract=%s alert=%s "
            "elapsed=%.1fs window=%.1fs remaining=%.1fs",
            symbol, contract_key, alert_level,
            elapsed_s, window_s, window_s - elapsed_s,
        )
        return False

    def record_emit(
        self,
        symbol: str,
        contract_key: str,
        alert_level: str,
        at: Optional[datetime] = None,
    ) -> None:
        """Stamp the last-emit time for this combination.

        Call this *after* a confirmed signal_history write, not before.
        If the DB write fails the guard state is not committed, which is
        the correct behaviour — a failed write should be retried.

        Parameters
        ----------
        symbol, contract_key, alert_level:
            Must match the values passed to the preceding ``should_emit()``
            call exactly.
        at:
            Explicit UTC datetime to stamp (used in tests to control time).
            Defaults to ``datetime.now(timezone.utc)``.
        """
        key: _DebounceKey = (symbol, contract_key, alert_level)
        stamp = at if at is not None else datetime.now(timezone.utc)
        self._state[key] = stamp
        log.debug(
            "[debounce] recorded — symbol=%s contract=%s alert=%s at=%s",
            symbol, contract_key, alert_level, stamp.isoformat(),
        )

    def clear(self) -> None:
        """Flush all debounce state.

        Intended for test teardown and post-market reset.  Calling this in
        production mid-session will cause all recently emitted signals to
        re-emit on the next episode close — use with care.
        """
        count = len(self._state)
        self._state.clear()
        log.info("[debounce] state cleared — evicted %d entries", count)

    def stats(self) -> dict:
        """Return a snapshot of current debounce state for ops/debug.

        Returns
        -------
        dict with keys:
            ``tracked_keys``   — number of (symbol, contract_key, alert_level)
                                 combinations currently in state.
            ``debounce_enabled`` — current live value from config store.
            ``window_seconds``   — current live window value from config store.
        """
        return {
            "tracked_keys":      len(self._state),
            "debounce_enabled":  self._read_enabled(),
            "window_seconds":    self._read_window_s(),
        }

    # ------------------------------------------------------------------
    # Config readers (private) — hot-reloadable, never cached
    # ------------------------------------------------------------------

    @staticmethod
    def _read_enabled() -> bool:
        """Read sig.debounce_enabled from the live config store.

        Falls back to ``_DEFAULT_DEBOUNCE_ENABLED`` (True) on any error so
        debounce is always active unless explicitly disabled.
        """
        try:
            val = get_param("sig.debounce_enabled", _DEFAULT_DEBOUNCE_ENABLED)
            # Config store may return 1.0 / 0.0 (numeric) or True/False.
            if isinstance(val, (int, float)):
                return bool(val)
            return bool(val)
        except Exception:
            return _DEFAULT_DEBOUNCE_ENABLED

    @staticmethod
    def _read_window_s() -> float:
        """Read sig.debounce_window_seconds from the live config store.

        Falls back to ``_DEFAULT_DEBOUNCE_WINDOW_S`` (300.0) on any error.
        """
        try:
            val = get_param("sig.debounce_window_seconds", _DEFAULT_DEBOUNCE_WINDOW_S)
            if val is not None and float(val) > 0:
                return float(val)
        except Exception:
            pass
        return _DEFAULT_DEBOUNCE_WINDOW_S


# ---------------------------------------------------------------------------
# Module-level singleton — shared by stream_worker and signal_store.
# Tests that need isolated state should instantiate a fresh SignalDebounce()
# directly rather than importing this singleton.
# ---------------------------------------------------------------------------
debounce = SignalDebounce()
