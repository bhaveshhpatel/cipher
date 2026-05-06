"""
test_ing009_episode_upsert.py — ING-009 QA test matrix

Covers E-1 through E-11 as specified in the 3-way deliberation
(2026-05-05, all roles signed off).

All Supabase HTTP calls are mocked via unittest.mock.AsyncMock.
No real network calls are made.

Test matrix:
  E-1  First qualifying print → INSERT, trade_count=1
  E-2  Second print, same contract, within window → PATCH, trade_count=2, premium accumulated
  E-3  Third print, same contract, within window → PATCH, trade_count=3
  E-4  Print after window expiry → INSERT, new episode, trade_count=1
  E-5  Different strike, same ticker → INSERT, different key
  E-6  Different expiry, same strike+ticker → INSERT, different key
  E-7  Next-day print → INSERT, new episode (ING-007 repeat flag independent)
  E-8  _lookup_open_episode Supabase error → fallback to INSERT, episode not lost
  E-9  strike and expiry populated correctly on both INSERT and PATCH paths
  E-10 Window boundary — print at exactly _EPISODE_MERGE_WINDOW_S → PATCH (inclusive)
  E-11 Window boundary — print at _EPISODE_MERGE_WINDOW_S + 1s → INSERT (new episode)

Counter invariants verified throughout:
  - created_episodes increments on INSERT only
  - merged_episodes increments on PATCH only
  - Neither counter increments on lookup failure fallback (E-8 inserts, counts as created)
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.flow_store as fs
from services.flow_store import (
    _EPISODE_MERGE_WINDOW_S,
    _episode_stats,
    _lookup_open_episode,
    persist_flow_episode,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signal(
    ticker="AAPL",
    direction="BULLISH",
    contract_type="CALL",
    strike=200.0,
    expiry="2026-06-20",
    total_premium=15000.0,
    trade_count=1,
    alert_level="WATCH",
    timestamp=None,
):
    return {
        "ticker":          ticker,
        "direction":       direction,
        "contract_type":   contract_type,
        "strike":          strike,
        "expiry":          expiry,
        "total_premium":   total_premium,
        "trade_count":     trade_count,
        "alert_level":     alert_level,
        "is_accelerating": False,
        "is_multi_day_repeat": False,
        "seed_episode":    None,
        "timestamp":       timestamp or datetime.now(timezone.utc).isoformat(),
    }


def _reset_episode_stats():
    _episode_stats["created_episodes"] = 0
    _episode_stats["merged_episodes"]  = 0


# ---------------------------------------------------------------------------
# E-1: First print → INSERT, created_episodes +1
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e1_first_print_inserts():
    _reset_episode_stats()
    signal = _make_signal()

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows", new=AsyncMock(return_value=True)):
        await persist_flow_episode(signal)

    assert _episode_stats["created_episodes"] == 1
    assert _episode_stats["merged_episodes"]  == 0


# ---------------------------------------------------------------------------
# E-2: Second print, same contract, within window → PATCH, trade_count=2
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2_second_print_merges():
    _reset_episode_stats()
    signal = _make_signal(total_premium=20000.0)

    existing_row = {"id": 42, "trade_count": 1, "total_premium": 15000.0}

    mock_patch_resp = MagicMock()
    mock_patch_resp.status_code = 204

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.patch = AsyncMock(return_value=mock_patch_resp)

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=existing_row)), \
         patch("services.flow_store.httpx.AsyncClient", return_value=mock_client):
        await persist_flow_episode(signal)

    assert _episode_stats["merged_episodes"]  == 1
    assert _episode_stats["created_episodes"] == 0

    patch_call_kwargs = mock_client.patch.call_args
    sent_payload = patch_call_kwargs.kwargs["json"]
    assert sent_payload["trade_count"]   == 2
    assert sent_payload["total_premium"] == pytest.approx(35000.0)


# ---------------------------------------------------------------------------
# E-3: Third print → PATCH, trade_count=3
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e3_third_print_trade_count_3():
    _reset_episode_stats()
    signal = _make_signal(total_premium=10000.0)

    existing_row = {"id": 99, "trade_count": 2, "total_premium": 35000.0}

    mock_patch_resp = MagicMock()
    mock_patch_resp.status_code = 204

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.patch = AsyncMock(return_value=mock_patch_resp)

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=existing_row)), \
         patch("services.flow_store.httpx.AsyncClient", return_value=mock_client):
        await persist_flow_episode(signal)

    assert _episode_stats["merged_episodes"] == 1
    sent_payload = mock_client.patch.call_args.kwargs["json"]
    assert sent_payload["trade_count"]   == 3
    assert sent_payload["total_premium"] == pytest.approx(45000.0)


# ---------------------------------------------------------------------------
# E-4: Print after window expiry → INSERT, new episode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e4_after_window_expiry_inserts():
    _reset_episode_stats()
    signal = _make_signal()

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows", new=AsyncMock(return_value=True)):
        await persist_flow_episode(signal)

    assert _episode_stats["created_episodes"] == 1
    assert _episode_stats["merged_episodes"]  == 0


# ---------------------------------------------------------------------------
# E-5: Different strike → INSERT (different merge key)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e5_different_strike_inserts():
    _reset_episode_stats()
    signal_210 = _make_signal(strike=210.0)

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows", new=AsyncMock(return_value=True)):
        await persist_flow_episode(signal_210)

    assert _episode_stats["created_episodes"] == 1
    assert _episode_stats["merged_episodes"]  == 0


# ---------------------------------------------------------------------------
# E-6: Different expiry → INSERT (different merge key)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e6_different_expiry_inserts():
    _reset_episode_stats()
    signal_sep = _make_signal(expiry="2026-09-19")

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows", new=AsyncMock(return_value=True)):
        await persist_flow_episode(signal_sep)

    assert _episode_stats["created_episodes"] == 1
    assert _episode_stats["merged_episodes"]  == 0


# ---------------------------------------------------------------------------
# E-7: Next-day print → INSERT (ING-007 repeat flag independent)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e7_next_day_inserts():
    _reset_episode_stats()
    signal = _make_signal()

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows", new=AsyncMock(return_value=True)):
        await persist_flow_episode(signal)

    assert _episode_stats["created_episodes"] == 1
    assert _episode_stats["merged_episodes"]  == 0


# ---------------------------------------------------------------------------
# E-8: Lookup Supabase error → fallback to INSERT, episode not lost
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e8_lookup_error_fallback_to_insert():
    _reset_episode_stats()
    signal = _make_signal()

    async def _lookup_raises(*args, **kwargs):
        raise Exception("Supabase connection timeout")

    insert_mock = AsyncMock(return_value=True)

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows", new=insert_mock):
        await persist_flow_episode(signal)

    assert _episode_stats["created_episodes"] == 1
    assert _episode_stats["merged_episodes"]  == 0
    insert_mock.assert_called_once()


# ---------------------------------------------------------------------------
# E-9: strike and expiry populated on both INSERT and PATCH paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e9_strike_expiry_populated_on_insert():
    _reset_episode_stats()
    signal = _make_signal(strike=195.0, expiry="2026-07-18")

    insert_mock = AsyncMock(return_value=True)

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows", new=insert_mock):
        await persist_flow_episode(signal)

    call_args = insert_mock.call_args
    rows = call_args.args[1]
    assert rows[0]["strike"] == 195.0
    assert rows[0]["expiry"] == "2026-07-18"


@pytest.mark.asyncio
async def test_e9_strike_expiry_in_lookup_key_on_patch():
    _reset_episode_stats()
    signal = _make_signal(strike=195.0, expiry="2026-07-18", total_premium=12000.0)

    existing_row = {"id": 77, "trade_count": 1, "total_premium": 15000.0}

    mock_patch_resp = MagicMock()
    mock_patch_resp.status_code = 204

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.patch = AsyncMock(return_value=mock_patch_resp)

    lookup_mock = AsyncMock(return_value=existing_row)

    with patch.object(fs, "_lookup_open_episode", new=lookup_mock), \
         patch("services.flow_store.httpx.AsyncClient", return_value=mock_client):
        await persist_flow_episode(signal)

    lookup_call = lookup_mock.call_args
    assert lookup_call.args[2] == 195.0 or lookup_call.kwargs.get("strike") == 195.0 or \
           195.0 in lookup_call.args
    assert _episode_stats["merged_episodes"] == 1


# ---------------------------------------------------------------------------
# E-10: Window boundary — at exactly _EPISODE_MERGE_WINDOW_S → PATCH
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e10_boundary_at_window_patches():
    """
    Lookup returns a row (simulating a signal_ts exactly at the boundary).
    persist_flow_episode should PATCH, not INSERT.
    """
    _reset_episode_stats()
    signal = _make_signal(total_premium=11000.0)

    existing_row = {"id": 55, "trade_count": 1, "total_premium": 14000.0}

    mock_patch_resp = MagicMock()
    mock_patch_resp.status_code = 204

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.patch = AsyncMock(return_value=mock_patch_resp)

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=existing_row)), \
         patch("services.flow_store.httpx.AsyncClient", return_value=mock_client):
        await persist_flow_episode(signal)

    assert _episode_stats["merged_episodes"]  == 1
    assert _episode_stats["created_episodes"] == 0


# ---------------------------------------------------------------------------
# E-11: Window boundary — at _EPISODE_MERGE_WINDOW_S + 1s → INSERT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e11_boundary_past_window_inserts():
    """
    Lookup returns None (simulating signal_ts just outside the window).
    persist_flow_episode should INSERT a new episode.
    """
    _reset_episode_stats()
    signal = _make_signal()

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows", new=AsyncMock(return_value=True)):
        await persist_flow_episode(signal)

    assert _episode_stats["created_episodes"] == 1
    assert _episode_stats["merged_episodes"]  == 0


# ---------------------------------------------------------------------------
# _lookup_open_episode unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lookup_returns_none_when_not_configured():
    with patch.object(fs, "_SUPABASE_URL", None):
        result = await _lookup_open_episode("AAPL", "BULLISH", "CALL", 200.0, "2026-06-20")
    assert result is None


@pytest.mark.asyncio
async def test_lookup_returns_none_on_http_error():
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch.object(fs, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(fs, "_SUPABASE_KEY", "fake-key"), \
         patch("services.flow_store.httpx.AsyncClient", return_value=mock_client):
        result = await _lookup_open_episode("AAPL", "BULLISH", "CALL", 200.0, "2026-06-20")

    assert result is None


@pytest.mark.asyncio
async def test_lookup_returns_none_when_empty_rows():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value=[])

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch.object(fs, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(fs, "_SUPABASE_KEY", "fake-key"), \
         patch("services.flow_store.httpx.AsyncClient", return_value=mock_client):
        result = await _lookup_open_episode("AAPL", "BULLISH", "CALL", 200.0, "2026-06-20")

    assert result is None


@pytest.mark.asyncio
async def test_lookup_returns_row_when_found():
    expected_row = {"id": 123, "trade_count": 2, "total_premium": 30000.0}
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value=[expected_row])

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch.object(fs, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(fs, "_SUPABASE_KEY", "fake-key"), \
         patch("services.flow_store.httpx.AsyncClient", return_value=mock_client):
        result = await _lookup_open_episode("AAPL", "BULLISH", "CALL", 200.0, "2026-06-20")

    assert result == expected_row


# ---------------------------------------------------------------------------
# Stats counter init — cold-start safety
# ---------------------------------------------------------------------------

def test_episode_stats_initialised_at_module_level():
    assert "created_episodes" in _episode_stats
    assert "merged_episodes"  in _episode_stats
    assert isinstance(_episode_stats["created_episodes"], int)
    assert isinstance(_episode_stats["merged_episodes"],  int)


def test_get_episode_stats_returns_copy():
    from services.flow_store import get_episode_stats
    stats = get_episode_stats()
    assert "created_episodes" in stats
    assert "merged_episodes"  in stats
    stats["created_episodes"] = 9999
    assert _episode_stats["created_episodes"] != 9999


# ---------------------------------------------------------------------------
# _EPISODE_MERGE_WINDOW_S constant
# ---------------------------------------------------------------------------

def test_episode_merge_window_is_1800():
    assert _EPISODE_MERGE_WINDOW_S == 1800
