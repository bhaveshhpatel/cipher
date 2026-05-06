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
import asyncio
import sys
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


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
    from signals.repetition_accumulator import RepetitionAccumulator
    return RepetitionAccumulator(
        window_minutes=30,
        min_trades=3,
        min_premium=50_000,
        signal_cooldown=cooldown_minutes,
    )


base_ts = datetime(2026, 4, 26, 14, 30, 0)


class TestC007FirstCrossing:
    def test_c007_1_first_threshold_crossing_fires(self):
        acc = _acc()
        ev1 = _make_ev(premium=20_000.0, timestamp=base_ts)
        ev2 = _make_ev(premium=20_000.0, timestamp=base_ts + timedelta(seconds=10))
        ev3 = _make_ev(premium=20_000.0, timestamp=base_ts + timedelta(seconds=20))

        assert asyncio.run(acc.ingest(ev1)) is None
        assert asyncio.run(acc.ingest(ev2)) is None
        ep = asyncio.run(acc.ingest(ev3))
        assert ep is not None, "3rd tick at threshold should fire signal"
        assert ep.trade_count == 3
        assert ep.total_premium == 60_000.0


class TestC007CooldownSuppresses:
    def test_c007_2_within_cooldown_returns_none(self):
        acc = _acc(cooldown_minutes=5)
        for i in range(3):
            asyncio.run(acc.ingest(_make_ev(premium=20_000.0, timestamp=base_ts + timedelta(seconds=i * 10))))

        ev4 = _make_ev(premium=20_000.0, timestamp=base_ts + timedelta(minutes=1))
        result = asyncio.run(acc.ingest(ev4))
        assert result is None, (
            f"Tick within cooldown should return None, got episode with "
            f"trade_count={result.trade_count if result else 'N/A'}"
        )


class TestC007CooldownExpiry:
    def test_c007_3_after_cooldown_fires_again(self):
        acc = _acc(cooldown_minutes=5)
        for i in range(3):
            asyncio.run(acc.ingest(_make_ev(premium=20_000.0, timestamp=base_ts + timedelta(seconds=i * 10))))

        # Within cooldown — should be suppressed
        asyncio.run(acc.ingest(_make_ev(premium=20_000.0, timestamp=base_ts + timedelta(minutes=1))))

        # After cooldown expires
        ev_late = _make_ev(premium=20_000.0, timestamp=base_ts + timedelta(minutes=6))
        result = asyncio.run(acc.ingest(ev_late))
        assert result is not None, "Tick after cooldown should fire signal"
        assert result.trade_count >= 4


class TestC007PerEpisodeKey:
    def test_c007_4_different_contract_not_blocked(self):
        acc = _acc(cooldown_minutes=5)

        for i in range(3):
            asyncio.run(acc.ingest(_make_ev(
                contract_type="CALL", premium=20_000.0,
                timestamp=base_ts + timedelta(seconds=i * 10)
            )))

        for i in range(2):
            asyncio.run(acc.ingest(_make_ev(
                contract_type="PUT", premium=20_000.0,
                timestamp=base_ts + timedelta(seconds=30 + i * 10)
            )))
        put_ep = asyncio.run(acc.ingest(_make_ev(
            contract_type="PUT", premium=20_000.0,
            timestamp=base_ts + timedelta(seconds=50)
        )))
        assert put_ep is not None, "PUT episode should fire even while CALL cooldown is active"
        assert put_ep.contract_type == "PUT"


class TestC007SubThreshold:
    def test_c007_5_sub_threshold_always_none(self):
        acc = _acc(cooldown_minutes=1)
        ev1 = _make_ev(premium=20_000.0, timestamp=base_ts)
        ev2 = _make_ev(premium=20_000.0, timestamp=base_ts + timedelta(minutes=10))

        assert asyncio.run(acc.ingest(ev1)) is None
        assert asyncio.run(acc.ingest(ev2)) is None


class TestC007LastSignalAt:
    def test_c007_6_last_signal_at_updated(self):
        acc = _acc(cooldown_minutes=5)
        ts3 = base_ts + timedelta(seconds=20)

        asyncio.run(acc.ingest(_make_ev(premium=20_000.0, timestamp=base_ts)))
        asyncio.run(acc.ingest(_make_ev(premium=20_000.0, timestamp=base_ts + timedelta(seconds=10))))
        ep = asyncio.run(acc.ingest(_make_ev(premium=20_000.0, timestamp=ts3)))

        assert ep is not None
        # last_signal_at is stored with UTC tzinfo; compare ignoring tzinfo
        assert ep.last_signal_at is not None
        assert ep.last_signal_at.replace(tzinfo=None) == ts3

        ts_late = base_ts + timedelta(minutes=6)
        ep2 = asyncio.run(acc.ingest(_make_ev(premium=20_000.0, timestamp=ts_late)))
        assert ep2 is not None
        assert ep2.last_signal_at.replace(tzinfo=None) == ts_late


class TestC007WindowPruning:
    def test_c007_7_stale_events_pruned(self):
        acc = _acc()
        old_ts = base_ts - timedelta(minutes=35)
        for i in range(3):
            asyncio.run(acc.ingest(_make_ev(premium=20_000.0, timestamp=old_ts + timedelta(seconds=i))))

        ev_fresh = _make_ev(premium=20_000.0, timestamp=base_ts)
        result = asyncio.run(acc.ingest(ev_fresh))
        assert result is None, "Stale events should be pruned; single fresh tick should not trigger signal"


class TestC007AlertLevelRegression:
    def test_c007_8_alert_levels_correct(self):
        from signals.repetition_accumulator import RepetitionAccumulator, RepetitionEpisode
        acc = RepetitionAccumulator()

        def _ep_with_premium(prem):
            """Build a real RepetitionEpisode so isinstance check passes."""
            ep = RepetitionEpisode(ticker="TEST", contract_type="CALL",
                                   strike=100.0, expiry="2026-05-16")
            # Add 3 non-accelerating events with equal premium split
            from datetime import datetime, timedelta
            base = datetime(2026, 4, 26, 10, 0, 0)
            for i, offset in enumerate([0, 500, 1000]):
                ev = MagicMock()
                ev.premium = prem / 3
                ev.timestamp = base + timedelta(seconds=offset)
                ev.is_aggressive = False
                ep.events.append(ev)
            return ep

        # Canonical table: >= 100_000 -> LARGE, < 100_000 -> WATCH.
        # Use 99_000 to probe below the LARGE boundary.
        assert acc.get_alert_level(_ep_with_premium(99_000))    == "WATCH"
        assert acc.get_alert_level(_ep_with_premium(300_000))   == "ALERT"
        assert acc.get_alert_level(_ep_with_premium(1_500_000)) == "STRONG_SIGNAL"
        assert acc.get_alert_level(_ep_with_premium(6_000_000)) == "CONVICTION"

        ep_accel = RepetitionEpisode(ticker="TEST", contract_type="CALL",
                                     strike=100.0, expiry="2026-05-16")
        base = datetime(2026, 4, 26, 10, 0, 0)
        for i, offset in enumerate([0, 20, 40]):
            ev = MagicMock()
            ev.premium = 1_000_000 / 3
            ev.timestamp = base + timedelta(seconds=offset)
            ev.is_aggressive = True
            ep_accel.events.append(ev)
        assert acc.get_alert_level(ep_accel) == "CONVICTION"
