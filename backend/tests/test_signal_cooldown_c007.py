"""
C-007 — Signal Cooldown: RepetitionAccumulator suppresses repeated signals.

Fix verified:
  Before fix: ingest() returned ep on every call once thresholds were crossed.
              32 workers each processing post-threshold ticks = signal spam.
  After fix:  ingest() tracks last_signal_at per episode. Returns ep only on
              first threshold crossing OR after signal_cooldown has elapsed.
              Intermediate ticks return None — no signal, no DB write.

Test IDs:
  C007-1  first threshold crossing fires signal
  C007-2  second tick within cooldown is suppressed (returns None)
  C007-3  tick after cooldown expires fires again
  C007-4  cooldown is per-episode-key (TSLA CALL cooldown doesn't block TSLA PUT)
  C007-5  sub-threshold ticks always return None regardless of cooldown state
  C007-6  last_signal_at updated on each fired signal
  C007-7  window pruning still works — stale events evicted before threshold check
  C007-8  regression — get_alert_level still returns correct levels post-fix

Run: pytest backend/tests/test_signal_cooldown_c007.py -v
"""
import sys, os
from datetime import datetime, timedelta
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helper: build a minimal OptionsFlowEvent-like mock
# ---------------------------------------------------------------------------

def _make_ev(
    ticker="TSLA",
    contract_type="CALL",
    strike=375.0,
    expiry="2026-05-16",
    premium=100_000.0,
    timestamp: datetime = None,
):
    ev = MagicMock()
    ev.ticker = ticker
    ev.contract_type = contract_type
    ev.strike = strike
    ev.expiry = expiry
    ev.premium = premium
    ev.timestamp = timestamp or datetime(2026, 4, 26, 14, 30, 0)
    return ev


def _acc(cooldown_minutes=5):
    """RepetitionAccumulator with low thresholds for easy testing."""
    from signals.repetition_accumulator import RepetitionAccumulator
    return RepetitionAccumulator(
        window_minutes=30,
        min_trades=3,
        min_premium=50_000,
        signal_cooldown=cooldown_minutes,
    )


base_ts = datetime(2026, 4, 26, 14, 30, 0)


# ---------------------------------------------------------------------------
# C007-1: first threshold crossing fires signal
# ---------------------------------------------------------------------------
class TestC007FirstCrossing:
    def test_c007_1_first_threshold_crossing_fires(self):
        """C007-1: on the 3rd qualifying tick, ingest() must return an episode."""
        acc = _acc()
        ev1 = _make_ev(premium=20_000.0, timestamp=base_ts)
        ev2 = _make_ev(premium=20_000.0, timestamp=base_ts + timedelta(seconds=10))
        ev3 = _make_ev(premium=20_000.0, timestamp=base_ts + timedelta(seconds=20))

        assert acc.ingest(ev1) is None
        assert acc.ingest(ev2) is None
        ep = acc.ingest(ev3)
        assert ep is not None, "3rd tick at threshold should fire signal"
        assert ep.trade_count == 3
        assert ep.total_premium == 60_000.0


# ---------------------------------------------------------------------------
# C007-2: second tick within cooldown suppressed
# ---------------------------------------------------------------------------
class TestC007CooldownSuppresses:
    def test_c007_2_within_cooldown_returns_none(self):
        """C007-2: tick arriving 1 min after signal (cooldown=5min) must return None."""
        acc = _acc(cooldown_minutes=5)
        for i in range(3):
            acc.ingest(_make_ev(premium=20_000.0, timestamp=base_ts + timedelta(seconds=i * 10)))

        ev4 = _make_ev(premium=20_000.0, timestamp=base_ts + timedelta(minutes=1))
        result = acc.ingest(ev4)
        assert result is None, (
            f"Tick within cooldown should return None, got episode with "
            f"trade_count={result.trade_count if result else 'N/A'}"
        )


# ---------------------------------------------------------------------------
# C007-3: tick after cooldown expires fires again
# ---------------------------------------------------------------------------
class TestC007CooldownExpiry:
    def test_c007_3_after_cooldown_fires_again(self):
        """C007-3: tick arriving after cooldown elapsed must return an episode."""
        acc = _acc(cooldown_minutes=5)
        for i in range(3):
            acc.ingest(_make_ev(premium=20_000.0, timestamp=base_ts + timedelta(seconds=i * 10)))

        ev_late = _make_ev(premium=20_000.0, timestamp=base_ts + timedelta(minutes=6))
        result = acc.ingest(ev_late)
        assert result is not None, "Tick after cooldown should fire signal"
        assert result.trade_count == 4


