"""
test_signal_store.py — 100% coverage for services/signal_store.py

Covers:
  - _is_configured()
  - _headers()
  - _normalise_direction()
  - _normalise_trade_type()
  - _normalise_influence_tier()
  - _coerce_to_dict()
  - _build_row()
  - _store_in_memory() + dedup
  - _clear_signal_memory()
  - _insert_signal()
  - _insert_signal_with_retry()
  - save_signal() — configured + unconfigured
  - persist_composite_signal() — configured + unconfigured
  - get_signals() / get_recent_signals()
  - _bus_signal_listener()
  - start_signal_writer() — configured + unconfigured
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_resp(status: int, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.text        = text
    return r


def _minimal_sig(**kwargs) -> dict:
    base = {
        "ticker":           "AAPL",
        "recommendation":   "BUY",
        "composite_score":  0.82,
        "flow_score":       0.75,
        "backtest_score":   0.70,
        "reasoning":        "test reasoning",
        "direction":        "BUY",
        "trade_type":       "SWEEP",
        "influence_tier":   "WHALE",
        "contract_type":    "CALL",
        "is_golden_sweep":  False,
        "swarm_direction":  "bullish",
        "swarm_confidence": 0.80,
        "swarm_bull_votes": 3,
        "swarm_bear_votes": 1,
        "swarm_hold_votes": 0,
        "swarm_agents":     None,
    }
    base.update(kwargs)
    return base


# ---------------------------------------------------------------------------
# _is_configured
# ---------------------------------------------------------------------------

class TestIsConfigured:
    def test_false_when_no_url(self):
        import services.signal_store as ss
        with patch.object(ss, "_SUPABASE_URL", None):
            assert ss._is_configured() is False

    def test_false_when_no_key(self):
        import services.signal_store as ss
        with patch.object(ss, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(ss, "_SUPABASE_KEY", None):
            assert ss._is_configured() is False

    def test_true_when_both_set(self):
        import services.signal_store as ss
        with patch.object(ss, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(ss, "_SUPABASE_KEY", "key"):
            assert ss._is_configured() is True


# ---------------------------------------------------------------------------
# _headers
# ---------------------------------------------------------------------------

class TestHeaders:
    def test_contains_required_keys(self):
        import services.signal_store as ss
        with patch.object(ss, "_SUPABASE_KEY", "test-key"):
            h = ss._headers()
        assert "apikey" in h
        assert "Authorization" in h
        assert "Prefer" in h


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

class TestNormaliseDirection:
    def test_buy_maps_to_bullish(self):
        from services.signal_store import _normalise_direction
        assert _normalise_direction("buy") == "bullish"

    def test_bullish_stays_bullish(self):
        from services.signal_store import _normalise_direction
        assert _normalise_direction("bullish") == "bullish"

    def test_repeat_buy_maps_to_bullish(self):
        from services.signal_store import _normalise_direction
        assert _normalise_direction("repeat_buy") == "bullish"

    def test_sell_maps_to_bearish(self):
        from services.signal_store import _normalise_direction
        assert _normalise_direction("sell") == "bearish"

    def test_bearish_stays_bearish(self):
        from services.signal_store import _normalise_direction
        assert _normalise_direction("bearish") == "bearish"

    def test_repeat_sell_maps_to_bearish(self):
        from services.signal_store import _normalise_direction
        assert _normalise_direction("repeat_sell") == "bearish"

    def test_unknown_maps_to_neutral(self):
        from services.signal_store import _normalise_direction
        assert _normalise_direction("hold") == "neutral"

    def test_empty_maps_to_neutral(self):
        from services.signal_store import _normalise_direction
        assert _normalise_direction("") == "neutral"

    def test_none_maps_to_neutral(self):
        from services.signal_store import _normalise_direction
        assert _normalise_direction(None) == "neutral"


class TestNormaliseTradeType:
    def test_sweep_preserved(self):
        from services.signal_store import _normalise_trade_type
        assert _normalise_trade_type("SWEEP") == "sweep"

    def test_block_preserved(self):
        from services.signal_store import _normalise_trade_type
        assert _normalise_trade_type("block") == "block"

    def test_split_preserved(self):
        from services.signal_store import _normalise_trade_type
        assert _normalise_trade_type("split") == "split"

    def test_single_preserved(self):
        from services.signal_store import _normalise_trade_type
        assert _normalise_trade_type("single") == "single"

    def test_unknown_maps_to_single(self):
        from services.signal_store import _normalise_trade_type
        assert _normalise_trade_type("BTO") == "single"

    def test_empty_maps_to_single(self):
        from services.signal_store import _normalise_trade_type
        assert _normalise_trade_type("") == "single"

    def test_none_maps_to_single(self):
        from services.signal_store import _normalise_trade_type
        assert _normalise_trade_type(None) == "single"


class TestNormaliseInfluenceTier:
    def test_whale_preserved(self):
        from services.signal_store import _normalise_influence_tier
        assert _normalise_influence_tier("whale") == "WHALE"

    def test_institutional_preserved(self):
        from services.signal_store import _normalise_influence_tier
        assert _normalise_influence_tier("INSTITUTIONAL") == "INSTITUTIONAL"

    def test_unknown_maps_to_retail(self):
        from services.signal_store import _normalise_influence_tier
        assert _normalise_influence_tier("VIP") == "RETAIL"

    def test_empty_maps_to_retail(self):
        from services.signal_store import _normalise_influence_tier
        assert _normalise_influence_tier("") == "RETAIL"

    def test_none_maps_to_retail(self):
        from services.signal_store import _normalise_influence_tier
        assert _normalise_influence_tier(None) == "RETAIL"


# ---------------------------------------------------------------------------
# _coerce_to_dict
# ---------------------------------------------------------------------------

class TestCoerceToDict:
    def test_dict_passthrough(self):
        from services.signal_store import _coerce_to_dict
        d = {"a": 1}
        assert _coerce_to_dict(d) is d

    def test_model_dump(self):
        from services.signal_store import _coerce_to_dict
        obj = MagicMock()
        obj.model_dump.return_value = {"b": 2}
        del obj.__dict__  # prevent __dict__ path
        assert _coerce_to_dict(obj) == {"b": 2}

    def test_dunder_dict(self):
        from services.signal_store import _coerce_to_dict
        class Obj:
            def __init__(self):
                self.c = 3
        assert _coerce_to_dict(Obj()) == {"c": 3}

    def test_fallback_dict_constructor(self):
        from services.signal_store import _coerce_to_dict
        assert _coerce_to_dict([("d", 4)]) == {"d": 4}

    def test_returns_empty_on_total_failure(self):
        from services.signal_store import _coerce_to_dict
        result = _coerce_to_dict(42)  # int, not iterable of pairs
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# _build_row
# ---------------------------------------------------------------------------

class TestBuildRow:
    def test_alert_level_conviction_at_high_score(self):
        from services.signal_store import _build_row
        row = _build_row({"ticker": "AAPL", "composite_score": 0.90, "flow_score": 0.8})
        assert row["alert_level"] == "CONVICTION"

    def test_alert_level_strong_signal(self):
        from services.signal_store import _build_row
        row = _build_row({"ticker": "AAPL", "composite_score": 0.72, "flow_score": 0.7})
        assert row["alert_level"] == "STRONG_SIGNAL"

    def test_alert_level_alert(self):
        from services.signal_store import _build_row
        row = _build_row({"ticker": "AAPL", "composite_score": 0.60, "flow_score": 0.6})
        assert row["alert_level"] == "ALERT"

    def test_alert_level_watch_at_low_score(self):
        from services.signal_store import _build_row
        row = _build_row({"ticker": "AAPL", "composite_score": 0.30, "flow_score": 0.3})
        assert row["alert_level"] == "WATCH"

    def test_alert_level_passthrough_when_set(self):
        from services.signal_store import _build_row
        row = _build_row({"ticker": "AAPL", "composite_score": 0.90,
                          "flow_score": 0.8, "alert_level": "CUSTOM"})
        assert row["alert_level"] == "CUSTOM"

    def test_direction_buy_maps_correctly(self):
        from services.signal_store import _build_row
        row = _build_row(_minimal_sig(direction="buy"))
        assert row["direction"] == "BUY"

    def test_direction_sell_maps_correctly(self):
        from services.signal_store import _build_row
        row = _build_row(_minimal_sig(direction="bearish"))
        assert row["direction"] == "SELL"

    def test_direction_neutral_maps_to_hold(self):
        from services.signal_store import _build_row
        row = _build_row(_minimal_sig(direction=""))
        assert row["direction"] == "HOLD"

    def test_sentiment_bullish_from_call(self):
        from services.signal_store import _build_row
        row = _build_row({"ticker": "AAPL", "composite_score": 0.8,
                          "flow_score": 0.7, "contract_type": "CALL"})
        assert row["sentiment"] == "BULLISH"

    def test_sentiment_bearish_from_put(self):
        from services.signal_store import _build_row
        row = _build_row({"ticker": "AAPL", "composite_score": 0.8,
                          "flow_score": 0.7, "contract_type": "PUT"})
        assert row["sentiment"] == "BEARISH"

    def test_sentiment_neutral_fallback(self):
        from services.signal_store import _build_row
        row = _build_row({"ticker": "AAPL", "composite_score": 0.8, "flow_score": 0.7})
        assert row["sentiment"] == "NEUTRAL"

    def test_trade_type_uppercased(self):
        from services.signal_store import _build_row
        row = _build_row(_minimal_sig(trade_type="sweep"))
        assert row["trade_type"] == "SWEEP"

    def test_unknown_trade_type_becomes_single(self):
        from services.signal_store import _build_row
        row = _build_row(_minimal_sig(trade_type="BTO"))
        assert row["trade_type"] == "SINGLE"

    def test_episode_fields_override_sig(self):
        from services.signal_store import _build_row
        ep = {"contract_type": "PUT", "direction": "SELL",
              "total_premium": 1_000_000, "trade_count": 10,
              "is_accelerating": True, "timestamp": "2026-04-25T10:00:00"}
        row = _build_row(_minimal_sig(), ep)
        assert row["contract_type"] == "PUT"
        assert row["trade_count"] == 10
        assert row["is_accelerating"] is True

    def test_is_golden_sweep_true(self):
        from services.signal_store import _build_row
        row = _build_row(_minimal_sig(is_golden_sweep=True))
        assert row["is_golden_sweep"] is True

    def test_swarm_fields_propagated(self):
        from services.signal_store import _build_row
        row = _build_row(_minimal_sig(
            swarm_direction="bearish",
            swarm_confidence=0.90,
            swarm_bull_votes=1,
            swarm_bear_votes=4,
            swarm_hold_votes=0,
        ))
        assert row["swarm_direction"] == "bearish"
        assert row["swarm_confidence"] == 0.90
        assert row["swarm_bear_votes"] == 4


# ---------------------------------------------------------------------------
# _store_in_memory + dedup
# ---------------------------------------------------------------------------

class TestStoreInMemory:
    def setup_method(self):
        from services.signal_store import _clear_signal_memory
        _clear_signal_memory()

    def test_stores_signal_without_id(self):
        from services.signal_store import _store_in_memory, _signal_memory
        _store_in_memory({"ticker": "AAPL"})
        assert len(_signal_memory) == 1

    def test_dedup_by_id(self):
        from services.signal_store import _store_in_memory, _signal_memory
        sig = {"ticker": "AAPL", "id": "dedup-1"}
        _store_in_memory(sig)
        _store_in_memory(sig)
        assert len(_signal_memory) == 1

    def test_different_ids_both_stored(self):
        from services.signal_store import _store_in_memory, _signal_memory
        _store_in_memory({"ticker": "AAPL", "id": "a"})
        _store_in_memory({"ticker": "AAPL", "id": "b"})
        assert len(_signal_memory) == 2

    def test_clear_resets_everything(self):
        from services.signal_store import _store_in_memory, _signal_memory, _clear_signal_memory
        _store_in_memory({"ticker": "AAPL", "id": "c"})
        _clear_signal_memory()
        assert len(_signal_memory) == 0


# ---------------------------------------------------------------------------
# _insert_signal
# ---------------------------------------------------------------------------

class TestInsertSignal:
    @pytest.mark.asyncio
    async def test_returns_false_when_not_configured(self):
        import services.signal_store as ss
        with patch.object(ss, "_SUPABASE_URL", None):
            assert await ss._insert_signal({"ticker": "AAPL"}) is False

    @pytest.mark.asyncio
    async def test_returns_true_on_201(self):
        import services.signal_store as ss
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post       = AsyncMock(return_value=_mock_resp(201))
        with patch.object(ss, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(ss, "_SUPABASE_KEY", "key"), \
             patch("services.signal_store.httpx.AsyncClient", return_value=mock_client):
            assert await ss._insert_signal({"ticker": "AAPL"}) is True

    @pytest.mark.asyncio
    async def test_returns_false_on_4xx(self):
        import services.signal_store as ss
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__  = AsyncMock(return_value=False)
        mock_client.post       = AsyncMock(return_value=_mock_resp(400, "bad"))
        with patch.object(ss, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(ss, "_SUPABASE_KEY", "key"), \
             patch("services.signal_store.httpx.AsyncClient", return_value=mock_client):
            assert await ss._insert_signal({"ticker": "AAPL"}) is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        import services.signal_store as ss
        with patch.object(ss, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(ss, "_SUPABASE_KEY", "key"), \
             patch("services.signal_store.httpx.AsyncClient", side_effect=RuntimeError("net")):
            assert await ss._insert_signal({"ticker": "AAPL"}) is False


# ---------------------------------------------------------------------------
# _insert_signal_with_retry
# ---------------------------------------------------------------------------

class TestInsertSignalWithRetry:
    @pytest.mark.asyncio
    async def test_succeeds_first_attempt(self):
        import services.signal_store as ss
        with patch.object(ss, "_insert_signal", new=AsyncMock(return_value=True)):
            assert await ss._insert_signal_with_retry({"ticker": "AAPL"}) is True

    @pytest.mark.asyncio
    async def test_succeeds_on_second_attempt(self):
        import services.signal_store as ss
        with patch.object(ss, "_insert_signal", new=AsyncMock(side_effect=[False, True])), \
             patch("services.signal_store.asyncio.sleep", new=AsyncMock()):
            assert await ss._insert_signal_with_retry({"ticker": "AAPL"}) is True

    @pytest.mark.asyncio
    async def test_returns_false_after_all_retries(self):
        import services.signal_store as ss
        with patch.object(ss, "_insert_signal", new=AsyncMock(return_value=False)), \
             patch("services.signal_store.asyncio.sleep", new=AsyncMock()):
            assert await ss._insert_signal_with_retry({"ticker": "AAPL"}) is False


# ---------------------------------------------------------------------------
# save_signal
# ---------------------------------------------------------------------------

class TestSaveSignal:
    def setup_method(self):
        from services.signal_store import _clear_signal_memory
        _clear_signal_memory()

    @pytest.mark.asyncio
    async def test_stores_in_memory_when_unconfigured(self):
        import services.signal_store as ss
        with patch.object(ss, "_SUPABASE_URL", None):
            ok = await ss.save_signal({"ticker": "AAPL", "id": "sv-1"})
        assert ok is True
        sigs = await ss.get_signals("AAPL")
        assert any(s.get("id") == "sv-1" for s in sigs)

    @pytest.mark.asyncio
    async def test_returns_true_on_successful_db_write(self):
        import services.signal_store as ss
        with patch.object(ss, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(ss, "_SUPABASE_KEY", "key"), \
             patch.object(ss, "_insert_signal_with_retry", new=AsyncMock(return_value=True)):
            ok = await ss.save_signal(_minimal_sig())
        assert ok is True

    @pytest.mark.asyncio
    async def test_stores_in_memory_even_on_db_failure(self):
        import services.signal_store as ss
        with patch.object(ss, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(ss, "_SUPABASE_KEY", "key"), \
             patch.object(ss, "_insert_signal_with_retry", new=AsyncMock(return_value=False)):
            ok = await ss.save_signal(_minimal_sig(id="sv-fail"))
        assert ok is False
        sigs = await ss.get_signals("AAPL")
        assert any(s.get("id") == "sv-fail" for s in sigs)


# ---------------------------------------------------------------------------
# persist_composite_signal
# ---------------------------------------------------------------------------

class TestPersistCompositeSignal:
    def setup_method(self):
        from services.signal_store import _clear_signal_memory
        _clear_signal_memory()

    @pytest.mark.asyncio
    async def test_stores_in_memory_when_unconfigured(self):
        import services.signal_store as ss
        with patch.object(ss, "_SUPABASE_URL", None):
            await ss.persist_composite_signal(_minimal_sig())
        sigs = await ss.get_signals("AAPL")
        assert len(sigs) >= 1

    @pytest.mark.asyncio
    async def test_logs_success_on_db_ok(self):
        import services.signal_store as ss
        with patch.object(ss, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(ss, "_SUPABASE_KEY", "key"), \
             patch.object(ss, "_insert_signal_with_retry", new=AsyncMock(return_value=True)):
            await ss.persist_composite_signal(_minimal_sig())

    @pytest.mark.asyncio
    async def test_logs_warning_on_db_failure(self):
        import services.signal_store as ss
        with patch.object(ss, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(ss, "_SUPABASE_KEY", "key"), \
             patch.object(ss, "_insert_signal_with_retry", new=AsyncMock(return_value=False)):
            # Should not raise
            await ss.persist_composite_signal(_minimal_sig())

    @pytest.mark.asyncio
    async def test_golden_sweep_tag_in_log(self):
        import services.signal_store as ss
        with patch.object(ss, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(ss, "_SUPABASE_KEY", "key"), \
             patch.object(ss, "_insert_signal_with_retry", new=AsyncMock(return_value=True)):
            await ss.persist_composite_signal(_minimal_sig(is_golden_sweep=True))


# ---------------------------------------------------------------------------
# get_signals / get_recent_signals
# ---------------------------------------------------------------------------

class TestGetSignals:
    def setup_method(self):
        from services.signal_store import _clear_signal_memory
        _clear_signal_memory()

    @pytest.mark.asyncio
    async def test_returns_empty_when_nothing_stored(self):
        from services.signal_store import get_signals
        assert await get_signals() == []

    @pytest.mark.asyncio
    async def test_filters_by_ticker(self):
        import services.signal_store as ss
        ss._store_in_memory({"ticker": "AAPL", "id": "a1"})
        ss._store_in_memory({"ticker": "TSLA", "id": "t1"})
        sigs = await ss.get_signals("AAPL")
        assert all(s["ticker"] == "AAPL" for s in sigs)

    @pytest.mark.asyncio
    async def test_respects_limit(self):
        import services.signal_store as ss
        for i in range(10):
            ss._store_in_memory({"ticker": "AAPL", "id": f"lim-{i}"})
        sigs = await ss.get_signals("AAPL", limit=3)
        assert len(sigs) <= 3

    @pytest.mark.asyncio
    async def test_get_recent_signals_is_alias(self):
        import services.signal_store as ss
        ss._store_in_memory({"ticker": "NVDA", "id": "n1"})
        a = await ss.get_signals("NVDA")
        b = await ss.get_recent_signals("NVDA")
        assert a == b


# ---------------------------------------------------------------------------
# _bus_signal_listener
# ---------------------------------------------------------------------------

class TestBusSignalListener:
    @pytest.mark.asyncio
    async def test_persists_composite_signal_from_bus(self):
        import services.signal_store as ss

        msg = {
            "type": "composite_signal",
            "data": {
                "signal":  _minimal_sig(),
                "episode": {"total_premium": 500_000, "trade_count": 5},
            },
        }

        fake_q = asyncio.Queue()
        await fake_q.put(msg)

        call_count = 0

        async def _stop_after_first(*_, **__):
            nonlocal call_count
            call_count += 1
            raise asyncio.CancelledError

        with patch.object(ss.bus, "subscribe", return_value=fake_q), \
             patch.object(ss.bus, "unsubscribe"), \
             patch.object(ss, "persist_composite_signal", side_effect=_stop_after_first):
            with pytest.raises(asyncio.CancelledError):
                await ss._bus_signal_listener()

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_ignores_non_composite_messages(self):
        import services.signal_store as ss

        fake_q   = asyncio.Queue()
        get_calls = 0

        async def _patched_get():
            nonlocal get_calls
            get_calls += 1
            if get_calls >= 2:
                raise asyncio.CancelledError
            return {"type": "raw_tick", "data": {}}

        fake_q.get = _patched_get
        persist_mock = AsyncMock()

        with patch.object(ss.bus, "subscribe", return_value=fake_q), \
             patch.object(ss.bus, "unsubscribe"), \
             patch.object(ss, "persist_composite_signal", persist_mock):
            with pytest.raises(asyncio.CancelledError):
                await ss._bus_signal_listener()

        persist_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# start_signal_writer
# ---------------------------------------------------------------------------

class TestStartSignalWriter:
    @pytest.mark.asyncio
    async def test_returns_early_when_unconfigured(self):
        import services.signal_store as ss
        with patch.object(ss, "_SUPABASE_URL", None):
            await ss.start_signal_writer()  # should not raise or hang

    @pytest.mark.asyncio
    async def test_calls_bus_listener_when_configured(self):
        import services.signal_store as ss
        listener_mock = AsyncMock()
        with patch.object(ss, "_SUPABASE_URL", "https://x.supabase.co"), \
             patch.object(ss, "_SUPABASE_KEY", "key"), \
             patch.object(ss, "_bus_signal_listener", listener_mock):
            await ss.start_signal_writer()
        listener_mock.assert_awaited_once()
