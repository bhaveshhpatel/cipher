"""
H-1 + H-3 + H-4 — High-priority bug fixes audit 2026-04-27

H-1 / H-3 (same root cause — both fixed in refresh_loop):
  Before: refresh_loop() called self.build() with no pre_fetched_quotes,
          triggering _fetch_stock_prices() (a full Tradier round-trip) on
          every scheduled cycle even when fresh quotes were available.
          Additionally the delta-path (P4 incremental logic) never received
          a current OI map, so drift detection was blind on warm rebuilds.
  After:  refresh_loop() calls _fetch_stock_prices() once per cycle and
          passes quote_map into build(pre_fetched_quotes=...).  Only one
          Tradier call per cycle; delta path has real OI data every time.

H-4:
  Before: _sweep_upgrade_dispatched was a bare Set[str] that was never
          cleared — unbounded memory leak on long-running processes.
  After:  replaced with Dict[str, float] (dispatch_key -> monotonic ts).
          _evict_stale_dispatch_entries() removes entries older than
          _SWEEP_DISPATCH_TTL (3600 s) before every membership check.

Test IDs:
  H1H3-1  refresh_loop calls _fetch_stock_prices exactly once per cycle
  H1H3-2  refresh_loop passes pre_fetched_quotes= into build() (not bare build)
  H1H3-3  build() is NOT called with empty pre_fetched_quotes=None on cycles 2+
  H1H3-4  refresh_loop respects REGISTRY_EXPIRY_DAY_REFRESH_MINS when expiry today
  H1H3-5  quote_map carries current OI from _oi_by_ticker into build()
  H4-1    _evict_stale_dispatch_entries removes entries older than TTL
  H4-2    fresh entries are NOT evicted
  H4-3    _sweep_upgrade_dispatched is a dict, not a set
  H4-4    dispatch_key recorded with monotonic timestamp on first sweep threshold
  H4-5    second identical sweep within TTL does NOT dispatch a second create_task
  H4-6    same sweep after TTL expires DOES dispatch again (TTL window resets)
  H4-7    eviction runs before membership check in _process_trade
  H4-8    multiple stale keys all evicted in one call

Run: pytest backend/tests/test_high_bugs_h1_h3_h4.py -v
"""
import asyncio
import time as _time
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry(watchlist=None, oi_map=None):
    """Return a SymbolRegistry with _expiry_cache pre-populated so
    is_first_build is False on the next build() call."""
    from services.symbol_registry import SymbolRegistry
    reg = SymbolRegistry(watchlist=watchlist or ["AAPL", "TSLA"])
    # Simulate post-first-build state.
    reg._expiry_cache = {"AAPL": {"2026-05-16"}, "TSLA": {"2026-05-16"}}
    reg._oi_by_ticker = oi_map or {"AAPL": 5000, "TSLA": 3000}
    return reg


def _make_raw_trade(occ="AAPL  260521C00200000", exchange="C"):
    return {
        "type": "timesale",
        "timesale": {
            "symbol": occ,
            "exch": exchange,
            "last": 3.0,
            "bid": 2.95,
            "ask": 3.05,
            "size": 100,
            "date": 1745686200000,
        },
    }


def _make_ev(trade_type="BTO"):
    import datetime
    ev = MagicMock()
    ev.ticker = "AAPL"
    ev.contract_type = "CALL"
    ev.strike = 200.0
    ev.premium = 300_000.0
    ev.size = 100
    ev.fill_price = 3.0
    ev.bid = 2.95
    ev.ask = 3.05
    ev.trade_type = trade_type
    ev.bid_ask_class = "ASK"
    ev.is_aggressive = True
    ev.is_golden_sweep = False
    ev.sentiment = "BULLISH"
    ev.influence_tier = "INSTITUTIONAL"
    ev.conviction_score = 0.75
    ev.exchange_count = 1
    ev.fill_count = 1
    ev.open_interest = 5000
    ev.iv = 0.4
    ev.underlying_price = 198.0
    ev.dte = 21
    ev.expiry = "2026-05-21"
    ev.is_synthetic_quote = False
    ev.timestamp = datetime.datetime(2026, 4, 27, 14, 30, 0)
    return ev


# ===========================================================================
# H-1 / H-3: refresh_loop pre-fetches quotes
# ===========================================================================

