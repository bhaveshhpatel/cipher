"""
Coverage boost for services/signal_store.py.
Targets: _insert_signal, _insert_signal_with_retry, persist_composite_signal,
         _bus_signal_listener, start_signal_writer, save_signal SDK path,
         get_signals SDK path.

Rearch-010 note: score-to-alert_level mapping is now REARCH vocab:
  score >= 0.85 or >= 0.70 -> BLOCK
  score >= 0.55            -> NOTEWORTHY
  score < 0.55             -> WATCH
  GOLDEN requires all Steamroom dimensions — cannot be score-derived.

Swarm columns (swarm_direction, swarm_confidence, swarm_agents,
swarm_bull_votes, swarm_bear_votes, swarm_hold_votes) were dropped in
migration 024 (rearch-010 pass 1) and are no longer written to
signal_history by _build_row().
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from services.signal_engine import EpisodeEvalResult
from services.signal_store import (
    _clear_signal_memory,
    _build_row,
    _coerce_to_dict,
    save_signal,
    get_signals,
    get_recent_signals,
    persist_composite_signal,
    start_signal_writer,
    _insert_signal,
    _insert_signal_with_retry,
)


def setup_function():
    _clear_signal_memory()


# --- _coerce_to_dict ---

def test_coerce_dict_passthrough():
    d = {"ticker": "AAPL"}
    assert _coerce_to_dict(d) is d

def test_coerce_object_with_dict():
    class Obj:
        def __init__(self): self.ticker = "TSLA"
    assert _coerce_to_dict(Obj())["ticker"] == "TSLA"

def test_coerce_pydantic_like():
    class PModel:
        def model_dump(self): return {"ticker": "NVDA"}
    assert _coerce_to_dict(PModel())["ticker"] == "NVDA"

def test_coerce_fallback_empty():
    class Bad:
        pass
    result = _coerce_to_dict(Bad())
    assert isinstance(result, dict)


# --- _build_row alert_level branches (rearch-010 REARCH vocab) ---
# CONVICTION/STRONG_SIGNAL/WHALE/etc. are legacy names bridged by
# _normalise_alert_level(); score branches emit WATCH/NOTEWORTHY/BLOCK.

def test_build_row_high_score_maps_to_block():
    """score >= 0.85 maps to BLOCK (rearch-010 — was CONVICTION)."""
    row = _build_row({"composite_score": 0.90, "ticker": "AAPL",
                      "recommendation": "BUY"})
    assert row["alert_level"] == "BLOCK"

def test_build_row_strong_signal_maps_to_block():
    """score >= 0.70 maps to BLOCK (rearch-010 — was WHALE/STRONG_SIGNAL)."""
    row = _build_row({"composite_score": 0.75, "ticker": "AAPL",
                      "recommendation": "BUY"})
    assert row["alert_level"] == "BLOCK"

def test_build_row_mid_score_maps_to_noteworthy():
    """score >= 0.55 maps to NOTEWORTHY (rearch-010 — was INSTITUTIONAL/ALERT)."""
    row = _build_row({"composite_score": 0.60, "ticker": "AAPL",
                      "recommendation": "BUY"})
    assert row["alert_level"] == "NOTEWORTHY"

def test_build_row_low_score_maps_to_watch():
    """score < 0.55 maps to WATCH (rearch-010 — was LARGE/WATCH)."""
    row = _build_row({"composite_score": 0.30, "ticker": "AAPL",
                      "recommendation": "HOLD"})
    assert row["alert_level"] == "WATCH"

def test_build_row_explicit_alert_level_wins():
    """
    Explicit alert_level in sig overrides score derivation.
    rearch-010: 'CONVICTION' is a legacy name — bridge maps it to 'BLOCK'.
    """
    row = _build_row({"composite_score": 0.10, "alert_level": "CONVICTION",
                      "ticker": "X", "recommendation": "HOLD"})
    assert row["alert_level"] == "BLOCK"

def test_build_row_explicit_rearch_alert_level_passthrough():
    """Valid REARCH vocab passed explicitly passes through unchanged."""
    row = _build_row({"composite_score": 0.10, "alert_level": "GOLDEN",
                      "ticker": "X", "recommendation": "HOLD"})
    assert row["alert_level"] == "GOLDEN"

def test_build_row_sentiment_from_ctype_call():
    row = _build_row({"composite_score": 0.5, "ticker": "AAPL",
                      "recommendation": "BUY"},
                     ep={"contract_type": "CALL", "direction": ""})
    assert row["sentiment"] == "BULLISH"

def test_build_row_sentiment_from_ctype_put():
    row = _build_row({"composite_score": 0.5, "ticker": "AAPL",
                      "recommendation": "SELL"},
                     ep={"contract_type": "PUT", "direction": ""})
    assert row["sentiment"] == "BEARISH"

def test_build_row_sentiment_explicit_wins():
    row = _build_row({"composite_score": 0.5, "ticker": "AAPL",
                      "recommendation": "BUY",
                      "sentiment": "NEUTRAL"})
    assert row["sentiment"] == "NEUTRAL"

def test_build_row_swarm_fields_not_in_row():
    """
    rearch-010: swarm columns dropped in migration 024.
    _build_row() must NOT write swarm_direction, swarm_bull_votes, etc.
    to the row — doing so would cause a 400 from Supabase REST (unknown column).
    """
    row = _build_row({"composite_score": 0.5, "ticker": "AAPL",
                      "recommendation": "BUY",
                      "swarm_direction": "BUY", "swarm_confidence": 0.8,
                      "swarm_bull_votes": 4, "swarm_bear_votes": 1, "swarm_hold_votes": 1,
                      "swarm_agents": [{"role": "A", "verdict": "BUY"}]})
    assert "swarm_direction"   not in row
    assert "swarm_bull_votes"  not in row
    assert "swarm_bear_votes"  not in row
    assert "swarm_hold_votes"  not in row
    assert "swarm_confidence"  not in row
    assert "swarm_agents"      not in row


# --- _insert_signal ---

def _make_resp(status, json_data=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = json_data or {}
    r.text = ""
    return r


def test_insert_signal_no_credentials():
    with patch("services.signal_store._SUPABASE_URL", None):
        result = asyncio.run(_insert_signal({"ticker": "AAPL"}))
    assert result is False


def test_insert_signal_200_returns_true():
    r = _make_resp(201)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=MagicMock(post=AsyncMock(return_value=r)))
    ctx.__aexit__  = AsyncMock(return_value=False)
    with patch("services.signal_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.signal_store._SUPABASE_KEY", "key"), \
         patch("services.signal_store.httpx.AsyncClient", return_value=ctx):
        result = asyncio.run(_insert_signal({"ticker": "AAPL"}))
    assert result is True


def test_insert_signal_non201_returns_false():
    r = _make_resp(500)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=MagicMock(post=AsyncMock(return_value=r)))
    ctx.__aexit__  = AsyncMock(return_value=False)
    with patch("services.signal_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.signal_store._SUPABASE_KEY", "key"), \
         patch("services.signal_store.httpx.AsyncClient", return_value=ctx):
        result = asyncio.run(_insert_signal({"ticker": "AAPL"}))
    assert result is False


def test_insert_signal_exception_returns_false():
    with patch("services.signal_store._SUPABASE_URL", "https://x.supabase.co"), \
         patch("services.signal_store._SUPABASE_KEY", "key"), \
         patch("services.signal_store.httpx.AsyncClient", side_effect=Exception("net")):
        result = asyncio.run(_insert_signal({"ticker": "AAPL"}))
    assert result is False


# --- _insert_signal_with_retry ---

def test_retry_succeeds_first_attempt():
    with patch("services.signal_store._insert_signal", new=AsyncMock(return_value=True)):
        result = asyncio.run(_insert_signal_with_retry({"ticker": "AAPL"}))
    assert result is True


def test_retry_exhausts_returns_false():
    with patch("services.signal_store._insert_signal", new=AsyncMock(return_value=False)), \
         patch("services.signal_store.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(_insert_signal_with_retry({"ticker": "AAPL"}))
    assert result is False


def test_retry_succeeds_on_second_attempt():
    call_count = [0]
    async def _mock_insert(row):
        call_count[0] += 1
        return call_count[0] >= 2
    with patch("services.signal_store._insert_signal", side_effect=_mock_insert), \
         patch("services.signal_store.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(_insert_signal_with_retry({"ticker": "AAPL"}))
    assert result is True


# --- save_signal: SDK mock path ---

def test_save_signal_sdk_mock_path():
    mock_client = MagicMock()
    mock_client.table.return_value.insert.return_value.execute.return_value = MagicMock()
    with patch("services.signal_store._client", return_value=mock_client):
        result = asyncio.run(save_signal({"ticker": "AAPL", "composite_score": 0.8}))
    assert result is True


def test_save_signal_sdk_insert_exception():
    mock_client = MagicMock()
    mock_client.table.side_effect = RuntimeError("db error")
    with patch("services.signal_store._client", return_value=mock_client):
        result = asyncio.run(save_signal({"ticker": "AAPL", "composite_score": 0.8}))
    assert result is False


def test_save_signal_no_credentials_stores_in_memory():
    with patch("services.signal_store._is_configured", return_value=False), \
         patch("services.signal_store._client", return_value=None):
        result = asyncio.run(save_signal({"id": "sig-1", "ticker": "TSLA"}))
    assert result is True
    signals = asyncio.run(get_signals())
    assert any(s.get("ticker") == "TSLA" for s in signals)


# --- get_signals: SDK path ---

def test_get_signals_sdk_path():
    mock_result = MagicMock()
    mock_result.data = [{"ticker": "AAPL", "id": 1}]
    mock_client = MagicMock()
    mock_client.table.return_value.select.return_value.order.return_value.limit.return_value.execute.return_value = mock_result
    with patch("services.signal_store._client", return_value=mock_client):
        rows = asyncio.run(get_signals(ticker="AAPL"))
    assert rows[0]["ticker"] == "AAPL"


def test_get_signals_sdk_exception_falls_through_to_memory():
    mock_client = MagicMock()
    mock_client.table.side_effect = RuntimeError("err")
    with patch("services.signal_store._client", return_value=mock_client):
        rows = asyncio.run(get_signals())
    assert isinstance(rows, list)


def test_get_recent_signals_alias():
    rows = asyncio.run(get_recent_signals())
    assert isinstance(rows, list)


# --- persist_composite_signal ---

def test_persist_composite_signal_not_configured_returns_early():
    with patch("services.signal_store._is_configured", return_value=False):
        asyncio.run(
            persist_composite_signal({"ticker": "AAPL", "composite_score": 0.9,
                                       "flow_score": 0.9, "recommendation": "BUY",
                                       "backtest_score": 0.8})
        )


def test_persist_composite_signal_insert_ok():
    with patch("services.signal_store._is_configured", return_value=True), \
         patch("services.signal_store._insert_signal_with_retry", new=AsyncMock(return_value=True)):
        asyncio.run(
            persist_composite_signal(
                {"ticker": "AAPL", "composite_score": 0.9, "flow_score": 0.9,
                 "recommendation": "BUY", "backtest_score": 0.8,
                 "swarm_direction": "BUY", "swarm_bull_votes": 5,
                 "swarm_bear_votes": 1, "swarm_hold_votes": 0,
                 "is_golden_sweep": True},
                ep={"total_premium": 500_000, "trade_count": 3,
                    "is_accelerating": True, "timestamp": "2026-04-26T12:00:00"}
            )
        )


def test_persist_composite_signal_insert_fail():
    with patch("services.signal_store._is_configured", return_value=True), \
         patch("services.signal_store._insert_signal_with_retry", new=AsyncMock(return_value=False)):
        asyncio.run(
            persist_composite_signal(
                {"ticker": "AAPL", "composite_score": 0.5, "flow_score": 0.5,
                 "recommendation": "HOLD", "backtest_score": 0.5}
            )
        )


# --- start_signal_writer ---

def test_start_signal_writer_not_configured_returns_early():
    with patch("services.signal_store._is_configured", return_value=False):
        asyncio.run(start_signal_writer())


def test_start_signal_writer_configured_cancels():
    async def _run():
        with patch("services.signal_store._is_configured", return_value=True), \
             patch("services.signal_store._bus_signal_listener",
                   new=AsyncMock(side_effect=asyncio.CancelledError)):
            try:
                await start_signal_writer()
            except asyncio.CancelledError:
                pass
    asyncio.run(_run())


# --- _bus_signal_listener ---

def test_bus_signal_listener_processes_composite_signal_and_cancels():
    """
    Verify _bus_signal_listener calls persist_composite_signal when the
    engine gate passes.

    REARCH-006: the listener now calls get_engine().evaluate_episode(ep)
    before any persist.  Without patching get_engine() the singleton would
    attempt real DB I/O and the sparse test episode would fail all gates
    (no notional_tier, ask_side_pct, dte_bucket, etc.), causing
    result.passed=False and persist to be skipped.

    Fix: patch get_engine to return a stub whose evaluate_episode always
    returns a passing EpisodeEvalResult.  E-18/E-19 in
    test_rearch006_signal_engine.py own the gate logic coverage — this test
    owns the persist-path wiring only.
    """
    from services import signal_store

    call_log = []

    async def _fake_persist(sig, ep, eval_result=None):
        call_log.append(sig)

    # Stub engine — always passes, alert=NOTEWORTHY
    _pass_result = EpisodeEvalResult(
        passed=True,
        alert_level="NOTEWORTHY",
        failing_dimensions=[],
        effective_threshold=50_000.0,
        premium=200_000.0,
        ticker="AAPL",
    )
    stub_engine = MagicMock()
    stub_engine.evaluate_episode.return_value = _pass_result

    async def _run():
        q = asyncio.Queue()
        await q.put({"type": "composite_signal", "data": {
            "signal":  {"ticker": "AAPL", "composite_score": 0.9},
            "episode": {"total_premium": 200_000},
        }})

        with patch.object(signal_store, "persist_composite_signal", side_effect=_fake_persist), \
             patch("services.signal_store.get_engine", return_value=stub_engine), \
             patch("services.signal_store.bus") as mock_bus:
            mock_bus.subscribe.return_value = q
            mock_bus.unsubscribe = MagicMock()

            async def _cancel_after_first():
                await asyncio.sleep(0.05)
                task.cancel()

            task = asyncio.create_task(signal_store._bus_signal_listener())
            asyncio.create_task(_cancel_after_first())
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())
    assert len(call_log) == 1
    assert call_log[0]["ticker"] == "AAPL"
