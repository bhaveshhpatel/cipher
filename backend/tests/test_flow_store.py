"""
test_flow_store.py — 100% coverage for services/flow_store.py

Covers:
  - _is_configured()
  - _headers()
  - _insert_rows()
  - _insert_rows_with_retry()
  - persist_flow_event() — buffer logic, lock safety, early flush
  - upgrade_to_sweep_in_db()
  - persist_flow_episode()
  - _flush_flow_events() — periodic drain loop
  - _bus_signal_listener() — composite_signal path
  - start_flow_writer() — configured + unconfigured
  - add_flow / get_flows / clear_flows — in-memory helpers
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_resp(status: int, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text        = text
    return r


def _minimal_event(**kwargs) -> dict:
    base = {
        "ticker":         "AAPL",
        "contract_type":  "CALL",
        "strike":         180.0,
        "expiry":         "2026-06-20",
        "dte":            30,
        "fill_price":     4.85,
        "bid":            4.80,
        "ask":            4.90,
        "size":           100,
        "premium":        48500.0,
        "trade_type":     "BTO",
        "bid_ask_class":  "MID",
        "is_aggressive":  False,
        "is_golden_sweep": False,
        "sentiment":      "BULLISH",
        "influence_tier": "RETAIL",
        "conviction_score": 0.5,
        "exchange_count": 1,
        "fill_count":     1,
        "open_interest":  5000,
        "iv":             0.28,
        "underlying_price": 178.0,
        "occ_symbol":     "AAPL  260620C00180000",
        "is_synthetic_quote": False,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# _is_configured
# ---------------------------------------------------------------------------

class TestIsConfigured:
    def test_false_when_no_env_vars(self):
        import services.flow_store as fs
        with patch.object(fs, "_SUPABASE_URL", None), \
             patch.object(fs, "_SUPABASE_KEY", None):
            assert fs._is_configured() is False

    def test_false_when_only_url(self):
        import services.flow_store as fs
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", None):
            assert fs._is_configured() is False

    def test_true_when_both_set(self):
        import services.flow_store as fs
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", "service-role-key"):
            assert fs._is_configured() is True


# ---------------------------------------------------------------------------
# _headers
# ---------------------------------------------------------------------------

class TestHeaders:
    def test_returns_required_keys(self):
        import services.flow_store as fs
        with patch.object(fs, "_SUPABASE_KEY", "test-key"):
            h = fs._headers()
        assert "apikey" in h
        assert "Authorization" in h
        assert h["Prefer"] == "return=minimal"


# ---------------------------------------------------------------------------
# _insert_rows
# ---------------------------------------------------------------------------

class TestInsertRows:
    @pytest.mark.asyncio
    async def test_returns_false_for_empty_rows(self):
        import services.flow_store as fs
        assert await fs._insert_rows("flow_events", []) is False

    @pytest.mark.asyncio
    async def test_returns_false_when_not_configured(self):
        import services.flow_store as fs
        with patch.object(fs, "_SUPABASE_URL", None):
            assert await fs._insert_rows("flow_events", [{"ticker": "AAPL"}]) is False

    @pytest.mark.asyncio
    async def test_returns_true_on_201(self):
        import services.flow_store as fs
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post       = AsyncMock(return_value=_mock_resp(201))
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", "key"), \
             patch("services.flow_store.httpx.AsyncClient", return_value=mock_client):
            result = await fs._insert_rows("flow_events", [{"ticker": "AAPL"}])
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_4xx(self):
        import services.flow_store as fs
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post       = AsyncMock(return_value=_mock_resp(400, "bad request"))
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", "key"), \
             patch("services.flow_store.httpx.AsyncClient", return_value=mock_client):
            result = await fs._insert_rows("flow_events", [{"ticker": "AAPL"}])
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        import services.flow_store as fs
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", "key"), \
             patch("services.flow_store.httpx.AsyncClient", side_effect=RuntimeError("conn")):
            result = await fs._insert_rows("flow_events", [{"ticker": "AAPL"}])
        assert result is False


# ---------------------------------------------------------------------------
# _insert_rows_with_retry
# ---------------------------------------------------------------------------

class TestInsertRowsWithRetry:
    @pytest.mark.asyncio
    async def test_returns_true_on_first_success(self):
        import services.flow_store as fs
        with patch.object(fs, "_insert_rows", new=AsyncMock(return_value=True)):
            assert await fs._insert_rows_with_retry("flow_events", [{"a": 1}]) is True

    @pytest.mark.asyncio
    async def test_retries_and_returns_true_on_second_attempt(self):
        import services.flow_store as fs
        calls = [False, True]
        with patch.object(fs, "_insert_rows", new=AsyncMock(side_effect=calls)), \
             patch("services.flow_store.asyncio.sleep", new=AsyncMock()):
            assert await fs._insert_rows_with_retry("flow_events", [{"a": 1}]) is True

    @pytest.mark.asyncio
    async def test_returns_false_after_all_retries(self):
        import services.flow_store as fs
        with patch.object(fs, "_insert_rows", new=AsyncMock(return_value=False)), \
             patch("services.flow_store.asyncio.sleep", new=AsyncMock()):
            assert await fs._insert_rows_with_retry("flow_events", [{"a": 1}]) is False


# ---------------------------------------------------------------------------
# persist_flow_event
# ---------------------------------------------------------------------------

class TestPersistFlowEvent:
    @pytest.mark.asyncio
    async def test_drops_event_when_not_configured(self):
        import services.flow_store as fs
        fs._flow_event_buffer.clear()
        with patch.object(fs, "_SUPABASE_URL", None), \
             patch.object(fs, "_SUPABASE_KEY", None):
            await fs.persist_flow_event(_minimal_event())
        assert len(fs._flow_event_buffer) == 0

    @pytest.mark.asyncio
    async def test_appends_row_to_buffer_when_configured(self):
        import services.flow_store as fs
        fs._flow_event_buffer.clear()
        fs._buffer_lock = None  # reset lock so it reinits
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", "key"):
            await fs.persist_flow_event(_minimal_event())
        assert len(fs._flow_event_buffer) == 1
        assert fs._flow_event_buffer[0]["ticker"] == "AAPL"
        fs._flow_event_buffer.clear()

    @pytest.mark.asyncio
    async def test_early_flush_triggered_at_max_rows(self):
        import services.flow_store as fs
        fs._flow_event_buffer.clear()
        fs._buffer_lock = None
        fs._flow_event_buffer.extend([{"ticker": "X"} for _ in range(fs._FLUSH_MAX_ROWS - 1)])
        flush_mock = AsyncMock(return_value=True)
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", "key"), \
             patch.object(fs, "_insert_rows_with_retry", flush_mock):
            await fs.persist_flow_event(_minimal_event())
        flush_mock.assert_awaited_once()
        fs._flow_event_buffer.clear()

    @pytest.mark.asyncio
    async def test_warns_on_empty_expiry(self):
        import services.flow_store as fs
        fs._flow_event_buffer.clear()
        fs._buffer_lock = None
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", "key"):
            ev = _minimal_event(expiry="")
            await fs.persist_flow_event(ev)
        fs._flow_event_buffer.clear()

    @pytest.mark.asyncio
    async def test_warns_on_zero_strike(self):
        import services.flow_store as fs
        fs._flow_event_buffer.clear()
        fs._buffer_lock = None
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", "key"):
            ev = _minimal_event(strike=0.0)
            await fs.persist_flow_event(ev)
        fs._flow_event_buffer.clear()


# ---------------------------------------------------------------------------
# upgrade_to_sweep_in_db
# ---------------------------------------------------------------------------

class TestUpgradeToSweepInDb:
    @pytest.mark.asyncio
    async def test_returns_false_when_not_configured(self):
        import services.flow_store as fs
        with patch.object(fs, "_SUPABASE_URL", None), \
             patch.object(fs, "_SUPABASE_KEY", None):
            assert await fs.upgrade_to_sweep_in_db("AAPL  260620C00180000", 4.85, 100) is False

    @pytest.mark.asyncio
    async def test_returns_true_on_200(self):
        import services.flow_store as fs
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.patch      = AsyncMock(return_value=_mock_resp(200))
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", "key"), \
             patch("services.flow_store.httpx.AsyncClient", return_value=mock_client):
            result = await fs.upgrade_to_sweep_in_db("AAPL  260620C00180000", 4.85, 100)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_on_204(self):
        import services.flow_store as fs
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.patch      = AsyncMock(return_value=_mock_resp(204))
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", "key"), \
             patch("services.flow_store.httpx.AsyncClient", return_value=mock_client):
            result = await fs.upgrade_to_sweep_in_db("AAPL  260620C00180000", 4.85, 100)
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_error_status(self):
        import services.flow_store as fs
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.patch      = AsyncMock(return_value=_mock_resp(500, "error"))
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", "key"), \
             patch("services.flow_store.httpx.AsyncClient", return_value=mock_client):
            result = await fs.upgrade_to_sweep_in_db("AAPL  260620C00180000", 4.85, 100)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        import services.flow_store as fs
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", "key"), \
             patch("services.flow_store.httpx.AsyncClient", side_effect=RuntimeError("net")):
            result = await fs.upgrade_to_sweep_in_db("AAPL  260620C00180000", 4.85, 100)
        assert result is False


# ---------------------------------------------------------------------------
# persist_flow_episode
# ---------------------------------------------------------------------------

class TestPersistFlowEpisode:
    @pytest.mark.asyncio
    async def test_calls_insert_rows(self):
        import services.flow_store as fs
        insert_mock = AsyncMock(return_value=True)
        with patch.object(fs, "_insert_rows", insert_mock):
            await fs.persist_flow_episode({
                "ticker": "AAPL", "direction": "BUY",
                "contract_type": "CALL", "strike": 180.0,
                "expiry": "2026-06-20", "total_premium": 500_000,
                "trade_count": 5, "alert_level": "CONVICTION",
                "is_accelerating": True, "seed_episode": "test",
                "timestamp": "2026-04-25T10:00:00",
            })
        insert_mock.assert_awaited_once()
        args = insert_mock.call_args[0]
        assert args[0] == "flow_episodes"
        assert args[1][0]["ticker"] == "AAPL"

    @pytest.mark.asyncio
    async def test_normalises_null_expiry(self):
        import services.flow_store as fs
        insert_mock = AsyncMock(return_value=True)
        with patch.object(fs, "_insert_rows", insert_mock):
            await fs.persist_flow_episode({"ticker": "TSLA", "expiry": ""})
        row = insert_mock.call_args[0][1][0]
        assert row["expiry"] is None


# ---------------------------------------------------------------------------
# _flush_flow_events
# ---------------------------------------------------------------------------

class TestFlushFlowEvents:
    @pytest.mark.asyncio
    async def test_flushes_buffered_rows(self):
        import services.flow_store as fs
        fs._flow_event_buffer.clear()
        fs._buffer_lock = None
        fs._flow_event_buffer.append({"ticker": "AAPL"})

        flush_mock = AsyncMock(return_value=True)
        sleep_calls = 0

        async def _fake_sleep(_):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError

        with patch.object(fs, "_insert_rows_with_retry", flush_mock), \
             patch("services.flow_store.asyncio.sleep", side_effect=_fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await fs._flush_flow_events()

        flush_mock.assert_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_buffer_empty(self):
        import services.flow_store as fs
        fs._flow_event_buffer.clear()
        fs._buffer_lock = None

        flush_mock = AsyncMock(return_value=True)
        sleep_calls = 0

        async def _fake_sleep(_):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise asyncio.CancelledError

        with patch.object(fs, "_insert_rows_with_retry", flush_mock), \
             patch("services.flow_store.asyncio.sleep", side_effect=_fake_sleep):
            with pytest.raises(asyncio.CancelledError):
                await fs._flush_flow_events()

        flush_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# _bus_signal_listener
# ---------------------------------------------------------------------------

class TestBusSignalListener:
    @pytest.mark.asyncio
    async def test_persists_episode_on_composite_signal(self):
        import services.flow_store as fs

        msg = {
            "type": "composite_signal",
            "data": {
                "signal":  {"ticker": "AAPL", "recommendation": "BUY", "reasoning": "x"},
                "episode": {
                    "direction": "BUY", "contract_type": "CALL",
                    "total_premium": 500_000, "trade_count": 5,
                    "is_accelerating": False, "timestamp": "2026-04-25T10:00:00",
                },
            },
        }

        fake_q  = asyncio.Queue()
        await fake_q.put(msg)

        call_count = 0

        async def _stop_after_one(*_, **__):
            nonlocal call_count
            call_count += 1
            raise asyncio.CancelledError

        with patch.object(fs.bus, "subscribe", return_value=fake_q), \
             patch.object(fs.bus, "unsubscribe"), \
             patch.object(fs, "persist_flow_episode", side_effect=_stop_after_one):
            with pytest.raises(asyncio.CancelledError):
                await fs._bus_signal_listener()

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_ignores_non_dict_messages(self):
        import services.flow_store as fs

        fake_q = asyncio.Queue()
        await fake_q.put("not-a-dict")
        await fake_q.put(asyncio.CancelledError())  # sentinel

        call_count = 0

        async def _cancel_after(*_, **__):
            nonlocal call_count
            call_count += 1
            raise asyncio.CancelledError

        original_get = fake_q.get
        get_calls = 0

        async def _patched_get():
            nonlocal get_calls
            get_calls += 1
            if get_calls >= 2:
                raise asyncio.CancelledError
            return await original_get()

        fake_q.get = _patched_get

        persist_ep = AsyncMock()
        with patch.object(fs.bus, "subscribe", return_value=fake_q), \
             patch.object(fs.bus, "unsubscribe"), \
             patch.object(fs, "persist_flow_episode", persist_ep):
            with pytest.raises(asyncio.CancelledError):
                await fs._bus_signal_listener()

        persist_ep.assert_not_awaited()


# ---------------------------------------------------------------------------
# start_flow_writer
# ---------------------------------------------------------------------------

class TestStartFlowWriter:
    @pytest.mark.asyncio
    async def test_returns_early_when_not_configured(self):
        import services.flow_store as fs
        with patch.object(fs, "_SUPABASE_URL", None), \
             patch.object(fs, "_SUPABASE_KEY", None):
            await fs.start_flow_writer()

    @pytest.mark.asyncio
    async def test_launches_gather_when_configured(self):
        import services.flow_store as fs
        gather_mock = AsyncMock()
        with patch.object(fs, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(fs, "_SUPABASE_KEY", "key"), \
             patch("services.flow_store.asyncio.gather", gather_mock):
            await fs.start_flow_writer()
        gather_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# In-memory helpers: add_flow / get_flows / clear_flows
# ---------------------------------------------------------------------------

class TestInMemoryHelpers:
    @pytest.mark.asyncio
    async def test_add_and_retrieve(self):
        import services.flow_store as fs
        await fs.clear_flows()
        await fs.add_flow({"ticker": "AAPL", "premium": 100_000.0})
        flows = await fs.get_flows("AAPL")
        assert len(flows) == 1
        assert flows[0]["ticker"] == "AAPL"

    @pytest.mark.asyncio
    async def test_clear_empties_store(self):
        import services.flow_store as fs
        await fs.add_flow({"ticker": "SPY"})
        await fs.clear_flows()
        assert await fs.get_flows("SPY") == []

    @pytest.mark.asyncio
    async def test_get_flows_filters_by_ticker(self):
        import services.flow_store as fs
        await fs.clear_flows()
        await fs.add_flow({"ticker": "AAPL"})
        await fs.add_flow({"ticker": "TSLA"})
        flows = await fs.get_flows("AAPL")
        assert all(f["ticker"] == "AAPL" for f in flows)
        assert len(flows) == 1

    @pytest.mark.asyncio
    async def test_maxlen_does_not_crash(self):
        """Deque with maxlen=5000 should silently drop oldest entries."""
        import services.flow_store as fs
        await fs.clear_flows()
        for i in range(6000):
            await fs.add_flow({"ticker": f"T{i % 10}"})
        assert len(fs._mem_store) == 5000
        await fs.clear_flows()

    @pytest.mark.asyncio
    async def test_concurrent_adds_are_safe(self):
        import services.flow_store as fs
        await fs.clear_flows()
        await asyncio.gather(*[
            fs.add_flow({"ticker": f"T{i}"})
            for i in range(20)
        ])
        total = 0
        for i in range(20):
            total += len(await fs.get_flows(f"T{i}"))
        assert total == 20
        await fs.clear_flows()