# ---------------------------------------------------------------------------
# C007-4: cooldown is per-episode-key
# ---------------------------------------------------------------------------
class TestC007PerEpisodeKey:
    def test_c007_4_different_contract_not_blocked(self):
        """C007-4: TSLA PUT episode fires independently of TSLA CALL cooldown."""
        acc = _acc(cooldown_minutes=5)

        for i in range(3):
            acc.ingest(_make_ev(
                contract_type="CALL", premium=20_000.0,
                timestamp=base_ts + timedelta(seconds=i * 10)
            ))

        for i in range(2):
            acc.ingest(_make_ev(
                contract_type="PUT", premium=20_000.0,
                timestamp=base_ts + timedelta(seconds=30 + i * 10)
            ))
        put_ep = acc.ingest(_make_ev(
            contract_type="PUT", premium=20_000.0,
            timestamp=base_ts + timedelta(seconds=50)
        ))
        assert put_ep is not None, (
            "PUT episode should fire even while CALL cooldown is active"
        )
        assert put_ep.contract_type == "PUT"


# ---------------------------------------------------------------------------
# C007-5: sub-threshold ticks always return None
# ---------------------------------------------------------------------------
class TestC007SubThreshold:
    def test_c007_5_sub_threshold_always_none(self):
        """C007-5: even after cooldown expires, sub-threshold ticks must return None."""
        acc = _acc(cooldown_minutes=1)

        ev1 = _make_ev(premium=20_000.0, timestamp=base_ts)
        ev2 = _make_ev(premium=20_000.0, timestamp=base_ts + timedelta(minutes=10))

        assert acc.ingest(ev1) is None
        assert acc.ingest(ev2) is None


# ---------------------------------------------------------------------------
# C007-6: last_signal_at updated on each fired signal
# ---------------------------------------------------------------------------
class TestC007LastSignalAt:
    def test_c007_6_last_signal_at_updated(self):
        """C007-6: last_signal_at on the episode must equal the timestamp of the firing tick."""
        acc = _acc(cooldown_minutes=5)
        ts3 = base_ts + timedelta(seconds=20)

        acc.ingest(_make_ev(premium=20_000.0, timestamp=base_ts))
        acc.ingest(_make_ev(premium=20_000.0, timestamp=base_ts + timedelta(seconds=10)))
        ep = acc.ingest(_make_ev(premium=20_000.0, timestamp=ts3))

        assert ep is not None
        assert ep.last_signal_at == ts3, (
            f"Expected last_signal_at={ts3}, got {ep.last_signal_at}"
        )

        ts_late = base_ts + timedelta(minutes=6)
        ep2 = acc.ingest(_make_ev(premium=20_000.0, timestamp=ts_late))
        assert ep2 is not None
        assert ep2.last_signal_at == ts_late


# ---------------------------------------------------------------------------
# C007-7: window pruning evicts stale events before threshold check
# ---------------------------------------------------------------------------
class TestC007WindowPruning:
    def test_c007_7_stale_events_pruned(self):
        """C007-7: events outside window_minutes are pruned before threshold is checked."""
        acc = _acc()
        old_ts = base_ts - timedelta(minutes=35)
        for i in range(3):
            acc.ingest(_make_ev(premium=20_000.0, timestamp=old_ts + timedelta(seconds=i)))

        ev_fresh = _make_ev(premium=20_000.0, timestamp=base_ts)
        result = acc.ingest(ev_fresh)
        assert result is None, (
            "Stale events should be pruned; single fresh tick should not trigger signal"
        )


# ---------------------------------------------------------------------------
# C007-8: regression — get_alert_level still correct
# ---------------------------------------------------------------------------
class TestC007AlertLevelRegression:
    def test_c007_8_alert_levels_correct(self):
        """C007-8: get_alert_level must still return correct tiers post-fix."""
        from signals.repetition_accumulator import RepetitionAccumulator, RepetitionEpisode
        acc = RepetitionAccumulator()

        def _ep_with_premium(prem):
            ep = MagicMock(spec=RepetitionEpisode)
            ep.total_premium = prem
            ep.is_accelerating = False
            return ep

        assert acc.get_alert_level(_ep_with_premium(100_000))   == "WATCH"
        assert acc.get_alert_level(_ep_with_premium(300_000))   == "ALERT"
        assert acc.get_alert_level(_ep_with_premium(1_500_000)) == "STRONG_SIGNAL"
        assert acc.get_alert_level(_ep_with_premium(6_000_000)) == "CONVICTION"

        ep_accel = MagicMock(spec=RepetitionEpisode)
        ep_accel.total_premium = 1_000_000
        ep_accel.is_accelerating = True
        assert acc.get_alert_level(ep_accel) == "CONVICTION"