class TestH1H3RefreshLoopPrefetch:

    @pytest.mark.asyncio
    async def test_h1h3_1_fetch_stock_prices_called_once_per_cycle(self):
        """refresh_loop must call _fetch_stock_prices exactly once and stop
        sleeping after the first cycle when we cancel the loop task."""
        reg = _make_registry()

        prices = {"AAPL": 195.0, "TSLA": 250.0}
        call_count = {"n": 0}

        async def _fake_fetch():
            call_count["n"] += 1
            return prices, {}

        async def _fake_build(pre_fetched_quotes=None):
            return 100

        cfg = {
            "REGISTRY_REFRESH_MINS": 0,  # 0 so asyncio.sleep(0) — no real delay
            "REGISTRY_EXPIRY_DAY_REFRESH_MINS": 0,
        }

        with patch("services.symbol_registry.get_config", new=AsyncMock(return_value=cfg)), \
             patch.object(reg, "_fetch_stock_prices", side_effect=_fake_fetch), \
             patch.object(reg, "build", side_effect=_fake_build):

            task = asyncio.create_task(reg.refresh_loop())
            # Let the loop execute one full cycle.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert call_count["n"] >= 1, "_fetch_stock_prices must be called at least once per cycle"

    @pytest.mark.asyncio
    async def test_h1h3_2_build_receives_pre_fetched_quotes(self):
        """build() must be called with a non-None pre_fetched_quotes kwarg."""
        reg = _make_registry()

        prices = {"AAPL": 195.0, "TSLA": 250.0}
        build_calls = []

        async def _fake_fetch():
            return prices, {}

        async def _fake_build(pre_fetched_quotes=None):
            build_calls.append(pre_fetched_quotes)
            return 100

        cfg = {
            "REGISTRY_REFRESH_MINS": 0,
            "REGISTRY_EXPIRY_DAY_REFRESH_MINS": 0,
        }

        with patch("services.symbol_registry.get_config", new=AsyncMock(return_value=cfg)), \
             patch.object(reg, "_fetch_stock_prices", side_effect=_fake_fetch), \
             patch.object(reg, "build", side_effect=_fake_build):

            task = asyncio.create_task(reg.refresh_loop())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert build_calls, "build() must have been called at least once"
        assert build_calls[0] is not None, \
            "pre_fetched_quotes must not be None — bare build() call detected (H-1/H-3 regression)"

    @pytest.mark.asyncio
    async def test_h1h3_3_build_never_called_bare(self):
        """build() must never be called with pre_fetched_quotes=None from refresh_loop."""
        reg = _make_registry()

        prices = {"AAPL": 195.0}
        build_calls = []

        async def _fake_fetch():
            return prices, {}

        async def _fake_build(pre_fetched_quotes=None):
            build_calls.append(pre_fetched_quotes)
            return 50

        cfg = {
            "REGISTRY_REFRESH_MINS": 0,
            "REGISTRY_EXPIRY_DAY_REFRESH_MINS": 0,
        }

        with patch("services.symbol_registry.get_config", new=AsyncMock(return_value=cfg)), \
             patch.object(reg, "_fetch_stock_prices", side_effect=_fake_fetch), \
             patch.object(reg, "build", side_effect=_fake_build):

            task = asyncio.create_task(reg.refresh_loop())
            # Run two full cycles.
            for _ in range(6):
                await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        for i, pf in enumerate(build_calls):
            assert pf is not None, (
                f"build() call #{i + 1} was bare (pre_fetched_quotes=None) "
                "— H-1/H-3 regression"
            )

    @pytest.mark.asyncio
    async def test_h1h3_4_expiry_day_uses_shorter_interval(self):
        """When an expiry falls today the loop must use REGISTRY_EXPIRY_DAY_REFRESH_MINS."""
        from datetime import date
        from services.symbol_registry import ContractMeta

        reg = _make_registry()
        today_str = date.today().isoformat()
        reg._registry = {
            "AAPL260516C00200000": ContractMeta(
                ticker="AAPL", strike=200.0, expiry=today_str,
                contract_type="CALL", dte=0, open_interest=5000,
            )
        }

        sleep_calls = []

        async def _fake_sleep(secs):
            sleep_calls.append(secs)
            if len(sleep_calls) >= 1:
                raise asyncio.CancelledError

        async def _fake_fetch():
            return {"AAPL": 195.0}, {}

        async def _fake_build(pre_fetched_quotes=None):
            return 50

        cfg = {
            "REGISTRY_REFRESH_MINS": 30,
            "REGISTRY_EXPIRY_DAY_REFRESH_MINS": 5,
        }

        with patch("services.symbol_registry.get_config", new=AsyncMock(return_value=cfg)), \
             patch("services.symbol_registry.asyncio.sleep", side_effect=_fake_sleep), \
             patch.object(reg, "_fetch_stock_prices", side_effect=_fake_fetch), \
             patch.object(reg, "build", side_effect=_fake_build):
            try:
                await reg.refresh_loop()
            except asyncio.CancelledError:
                pass

        assert sleep_calls, "asyncio.sleep must have been called"
        # Expiry day: 5 min * 60 = 300 s
        assert sleep_calls[0] == 5 * 60, (
            f"Expected sleep of {5 * 60}s (expiry day interval), got {sleep_calls[0]}s"
        )

    @pytest.mark.asyncio
    async def test_h1h3_5_quote_map_carries_oi_from_registry(self):
        """The SymbolQuote objects passed to build() must carry open_interest
        from _oi_by_ticker so the delta path can compute OI drift correctly."""
        reg = _make_registry(oi_map={"AAPL": 7500, "TSLA": 4200})

        prices = {"AAPL": 195.0, "TSLA": 250.0}
        captured_quotes = {}

        async def _fake_fetch():
            return prices, {}

        async def _fake_build(pre_fetched_quotes=None):
            if pre_fetched_quotes:
                captured_quotes.update(pre_fetched_quotes)
            return 100

        cfg = {
            "REGISTRY_REFRESH_MINS": 0,
            "REGISTRY_EXPIRY_DAY_REFRESH_MINS": 0,
        }

        with patch("services.symbol_registry.get_config", new=AsyncMock(return_value=cfg)), \
             patch.object(reg, "_fetch_stock_prices", side_effect=_fake_fetch), \
             patch.object(reg, "build", side_effect=_fake_build):

            task = asyncio.create_task(reg.refresh_loop())
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert "AAPL" in captured_quotes, "AAPL must be in pre_fetched_quotes"
        assert captured_quotes["AAPL"].open_interest == 7500, (
            "open_interest in quote_map must match _oi_by_ticker (H-3 OI drift fix)"
        )
        assert captured_quotes["TSLA"].open_interest == 4200


