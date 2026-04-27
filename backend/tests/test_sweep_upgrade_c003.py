"""
C-003 — Retroactive Sweep Upgrade.

Fix verified:
  Before fix: duplicate ticks from MIAX/PHLX confirming sweep threshold
              (3+ exchanges) were silently dropped. The canonical row already
              written to flow_events stayed as trade_type='BTO' forever.
  After fix:  on the DUPLICATE path, get_exchange_count() is checked. If
              count == sweep_min_exchanges exactly, upgrade_to_sweep_in_db()
              is fired as a background task to PATCH the DB row to 'SWEEP'.

Test IDs:
  C003-1  2 exchanges (below threshold) — upgrade NOT fired
  C003-2  3rd exchange exactly — upgrade_to_sweep_in_db called once
  C003-3  4th exchange — upgrade NOT fired again (guard prevents double-call)
  C003-4  upgrade_to_sweep_in_db builds correct PATCH URL and payload
  C003-5  upgrade_to_sweep_in_db is a no-op when Supabase not configured
  C003-6  canonical path with established sweep — ev.trade_type upgraded inline
  C003-7  regression: deduped events still never reach accumulator or persist
  C003-8  regression: qualifying canonical still writes to DB correctly

Run: pytest backend/tests/test_sweep_upgrade_c003.py -v
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ev(
    ticker="AAPL", contract_type="CALL", strike=200.0,
    premium=300_000.0, size=100, fill_price=3.0,
    bid=2.95, ask=3.05, trade_type="BTO",
    bid_ask_class="ASK", is_aggressive=True,
    is_golden_sweep=False, sentiment="BULLISH",
    influence_tier="INSTITUTIONAL", conviction_score=0.75,
    exchange_count=1, fill_count=1, open_interest=5000,
    iv=0.4, underlying_price=198.0, dte=21, expiry="2026-05-21",
    is_synthetic_quote=False,
):
    import datetime
    ev = MagicMock()
    ev.ticker = ticker
    ev.contract_type = contract_type
    ev.strike = strike
    ev.premium = premium
    ev.size = size
    ev.fill_price = fill_price
    ev.bid = bid
    ev.ask = ask
    ev.trade_type = trade_type
    ev.bid_ask_class = bid_ask_class
    ev.is_aggressive = is_aggressive
    ev.is_golden_sweep = is_golden_sweep
    ev.sentiment = sentiment
    ev.influence_tier = influence_tier
    ev.conviction_score = conviction_score
    ev.exchange_count = exchange_count
    ev.fill_count = fill_count
    ev.open_interest = open_interest
    ev.iv = iv
    ev.underlying_price = underlying_price
    ev.dte = dte
    ev.expiry = expiry
    ev.is_synthetic_quote = is_synthetic_quote
    ev.timestamp = datetime.datetime(2026, 4, 26, 14, 30, 0)
    return ev


def _make_ep():
    ep = MagicMock()
    ep.ticker = "AAPL"
    ep.contract_type = "CALL"
    ep.strike = 200.0
    ep.expiry = "2026-05-21"
    ep.trade_count = 3
    ep.total_premium = 900_000.0
    ep.is_accelerating = False
    ep.summary_str.return_value = "AAPL CALL x3"
    return ep


def _make_raw(occ="AAPL  260521C00200000", exchange="C"):
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


# ---------------------------------------------------------------------------
# C003-1: 2 exchanges — upgrade NOT fired
# ---------------------------------------------------------------------------
class TestC003BelowThreshold:
    @pytest.mark.asyncio
    async def test_c003_1_two_exchanges_no_upgrade(self):
        """C003-1: when exchange count is 2 (below sweep_min=3), upgrade must not fire."""
        from services import tradier_stream as ts

        ev = _make_ev()
        raw = _make_raw()

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.upgrade_to_sweep_in_db", new_callable=AsyncMock) as mock_upgrade:

            mock_dedup.is_duplicate.return_value = True
            mock_dedup.get_exchange_count.return_value = 2
            mock_dedup._sweep_min = 3

            await ts._process_trade(raw)

        mock_upgrade.assert_not_called()


# ---------------------------------------------------------------------------
# C003-2: 3rd exchange exactly — upgrade fired once
# ---------------------------------------------------------------------------
class TestC003ThresholdExactly:
    @pytest.mark.asyncio
    async def test_c003_2_third_exchange_fires_upgrade(self):
        """C003-2: when exchange count hits exactly 3, upgrade_to_sweep_in_db must be called."""
        from services import tradier_stream as ts

        ev = _make_ev()
        raw = _make_raw(exchange="M")
        tasks_created = []

        original_create_task = asyncio.create_task

        def _capture_task(coro):
            tasks_created.append(coro)
            async def _noop(): pass
            return original_create_task(_noop())

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.upgrade_to_sweep_in_db", new_callable=AsyncMock), \
             patch("services.tradier_stream.asyncio.create_task", side_effect=_capture_task):

            mock_dedup.is_duplicate.return_value = True
            mock_dedup.get_exchange_count.return_value = 3
            mock_dedup._sweep_min = 3

            await ts._process_trade(raw)

        assert len(tasks_created) == 1, (
            f"Expected 1 create_task call for sweep upgrade, got {len(tasks_created)}"
        )


# ---------------------------------------------------------------------------
# C003-3: 4th exchange — upgrade NOT fired again
# ---------------------------------------------------------------------------
class TestC003FourthExchangeNoRepeat:
    @pytest.mark.asyncio
    async def test_c003_3_fourth_exchange_no_upgrade(self):
        """C003-3: when exchange count is 4 (already past threshold), upgrade must not fire again."""
        from services import tradier_stream as ts

        ev = _make_ev()
        raw = _make_raw(exchange="X")
        tasks_created = []

        def _capture_task(coro):
            tasks_created.append(coro)
            async def _noop(): pass
            return asyncio.create_task(_noop())

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.upgrade_to_sweep_in_db", new_callable=AsyncMock), \
             patch("services.tradier_stream.asyncio.create_task", side_effect=_capture_task):

            mock_dedup.is_duplicate.return_value = True
            mock_dedup.get_exchange_count.return_value = 4
            mock_dedup._sweep_min = 3

            await ts._process_trade(raw)

        assert len(tasks_created) == 0, (
            f"Expected 0 create_task calls for 4th exchange, got {len(tasks_created)}"
        )


# ---------------------------------------------------------------------------
# C003-4: upgrade_to_sweep_in_db builds correct PATCH URL and payload
# ---------------------------------------------------------------------------
class TestC003UpgradeFnPayload:
    @pytest.mark.asyncio
    async def test_c003_4_upgrade_fn_correct_patch_request(self):
        """C003-4: upgrade_to_sweep_in_db must PATCH with trade_type='SWEEP'."""
        import os
        from services import flow_store

        patched_responses = []

        class _MockResp:
            status_code = 200
            text = ""

        class _MockClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def patch(self, url, headers, json):
                patched_responses.append({"url": url, "json": json})
                return _MockResp()

        with patch.dict(os.environ, {
            "SUPABASE_URL": "https://test.supabase.co",
            "SUPABASE_SERVICE_ROLE_KEY": "test_key",
        }):
            flow_store._SUPABASE_URL = "https://test.supabase.co"
            flow_store._SUPABASE_KEY = "test_key"

            with patch("services.flow_store.httpx.AsyncClient", return_value=_MockClient()):
                await flow_store.upgrade_to_sweep_in_db(
                    occ_symbol="AAPL  260521C00200000",
                    fill_price=3.0,
                    size=100,
                )

        assert len(patched_responses) == 1
        assert patched_responses[0]["json"] == {"trade_type": "SWEEP"}
        assert "AAPL" in patched_responses[0]["url"]
        assert "trade_type=neq.SWEEP" in patched_responses[0]["url"]


# ---------------------------------------------------------------------------
# C003-5: upgrade_to_sweep_in_db no-op when not configured
# ---------------------------------------------------------------------------
class TestC003NotConfigured:
    @pytest.mark.asyncio
    async def test_c003_5_no_op_when_not_configured(self):
        """C003-5: upgrade_to_sweep_in_db must return False silently when Supabase not set."""
        from services import flow_store

        original_url = flow_store._SUPABASE_URL
        original_key = flow_store._SUPABASE_KEY
        try:
            flow_store._SUPABASE_URL = None
            flow_store._SUPABASE_KEY = None
            result = await flow_store.upgrade_to_sweep_in_db(
                occ_symbol="AAPL  260521C00200000",
                fill_price=3.0,
                size=100,
            )
        finally:
            flow_store._SUPABASE_URL = original_url
            flow_store._SUPABASE_KEY = original_key

        assert result is False


# ---------------------------------------------------------------------------
# C003-6: canonical path with established sweep upgrades ev.trade_type inline
# ---------------------------------------------------------------------------
class TestC003CanonicalSweepInline:
    @pytest.mark.asyncio
    async def test_c003_6_canonical_with_prior_sweep_pattern(self):
        """C003-6: canonical tick arriving after sweep was established gets trade_type='SWEEP' inline."""
        from services import tradier_stream as ts

        ev = _make_ev(trade_type="BTO")
        ep = _make_ep()
        raw = _make_raw()

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", new_callable=AsyncMock) as mock_persist, \
             patch("services.tradier_stream.build_composite", return_value=None), \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = True
            mock_dedup.get_exchange_count.return_value = 4
            mock_acc.ingest_tick.return_value = ep
            mock_acc.get_signal.return_value = ep
            mock_acc.get_alert_level.return_value = "CONVICTION"
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        assert ev.trade_type == "SWEEP", (
            f"Expected ev.trade_type='SWEEP', got '{ev.trade_type}'"
        )
        mock_persist.assert_awaited_once()


# ---------------------------------------------------------------------------
# C003-7: regression — deduped events still never reach accumulator or persist
# ---------------------------------------------------------------------------
class TestC003DedupRegressionCheck:
    @pytest.mark.asyncio
    async def test_c003_7_deduped_no_accumulator_no_persist(self):
        """C003-7: deduped events must still never reach accumulator.ingest_tick or persist_flow_event."""
        from services import tradier_stream as ts

        ev = _make_ev()
        raw = _make_raw()

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", new_callable=AsyncMock) as mock_persist, \
             patch("services.tradier_stream.asyncio.create_task"):

            mock_dedup.is_duplicate.return_value = True
            mock_dedup.get_exchange_count.return_value = 2
            mock_dedup._sweep_min = 3

            await ts._process_trade(raw)

        mock_acc.ingest_tick.assert_not_called()
        mock_persist.assert_not_called()


# ---------------------------------------------------------------------------
# C003-8: regression — qualifying canonical still writes to DB normally
# ---------------------------------------------------------------------------
class TestC003QualifyingCanonicalRegression:
    @pytest.mark.asyncio
    async def test_c003_8_qualifying_canonical_persists(self):
        """C003-8: qualifying canonical ticks still write to flow_events as before."""
        from services import tradier_stream as ts

        ev = _make_ev()
        ep = _make_ep()
        raw = _make_raw()

        with patch("services.tradier_stream.parse_tradier_trade", return_value=ev), \
             patch("services.tradier_stream.flow_dedup") as mock_dedup, \
             patch("services.tradier_stream.accumulator") as mock_acc, \
             patch("services.tradier_stream.persist_flow_event", new_callable=AsyncMock) as mock_persist, \
             patch("services.tradier_stream.build_composite", return_value=None), \
             patch("services.tradier_stream.bus") as mock_bus:

            mock_dedup.is_duplicate.return_value = False
            mock_dedup.is_sweep.return_value = False
            mock_acc.ingest_tick.return_value = ep
            mock_acc.get_signal.return_value = None
            mock_acc.get_alert_level.return_value = "ALERT"
            mock_bus.publish_all = AsyncMock()

            await ts._process_trade(raw)

        mock_persist.assert_awaited_once()
        args = mock_persist.call_args[0][0]
        assert args["ticker"] == "AAPL"
        assert args["trade_type"] == "BTO"
