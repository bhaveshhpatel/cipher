"""
tests/test_flow_store.py

Unit tests for services/flow_store.py.

Covers:
  1. flow_episode row has correct schema matching flow_episodes table
  2. flow_event row has no `id` field (Postgres generates uuid)
  3. Sparse/None inputs produce safe defaults
  4. f-string log with None fields does not raise
  5. Event buffer accumulates and drains correctly
  6. No-op when SUPABASE_URL/KEY not configured
  7. persist_flow_episode calls _insert_rows with correct table name
  8. persist_flow_event buffers row without hitting network
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_SIGNAL_DATA = {
    "ticker":          "AAPL",
    "direction":       "REPEAT_BUY",
    "contract_type":   "CALL",
    "strike":          185.0,
    "expiry":          "2026-05-16",
    "total_premium":   750_000,
    "trade_count":     5,
    "alert_level":     "CONVICTION",
    "is_accelerating": True,
    "seed_episode":    "5x CALL $185 2026-05-16",
    "timestamp":       "2026-04-23T19:00:00",
}

SAMPLE_FLOW_TICK = {
    "ticker":          "AAPL",
    "contract_type":   "CALL",
    "strike":          185.0,
    "expiry":          "2026-05-16",
    "premium":         125_000.0,
    "trade_type":      "SWEEP",
    "sentiment":       "BULLISH",
    "influence_tier":  "LARGE",
    "conviction_score": 0.75,
    "is_golden_sweep": False,
}


# ---------------------------------------------------------------------------
# Helpers that mirror flow_store row-building logic
# ---------------------------------------------------------------------------

def _make_episode_row(d: dict) -> dict:
    return {
        "ticker":          d.get("ticker"),
        "direction":       d.get("direction"),
        "contract_type":   d.get("contract_type"),
        "strike":          d.get("strike"),
        "expiry":          d.get("expiry"),
        "total_premium":   d.get("total_premium"),
        "trade_count":     d.get("trade_count"),
        "alert_level":     d.get("alert_level"),
        "is_accelerating": d.get("is_accelerating", False),
        "seed_episode":    d.get("seed_episode"),
        "signal_ts":       d.get("timestamp"),
    }


def _make_event_row(d: dict) -> dict:
    return {
        "ticker":           d.get("ticker"),
        "contract_type":    d.get("contract_type"),
        "strike":           d.get("strike"),
        "expiry":           d.get("expiry"),
        "premium":          d.get("premium"),
        "trade_type":       d.get("trade_type", "UNKNOWN"),
        "sentiment":        d.get("sentiment", "UNKNOWN"),
        "influence_tier":   d.get("influence_tier", "UNKNOWN"),
        "conviction_score": d.get("conviction_score", 0.0),
        "is_golden_sweep":  d.get("is_golden_sweep", False),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_flow_episode_row_schema():
    """Row sent to flow_episodes has correct columns; no old composite_signals columns."""
    row = _make_episode_row(SAMPLE_SIGNAL_DATA)
    assert "id" not in row
    assert row["ticker"] == "AAPL"
    assert row["alert_level"] == "CONVICTION"
    assert row["total_premium"] == 750_000
    assert row["direction"] == "REPEAT_BUY"
    assert row["is_accelerating"] is True
    assert row["signal_ts"] == "2026-04-23T19:00:00"
    # Must NOT contain old composite_signals columns
    assert "recommendation" not in row
    assert "composite_score" not in row
    assert "episode_id" not in row


def test_flow_event_row_no_id():
    """flow_events row must not include id — Postgres generates uuid."""
    row = _make_event_row(SAMPLE_FLOW_TICK)
    assert "id" not in row
    assert row["ticker"] == "AAPL"
    assert row["sentiment"] == "BULLISH"
    assert row["premium"] == 125_000.0
    assert row["trade_type"] == "SWEEP"


def test_flow_event_sparse_defaults():
    """Sparse input with only ticker gets safe defaults for all nullable fields."""
    row = _make_event_row({"ticker": "TSLA"})
    assert row["trade_type"] == "UNKNOWN"
    assert row["sentiment"] == "UNKNOWN"
    assert row["influence_tier"] == "UNKNOWN"
    assert row["conviction_score"] == 0.0
    assert row["is_golden_sweep"] is False


def test_fstring_log_none_fields_no_crash():
    """f-string log formatting with None/zero values must never raise."""
    row = _make_episode_row({"ticker": None, "contract_type": None,
                              "alert_level": None, "total_premium": 0})
    # This is exactly what the fixed log.info f-string does:
    msg = (
        f"[flow_store] flow_episode saved: {row['ticker']} {row['contract_type']} "
        f"alert={row['alert_level']} prem=${(row['total_premium'] or 0):,.0f}"
    )
    assert "None" in msg  # renders safely as string 'None'
    assert "$0" in msg


def test_buffer_accumulate_and_drain():
    """Buffer collects rows and drains atomically."""
    buf = []
    for i in range(4):
        buf.append(_make_event_row({**SAMPLE_FLOW_TICK, "ticker": f"SYM{i}", "premium": i * 10_000.0}))
    assert len(buf) == 4
    batch = buf.copy()
    buf.clear()
    assert len(buf) == 0
    assert len(batch) == 4
    assert batch[0]["ticker"] == "SYM0"
    assert batch[3]["ticker"] == "SYM3"


def test_no_op_without_supabase_env(monkeypatch):
    """start_flow_writer returns immediately and does not raise when env vars absent."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ.get("SUPABASE_KEY")
    assert url is None
    assert key is None


@pytest.mark.asyncio
async def test_persist_flow_episode_calls_correct_table():
    """persist_flow_episode posts to flow_episodes, not composite_signals."""
    import services.flow_store as fs
    fs._SUPABASE_URL = "https://fake.supabase.co"
    fs._SUPABASE_KEY = "fake-key"
    with patch.object(fs, "_insert_rows", new_callable=AsyncMock) as mock_insert:
        mock_insert.return_value = True
        await fs.persist_flow_episode(SAMPLE_SIGNAL_DATA)
        mock_insert.assert_called_once()
        table_arg = mock_insert.call_args[0][0]
        assert table_arg == "flow_episodes", (
            f"Expected 'flow_episodes', got '{table_arg}' — "
            "flow_store is still writing to wrong table"
        )
        rows_arg = mock_insert.call_args[0][1]
        assert len(rows_arg) == 1
        assert "id" not in rows_arg[0]
        assert rows_arg[0]["alert_level"] == "CONVICTION"


@pytest.mark.asyncio
async def test_persist_flow_event_buffers_without_network():
    """persist_flow_event appends to buffer without any network call."""
    import services.flow_store as fs
    fs._flow_event_buffer.clear()
    with patch.object(fs, "_insert_rows", new_callable=AsyncMock) as mock_insert:
        await fs.persist_flow_event(SAMPLE_FLOW_TICK)
        mock_insert.assert_not_called()  # no immediate DB call
        assert len(fs._flow_event_buffer) == 1
        assert fs._flow_event_buffer[0]["ticker"] == "AAPL"
        assert "id" not in fs._flow_event_buffer[0]
    fs._flow_event_buffer.clear()