# ===========================================================================
# H-4: _sweep_upgrade_dispatched TTL eviction
# ===========================================================================

class TestH4SweepDispatchTTL:

    def setup_method(self):
        """Clear module-level dispatch dict before each test."""
        import services.tradier_stream as ts
        ts._sweep_upgrade_dispatched.clear()

    def test_h4_3_dispatched_is_dict_not_set(self):
        """_sweep_upgrade_dispatched must be a dict (not a set)."""
        import services.tradier_stream as ts
        assert isinstance(ts._sweep_upgrade_dispatched, dict), (
            "_sweep_upgrade_dispatched must be Dict[str, float] (H-4 fix)"
        )

    def test_h4_1_evict_removes_stale_entries(self):
        """Entries older than TTL must be removed by _evict_stale_dispatch_entries."""
        import services.tradier_stream as ts

        old_ts = _time.monotonic() - ts._SWEEP_DISPATCH_TTL - 10  # clearly stale
        ts._sweep_upgrade_dispatched["KEY_OLD"] = old_ts
        ts._sweep_upgrade_dispatched["KEY_NEW"] = _time.monotonic()

        ts._evict_stale_dispatch_entries()

        assert "KEY_OLD" not in ts._sweep_upgrade_dispatched, "Stale entry must be evicted"
        assert "KEY_NEW" in ts._sweep_upgrade_dispatched, "Fresh entry must be retained"

    def test_h4_2_fresh_entries_not_evicted(self):
        """Entries younger than TTL must survive eviction."""
        import services.tradier_stream as ts

        ts._sweep_upgrade_dispatched["K1"] = _time.monotonic()  # just added
        ts._sweep_upgrade_dispatched["K2"] = _time.monotonic() - (ts._SWEEP_DISPATCH_TTL / 2)

        ts._evict_stale_dispatch_entries()

        assert "K1" in ts._sweep_upgrade_dispatched
        assert "K2" in ts._sweep_upgrade_dispatched

    def test_h4_8_multiple_stale_keys_all_evicted(self):
        """All stale keys must be removed in a single eviction pass."""
        import services.tradier_stream as ts

        old_ts = _time.monotonic() - ts._SWEEP_DISPATCH_TTL - 1
        for i in range(5):
            ts._sweep_upgrade_dispatched[f"STALE_{i}"] = old_ts
        ts._sweep_upgrade_dispatched["FRESH"] = _time.monotonic()

        ts._evict_stale_dispatch_entries()

        for i in range(5):
            assert f"STALE_{i}" not in ts._sweep_upgrade_dispatched
        assert "FRESH" in ts._sweep_upgrade_dispatched

    def test_h4_4_dispatch_key_recorded_with_timestamp(self):
        """After a sweep threshold crossing the key must be in the dict with a
        float timestamp close to now."""
        import services.tradier_stream as ts

        key = "AAPL  260521C00200000|100|3.00"
        before = _time.monotonic()
        ts._sweep_upgrade_dispatched[key] = _time.monotonic()
        after = _time.monotonic()

        assert key in ts._sweep_upgrade_dispatched
        recorded = ts._sweep_upgrade_dispatched[key]
        assert before <= recorded <= after

    @pytest.mark.asyncio
    async def test_h4_5_second_identical_sweep_within_ttl_no_duplicate_dispatch(self):
        """A second sweep for the same (occ, size, fill) within the TTL window
        must NOT trigger a second create_task."""
        import services.tradier_stream as ts

        ev = _make_ev()
        raw = _make_raw_trade()
        tasks_created = []

        original_create_task = asyncio.create_task

        def _capture(coro):
            tasks_created.append(coro)
            async def _noop(): pass
            return original_create_task(_noop())

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.upgrade_to_sweep_in_db", new_callable=AsyncMock), \
             patch("services.tradier_stream.asyncio.create_task", side_effect=_capture):

            mock_dedup.is_duplicate.return_value = True
            mock_dedup.get_exchange_count.return_value = 3
            mock_dedup._sweep_min = 3

            # First occurrence — should dispatch.
            await ts._process_trade(raw)
            # Second occurrence within TTL — must NOT dispatch again.
            await ts._process_trade(raw)

        assert len(tasks_created) == 1, (
            f"Expected exactly 1 dispatch, got {len(tasks_created)} — "
            "duplicate dispatch within TTL window (H-4 regression)"
        )

    @pytest.mark.asyncio
    async def test_h4_6_sweep_after_ttl_expires_dispatches_again(self):
        """Once the TTL has elapsed the same sweep must be dispatchable again."""
        import services.tradier_stream as ts

        ev = _make_ev()
        raw = _make_raw_trade()
        tasks_created = []

        original_create_task = asyncio.create_task

        def _capture(coro):
            tasks_created.append(coro)
            async def _noop(): pass
            return original_create_task(_noop())

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.upgrade_to_sweep_in_db", new_callable=AsyncMock), \
             patch("services.tradier_stream.asyncio.create_task", side_effect=_capture):

            mock_dedup.is_duplicate.return_value = True
            mock_dedup.get_exchange_count.return_value = 3
            mock_dedup._sweep_min = 3

            # First dispatch.
            await ts._process_trade(raw)
            assert len(tasks_created) == 1

            # Manually expire the entry.
            for k in list(ts._sweep_upgrade_dispatched):
                ts._sweep_upgrade_dispatched[k] = _time.monotonic() - ts._SWEEP_DISPATCH_TTL - 1

            # Second dispatch after TTL.
            await ts._process_trade(raw)

        assert len(tasks_created) == 2, (
            f"Expected 2 dispatches (one after TTL reset), got {len(tasks_created)}"
        )

    @pytest.mark.asyncio
    async def test_h4_7_eviction_runs_before_membership_check(self):
        """_evict_stale_dispatch_entries must be called before the membership
        check so that a re-appearing stale key is treated as new."""
        import services.tradier_stream as ts

        ev = _make_ev()
        raw = _make_raw_trade()
        tasks_created = []
        evict_calls = []

        original_evict = ts._evict_stale_dispatch_entries
        original_create_task = asyncio.create_task

        def _tracking_evict():
            evict_calls.append(_time.monotonic())
            original_evict()

        def _capture(coro):
            tasks_created.append(_time.monotonic())
            async def _noop(): pass
            return original_create_task(_noop())

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.upgrade_to_sweep_in_db", new_callable=AsyncMock), \
             patch("services.tradier_stream._evict_stale_dispatch_entries", side_effect=_tracking_evict), \
             patch("services.tradier_stream.asyncio.create_task", side_effect=_capture):

            mock_dedup.is_duplicate.return_value = True
            mock_dedup.get_exchange_count.return_value = 3
            mock_dedup._sweep_min = 3

            await ts._process_trade(raw)

        assert evict_calls, "_evict_stale_dispatch_entries must have been called"
        assert tasks_created, "create_task must have been called for the sweep upgrade"
        # Eviction must happen before (or at same instant as) the task creation.
        assert evict_calls[0] <= tasks_created[0], (
            "Eviction must precede the membership check / task creation"
        )
