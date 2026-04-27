"""
tests/test_universe_refresh_loop.py

Unit tests for Issue 2 fixes in _universe_refresh_loop and
_refresh_quotes_in_background:

  1. _universe_refresh_loop anchors its first sleep to (24h - elapsed)
     so a restart near the boundary does not sleep a full extra 24h.

  2. When _quote_refresh_lock is held by _refresh_quotes_in_background,
     _universe_refresh_loop skips _fetch_batch_quotes instead of queuing
     a second redundant call.

  3. Conversely, _refresh_quotes_in_background returns immediately (without
     fetching) when the lock is already held.

  4. get_latest_snapshot_timestamp() in universe_store returns the epoch
     datetime when no snapshots exist (so the loop fires immediately).
"""
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import main
import services.universe_store as us


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Test 1: first sleep is anchored to elapsed time, not flat 24h
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_loop_first_sleep_anchored_to_elapsed():
    """
    If the last snapshot was 23h 50m ago the first sleep must be ~10 min,
    not 24h.  We capture the sleep argument via a mock and assert it is close
    to the expected residual interval.
    """
    REFRESH_INTERVAL = 24 * 3600
    elapsed_secs = 23 * 3600 + 50 * 60   # 23h 50m ago
    last_ts = _utcnow() - timedelta(seconds=elapsed_secs)
    expected_first_sleep = REFRESH_INTERVAL - elapsed_secs  # ~600 s

    sleep_calls: list[float] = []

    async def _fake_sleep(secs: float):
        sleep_calls.append(secs)
        if len(sleep_calls) >= 2:  # let the loop run one full iteration
            raise asyncio.CancelledError

    with (
        patch("main.universe_store.get_latest_snapshot_timestamp",
              new=AsyncMock(return_value=last_ts)),
        patch("main.asyncio.sleep",    side_effect=_fake_sleep),
        patch("main.universe_store.load_any_snapshot",
              new=AsyncMock(return_value=["AAPL"])),
        patch("main.load_universe",
              new=AsyncMock(return_value=(["AAPL"], "cboe_fallback", None))),
    ):
        with pytest.raises(asyncio.CancelledError):
            await main._universe_refresh_loop()

    assert sleep_calls, "asyncio.sleep was never called"
    first_sleep = sleep_calls[0]
    # Allow a 5-second tolerance for execution overhead.
    assert abs(first_sleep - expected_first_sleep) < 5, (
        f"Expected first sleep ~{expected_first_sleep}s, got {first_sleep}s"
    )


@pytest.mark.asyncio
async def test_refresh_loop_first_sleep_zero_when_overdue():
    """
    If the last snapshot was 25h ago (overdue) the first sleep must be 0
    (i.e. max(0, REFRESH_INTERVAL - elapsed) clamps at 0).
    """
    last_ts = _utcnow() - timedelta(hours=25)
    sleep_calls: list[float] = []

    async def _fake_sleep(secs: float):
        sleep_calls.append(secs)
        raise asyncio.CancelledError  # stop after first sleep

    with (
        patch("main.universe_store.get_latest_snapshot_timestamp",
              new=AsyncMock(return_value=last_ts)),
        patch("main.asyncio.sleep",    side_effect=_fake_sleep),
        patch("main.universe_store.load_any_snapshot",
              new=AsyncMock(return_value=None)),
        patch("main.load_universe",
              new=AsyncMock(return_value=([], "cboe_fallback", None))),
    ):
        with pytest.raises(asyncio.CancelledError):
            await main._universe_refresh_loop()

    assert sleep_calls[0] == 0.0, (
        f"Expected first sleep 0.0 (overdue), got {sleep_calls[0]}"
    )


# ---------------------------------------------------------------------------
# Test 2: refresh loop skips fetch when lock is held
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_loop_skips_fetch_when_lock_held():
    """
    When _quote_refresh_lock is already acquired, _universe_refresh_loop
    must NOT call _fetch_batch_quotes.
    """
    last_ts  = _utcnow() - timedelta(hours=25)  # overdue → first sleep = 0
    fetch_mock = AsyncMock(return_value=[])
    sleep_iter = iter([0.0])  # only allow the initial anchored sleep

    async def _fake_sleep(secs: float):
        try:
            next(sleep_iter)
        except StopIteration:
            raise asyncio.CancelledError

    # Manually acquire the lock before the loop runs.
    await main._quote_refresh_lock.acquire()
    try:
        with (
            patch("main.universe_store.get_latest_snapshot_timestamp",
                  new=AsyncMock(return_value=last_ts)),
            patch("main.asyncio.sleep",        side_effect=_fake_sleep),
            patch("main.universe_store.load_any_snapshot",
                  new=AsyncMock(return_value=["AAPL"])),
            patch("main.load_universe",
                  new=AsyncMock(return_value=(["AAPL"], "tradier_validated", {"AAPL"}))),
            patch("main.universe_store.save_snapshot",
                  new=AsyncMock(return_value=True)),
            patch("main._fetch_batch_quotes",  fetch_mock),
            patch("main.get_registry",         return_value=None),
        ):
            with pytest.raises(asyncio.CancelledError):
                await main._universe_refresh_loop()
    finally:
        main._quote_refresh_lock.release()

    fetch_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: _refresh_quotes_in_background skips when lock is held
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bg_refresh_skips_when_lock_held():
    """
    If _quote_refresh_lock is held when _refresh_quotes_in_background is
    called it should return immediately without calling _fetch_batch_quotes.
    """
    fetch_mock = AsyncMock(return_value=[])

    await main._quote_refresh_lock.acquire()
    try:
        with patch("main._fetch_batch_quotes", fetch_mock):
            await main._refresh_quotes_in_background(["AAPL", "TSLA"])
    finally:
        main._quote_refresh_lock.release()

    fetch_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 4: get_latest_snapshot_timestamp returns epoch when no rows
# ---------------------------------------------------------------------------

def test_get_latest_snapshot_timestamp_returns_epoch_when_empty():
    """
    _sync_get_latest_snapshot_timestamp() must return the Unix epoch when
    the DB returns no rows so the refresh loop treats it as "never refreshed"
    and the first sleep evaluates to 0.
    """
    mock_sb = MagicMock()
    (
        mock_sb.table.return_value
            .select.return_value
            .order.return_value
            .limit.return_value
            .execute.return_value
            .data
    ) = []

    with patch("services.universe_store._client", return_value=mock_sb):
        result = us._sync_get_latest_snapshot_timestamp()

    assert result == us._EPOCH, f"Expected epoch, got {result}"


def test_get_latest_snapshot_timestamp_parses_iso_string():
    """
    _sync_get_latest_snapshot_timestamp() must parse a Supabase ISO-8601
    string (with trailing Z) and return a tz-aware datetime.
    """
    iso_str  = "2026-04-26T10:00:00Z"
    expected = datetime(2026, 4, 26, 10, 0, 0, tzinfo=timezone.utc)

    mock_sb = MagicMock()
    (
        mock_sb.table.return_value
            .select.return_value
            .order.return_value
            .limit.return_value
            .execute.return_value
            .data
    ) = [{"fetched_at": iso_str}]

    with patch("services.universe_store._client", return_value=mock_sb):
        result = us._sync_get_latest_snapshot_timestamp()

    assert result == expected, f"Expected {expected}, got {result}"
    assert result.tzinfo is not None, "Result must be tz-aware"
