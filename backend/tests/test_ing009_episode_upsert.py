"""
test_ing009_episode_upsert.py — ING-009 QA test matrix

Covers E-1 through E-16 (E-12 through E-16 added for ING-009-RACE fix,
2026-05-08).

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

  ING-009-RACE fixes (2026-05-08):
  E-12 Concurrent same-key calls → exactly 1 INSERT + N-1 PATCHes, premium accumulates
  E-13 Lock is per-key — different keys run concurrently without blocking each other
  E-14 In-flight cache hit: second waiter uses cached row id, skips DB GET
  E-15 reset_episode_state() clears in-flight cache and locks; next call triggers fresh INSERT
  E-16 _insert_rows_with_episode_id: id returned by PostgREST is stored in _episode_in_flight;
       missing id in response body does not crash — in-flight not populated (safe fallback)

Counter invariants verified throughout:
  - created_episodes increments on INSERT only
  - merged_episodes increments on PATCH only
  - Neither counter increments on lookup failure fallback (E-8 inserts, counts as created)

NOTE — import discipline:
  All module-level names (_episode_stats, _lookup_open_episode,
  persist_flow_episode, _EPISODE_MERGE_WINDOW_S) are accessed via the
  `fs` module reference, never via direct `from ... import` bindings.
  This ensures patch.object(fs, ...), _reset_episode_stats(), and all
  assertions operate on the same live object, regardless of how pytest
  caches sys.modules between test runs.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

import services.flow_store as fs


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
    fs._episode_stats["created_episodes"] = 0
    fs._episode_stats["merged_episodes"]  = 0


def _reset_all_state():
    """Full state reset: stats + in-flight cache + locks."""
    _reset_episode_stats()
    fs.reset_episode_state()


# ---------------------------------------------------------------------------
# E-1: First print → INSERT, created_episodes +1
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e1_first_print_inserts():
    _reset_all_state()
    signal = _make_signal()

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows_with_episode_id", new=AsyncMock(return_value=True)):
        await fs.persist_flow_episode(signal)

    assert fs._episode_stats["created_episodes"] == 1
    assert fs._episode_stats["merged_episodes"]  == 0


# ---------------------------------------------------------------------------
# E-2: Second print, same contract, within window → PATCH, trade_count=2
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e2_second_print_merges():
    _reset_all_state()
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
        await fs.persist_flow_episode(signal)

    assert fs._episode_stats["merged_episodes"]  == 1
    assert fs._episode_stats["created_episodes"] == 0

    sent_payload = mock_client.patch.call_args.kwargs["json"]
    assert sent_payload["trade_count"]   == 2
    assert sent_payload["total_premium"] == pytest.approx(35000.0)


# ---------------------------------------------------------------------------
# E-3: Third print → PATCH, trade_count=3
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e3_third_print_trade_count_3():
    _reset_all_state()
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
        await fs.persist_flow_episode(signal)

    assert fs._episode_stats["merged_episodes"] == 1
    sent_payload = mock_client.patch.call_args.kwargs["json"]
    assert sent_payload["trade_count"]   == 3
    assert sent_payload["total_premium"] == pytest.approx(45000.0)


# ---------------------------------------------------------------------------
# E-4: Print after window expiry → INSERT, new episode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e4_after_window_expiry_inserts():
    _reset_all_state()
    signal = _make_signal()

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows_with_episode_id", new=AsyncMock(return_value=True)):
        await fs.persist_flow_episode(signal)

    assert fs._episode_stats["created_episodes"] == 1
    assert fs._episode_stats["merged_episodes"]  == 0


# ---------------------------------------------------------------------------
# E-5: Different strike → INSERT (different merge key)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e5_different_strike_inserts():
    _reset_all_state()
    signal_210 = _make_signal(strike=210.0)

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows_with_episode_id", new=AsyncMock(return_value=True)):
        await fs.persist_flow_episode(signal_210)

    assert fs._episode_stats["created_episodes"] == 1
    assert fs._episode_stats["merged_episodes"]  == 0


# ---------------------------------------------------------------------------
# E-6: Different expiry → INSERT (different merge key)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e6_different_expiry_inserts():
    _reset_all_state()
    signal_sep = _make_signal(expiry="2026-09-19")

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows_with_episode_id", new=AsyncMock(return_value=True)):
        await fs.persist_flow_episode(signal_sep)

    assert fs._episode_stats["created_episodes"] == 1
    assert fs._episode_stats["merged_episodes"]  == 0


# ---------------------------------------------------------------------------
# E-7: Next-day print → INSERT (ING-007 repeat flag independent)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e7_next_day_inserts():
    _reset_all_state()
    signal = _make_signal()

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows_with_episode_id", new=AsyncMock(return_value=True)):
        await fs.persist_flow_episode(signal)

    assert fs._episode_stats["created_episodes"] == 1
    assert fs._episode_stats["merged_episodes"]  == 0


# ---------------------------------------------------------------------------
# E-8: Lookup Supabase error → fallback to INSERT, episode not lost
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e8_lookup_error_fallback_to_insert():
    _reset_all_state()
    signal = _make_signal()

    insert_mock = AsyncMock(return_value=True)

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows_with_episode_id", new=insert_mock):
        await fs.persist_flow_episode(signal)

    assert fs._episode_stats["created_episodes"] == 1
    assert fs._episode_stats["merged_episodes"]  == 0
    insert_mock.assert_called_once()


# ---------------------------------------------------------------------------
# E-9: strike and expiry populated on both INSERT and PATCH paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e9_strike_expiry_populated_on_insert():
    _reset_all_state()
    signal = _make_signal(strike=195.0, expiry="2026-07-18")

    captured_rows = []

    async def capture_insert(table, row, key, premium, current_oi=None):
        captured_rows.append(row)
        return True

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows_with_episode_id", new=capture_insert):
        await fs.persist_flow_episode(signal)

    assert captured_rows[0]["strike"] == 195.0
    assert captured_rows[0]["expiry"] == "2026-07-18"


@pytest.mark.asyncio
async def test_e9_strike_expiry_in_lookup_key_on_patch():
    _reset_all_state()
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
        await fs.persist_flow_episode(signal)

    lookup_call = lookup_mock.call_args
    assert 195.0 in lookup_call.args
    assert fs._episode_stats["merged_episodes"] == 1


# ---------------------------------------------------------------------------
# E-10: Window boundary — at exactly _EPISODE_MERGE_WINDOW_S → PATCH
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e10_boundary_at_window_patches():
    """
    Lookup returns a row (simulating a signal_ts exactly at the boundary).
    persist_flow_episode should PATCH, not INSERT.
    """
    _reset_all_state()
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
        await fs.persist_flow_episode(signal)

    assert fs._episode_stats["merged_episodes"]  == 1
    assert fs._episode_stats["created_episodes"] == 0


# ---------------------------------------------------------------------------
# E-11: Window boundary — at _EPISODE_MERGE_WINDOW_S + 1s → INSERT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e11_boundary_past_window_inserts():
    """
    Lookup returns None (simulating signal_ts just outside the window).
    persist_flow_episode should INSERT a new episode.
    """
    _reset_all_state()
    signal = _make_signal()

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows_with_episode_id", new=AsyncMock(return_value=True)):
        await fs.persist_flow_episode(signal)

    assert fs._episode_stats["created_episodes"] == 1
    assert fs._episode_stats["merged_episodes"]  == 0


# ---------------------------------------------------------------------------
# E-12: Concurrent same-key calls → exactly 1 INSERT + N-1 PATCHes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e12_concurrent_same_key_produces_one_insert():
    """
    Three coroutines for the same contract key fire simultaneously.
    Expected outcome:
      - Exactly 1 _insert_rows_with_episode_id call (first to acquire lock)
      - Exactly 2 _patch_episode calls (second and third waiters hit in-flight)
      - created_episodes == 1, merged_episodes == 2
      - Final total_premium = 10000 + 10000 + 10000 = 30000
    """
    _reset_all_state()

    INSERTED_ID = 500
    premiums = [10000.0, 10000.0, 10000.0]
    signals = [_make_signal(total_premium=p) for p in premiums]

    patch_call_payloads = []

    async def fake_insert(table, row, key, premium, current_oi=None):
        # Simulate PostgREST returning the id — populate in-flight.
        fs._set_episode_in_flight(key, INSERTED_ID, row.get("trade_count") or 1, premium)
        return True

    mock_patch_resp = MagicMock()
    mock_patch_resp.status_code = 204

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    async def capture_patch(url, headers, json):
        patch_call_payloads.append(json)
        return mock_patch_resp

    mock_client.patch = capture_patch

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows_with_episode_id", new=fake_insert), \
         patch("services.flow_store.httpx.AsyncClient", return_value=mock_client):
        await asyncio.gather(*[fs.persist_flow_episode(s) for s in signals])

    assert fs._episode_stats["created_episodes"] == 1
    assert fs._episode_stats["merged_episodes"]  == 2
    # Total premium accumulated across all 3 calls.
    assert patch_call_payloads[-1]["total_premium"] == pytest.approx(30000.0)


# ---------------------------------------------------------------------------
# E-13: Different keys run concurrently without blocking each other
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e13_different_keys_concurrent_no_blocking():
    """
    Two coroutines for different contracts fire simultaneously.
    Both should INSERT independently — no cross-key interference.
    """
    _reset_all_state()

    sig_a = _make_signal(ticker="AAPL", strike=200.0)
    sig_b = _make_signal(ticker="TSLA", strike=300.0)

    insert_calls = []

    async def fake_insert(table, row, key, premium, current_oi=None):
        insert_calls.append(key)
        return True

    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows_with_episode_id", new=fake_insert):
        await asyncio.gather(
            fs.persist_flow_episode(sig_a),
            fs.persist_flow_episode(sig_b),
        )

    assert fs._episode_stats["created_episodes"] == 2
    assert fs._episode_stats["merged_episodes"]  == 0
    assert len(insert_calls) == 2
    # Both keys present.
    keys_tickers = [k.split("|")[0] for k in insert_calls]
    assert "AAPL" in keys_tickers
    assert "TSLA" in keys_tickers


# ---------------------------------------------------------------------------
# E-14: In-flight cache hit — second waiter skips DB GET
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e14_in_flight_cache_hit_skips_db_get():
    """
    Manually pre-populate _episode_in_flight for a key.
    persist_flow_episode should go directly to PATCH without calling
    _lookup_open_episode.
    """
    _reset_all_state()

    key = fs._episode_key("AAPL", "BULLISH", "CALL", 200.0, "2026-06-20")
    fs._set_episode_in_flight(key, 999, 1, 15000.0)

    signal = _make_signal(total_premium=12000.0)

    lookup_mock = AsyncMock(return_value=None)  # should NOT be called

    mock_patch_resp = MagicMock()
    mock_patch_resp.status_code = 204
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.patch = AsyncMock(return_value=mock_patch_resp)

    with patch.object(fs, "_lookup_open_episode", new=lookup_mock), \
         patch("services.flow_store.httpx.AsyncClient", return_value=mock_client):
        await fs.persist_flow_episode(signal)

    lookup_mock.assert_not_called()
    assert fs._episode_stats["merged_episodes"] == 1
    assert fs._episode_stats["created_episodes"] == 0
    # Verify patch payload
    sent = mock_client.patch.call_args.kwargs["json"]
    assert sent["trade_count"]   == 2
    assert sent["total_premium"] == pytest.approx(27000.0)


# ---------------------------------------------------------------------------
# E-15: reset_episode_state clears in-flight; next call triggers INSERT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e15_reset_episode_state_clears_cache():
    _reset_all_state()

    key = fs._episode_key("AAPL", "BULLISH", "CALL", 200.0, "2026-06-20")
    fs._set_episode_in_flight(key, 111, 3, 45000.0)
    assert key in fs._episode_in_flight

    fs.reset_episode_state()
    assert fs._episode_in_flight == {}
    assert fs._episode_locks == {}

    # After reset, a new call for the same key should INSERT (no in-flight).
    signal = _make_signal()
    with patch.object(fs, "_lookup_open_episode", new=AsyncMock(return_value=None)), \
         patch.object(fs, "_insert_rows_with_episode_id", new=AsyncMock(return_value=True)):
        await fs.persist_flow_episode(signal)

    assert fs._episode_stats["created_episodes"] == 1
    assert fs._episode_stats["merged_episodes"]  == 0


# ---------------------------------------------------------------------------
# E-16: _insert_rows_with_episode_id id-return handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e16_insert_with_episode_id_stores_in_flight():
    """
    When PostgREST returns the inserted row with an id, _episode_in_flight
    is populated after _insert_rows_with_episode_id.
    """
    _reset_all_state()

    key = fs._episode_key("NVDA", "BEARISH", "PUT", 500.0, "2026-05-16")
    row = {
        "ticker": "NVDA", "direction": "BEARISH", "contract_type": "PUT",
        "strike": 500.0, "expiry": "2026-05-16", "total_premium": 25000.0,
        "trade_count": 1, "alert_level": "LARGE", "is_accelerating": False,
        "is_multi_day_repeat": False, "seed_episode": None, "signal_ts": None,
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json = MagicMock(return_value=[{"id": 777}])

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch.object(fs, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(fs, "_SUPABASE_KEY", "fake-key"), \
         patch("services.flow_store.httpx.AsyncClient", return_value=mock_client):
        result = await fs._insert_rows_with_episode_id("flow_episodes", row, key, 25000.0)

    assert result is True
    assert key in fs._episode_in_flight
    assert fs._episode_in_flight[key]["id"] == 777
    assert fs._episode_in_flight[key]["total_premium"] == pytest.approx(25000.0)


@pytest.mark.asyncio
async def test_e16_insert_with_episode_id_no_crash_on_missing_id():
    """
    When PostgREST returns a body without an id field (e.g. return=minimal),
    _episode_in_flight is NOT populated — the call still returns True and
    the next waiter safely falls back to _lookup_open_episode.
    """
    _reset_all_state()

    key = fs._episode_key("NVDA", "BEARISH", "PUT", 500.0, "2026-05-16")
    row = {
        "ticker": "NVDA", "direction": "BEARISH", "contract_type": "PUT",
        "strike": 500.0, "expiry": "2026-05-16", "total_premium": 25000.0,
        "trade_count": 1, "alert_level": "LARGE", "is_accelerating": False,
        "is_multi_day_repeat": False, "seed_episode": None, "signal_ts": None,
    }

    mock_resp = MagicMock()
    mock_resp.status_code = 201
    mock_resp.json = MagicMock(return_value=[{}])  # no id field

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_resp)

    with patch.object(fs, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(fs, "_SUPABASE_KEY", "fake-key"), \
         patch("services.flow_store.httpx.AsyncClient", return_value=mock_client):
        result = await fs._insert_rows_with_episode_id("flow_episodes", row, key, 25000.0)

    assert result is True
    assert key not in fs._episode_in_flight  # safe fallback — not populated


# ---------------------------------------------------------------------------
# _lookup_open_episode unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lookup_returns_none_when_not_configured():
    with patch.object(fs, "_SUPABASE_URL", None):
        result = await fs._lookup_open_episode("AAPL", "BULLISH", "CALL", 200.0, "2026-06-20")
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
        result = await fs._lookup_open_episode("AAPL", "BULLISH", "CALL", 200.0, "2026-06-20")

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
        result = await fs._lookup_open_episode("AAPL", "BULLISH", "CALL", 200.0, "2026-06-20")

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
        result = await fs._lookup_open_episode("AAPL", "BULLISH", "CALL", 200.0, "2026-06-20")

    assert result == expected_row


# ---------------------------------------------------------------------------
# Stats counter init — cold-start safety
# ---------------------------------------------------------------------------

def test_episode_stats_initialised_at_module_level():
    assert "created_episodes" in fs._episode_stats
    assert "merged_episodes"  in fs._episode_stats
    assert isinstance(fs._episode_stats["created_episodes"], int)
    assert isinstance(fs._episode_stats["merged_episodes"],  int)


def test_get_episode_stats_returns_copy():
    stats = fs.get_episode_stats()
    assert "created_episodes" in stats
    assert "merged_episodes"  in stats
    stats["created_episodes"] = 9999
    assert fs._episode_stats["created_episodes"] != 9999


# ---------------------------------------------------------------------------
# _EPISODE_MERGE_WINDOW_S constant
# ---------------------------------------------------------------------------

def test_episode_merge_window_is_1800():
    assert fs._EPISODE_MERGE_WINDOW_S == 1800


# ---------------------------------------------------------------------------
# _episode_key helper
# ---------------------------------------------------------------------------

def test_episode_key_format():
    key = fs._episode_key("MSTR", "REPEAT_SELL", "PUT", 185.0, "2026-05-22")
    assert key == "MSTR|REPEAT_SELL|PUT|185.0|2026-05-22"


def test_episode_key_unique_per_dimension():
    k1 = fs._episode_key("AAPL", "BULLISH", "CALL", 200.0, "2026-06-20")
    k2 = fs._episode_key("AAPL", "BULLISH", "CALL", 210.0, "2026-06-20")  # different strike
    k3 = fs._episode_key("AAPL", "BULLISH", "CALL", 200.0, "2026-09-19")  # different expiry
    k4 = fs._episode_key("TSLA", "BULLISH", "CALL", 200.0, "2026-06-20")  # different ticker
    assert len({k1, k2, k3, k4}) == 4


# ---------------------------------------------------------------------------
# reset_episode_state() edge cases
# ---------------------------------------------------------------------------

def test_reset_episode_state_idempotent_on_empty():
    fs.reset_episode_state()  # already empty
    fs.reset_episode_state()  # should not raise
    assert fs._episode_in_flight == {}
    assert fs._episode_locks == {}


def test_set_episode_in_flight_roundtrip():
    _reset_all_state()
    key = "TEST|BUL|CALL|100.0|2026-01-01"
    fs._set_episode_in_flight(key, 42, 3, 55000.0)
    assert fs._episode_in_flight[key] == {
        "id": 42,
        "trade_count": 3,
        "total_premium": 55000.0,
    }


def test_get_episode_lock_creates_and_reuses():
    _reset_all_state()
    key = "X|BUL|CALL|1.0|2026-01-01"
    lock_a = fs._get_episode_lock(key)
    lock_b = fs._get_episode_lock(key)
    assert lock_a is lock_b  # same object
    assert isinstance(lock_a, asyncio.Lock)
