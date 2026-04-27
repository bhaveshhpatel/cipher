"""
Tests for _registry_prewarm_loop in main.py.

Covers:
  - Weekend: loop sleeps 3600s and does NOT call registry.build()
  - Weekday before 9:15 ET: sleep duration is <= 915s (at most 15 min)
  - Weekday after 9:15 ET: next_prewarm rolls to a future weekday
  - build() is awaited on the registry returned by get_registry()
  - Build exception is caught and loop continues (non-fatal)
"""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

_ET = ZoneInfo("America/New_York")


async def _run_one_iteration(mock_now: datetime) -> tuple[list, list]:
    """Run _registry_prewarm_loop for one sleep cycle.

    Returns (sleep_calls, build_calls).
    """
    from main import _registry_prewarm_loop

    sleep_calls: list[float] = []
    build_calls: list = []

    async def fake_sleep(secs: float):
        sleep_calls.append(secs)
        raise asyncio.CancelledError

    mock_registry = MagicMock()
    mock_registry.build = AsyncMock(side_effect=lambda: build_calls.append(True) or 42)

    with patch("main.datetime") as mock_dt, \
         patch("main.asyncio.sleep", side_effect=fake_sleep), \
         patch("main.get_registry", return_value=mock_registry):

        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        try:
            await _registry_prewarm_loop()
        except asyncio.CancelledError:
            pass

    return sleep_calls, build_calls


@pytest.mark.asyncio
async def test_prewarm_skips_weekends():
    saturday = datetime(2026, 4, 25, 12, 0, 0, tzinfo=_ET)
    assert saturday.weekday() == 5

    sleep_calls, build_calls = await _run_one_iteration(saturday)

    assert sleep_calls == [3600], f"Expected sleep(3600) on weekend, got {sleep_calls}"
    assert build_calls == [], "build() must NOT be called on weekends"


@pytest.mark.asyncio
async def test_prewarm_sleep_before_915():
    monday_before = datetime(2026, 4, 27, 9, 0, 0, tzinfo=_ET)
    assert monday_before.weekday() == 0

    sleep_calls, _build_calls = await _run_one_iteration(monday_before)

    assert len(sleep_calls) == 1
    assert 895 <= sleep_calls[0] <= 905, (
        f"Expected sleep ~900s before 9:15, got {sleep_calls[0]:.1f}s"
    )


@pytest.mark.asyncio
async def test_prewarm_rolls_to_next_weekday_if_past_915():
    monday_after = datetime(2026, 4, 27, 10, 0, 0, tzinfo=_ET)
    assert monday_after.weekday() == 0

    sleep_calls, _build_calls = await _run_one_iteration(monday_after)

    assert len(sleep_calls) == 1
    expected = (24 * 3600) - (10 * 3600) + (9 * 3600 + 15 * 60)
    assert abs(sleep_calls[0] - expected) <= 120, (
        f"Expected sleep ~{expected}s for next-day prewarm, got {sleep_calls[0]:.1f}s"
    )


@pytest.mark.asyncio
async def test_prewarm_calls_registry_build():
    """After sleeping to 9:15, _registry_prewarm_loop must await registry.build()."""
    from main import _registry_prewarm_loop

    build_calls: list = []
    sleep_count = 0

    async def fake_sleep(secs: float):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 2:
            raise asyncio.CancelledError

    mock_registry = MagicMock()
    mock_registry.build = AsyncMock(side_effect=lambda: build_calls.append(True) or 100)

    monday_before = datetime(2026, 4, 27, 9, 0, 0, tzinfo=_ET)

    with patch("main.datetime") as mock_dt, \
         patch("main.asyncio.sleep", side_effect=fake_sleep), \
         patch("main.get_registry", return_value=mock_registry):

        mock_dt.now.return_value = monday_before
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        try:
            await _registry_prewarm_loop()
        except asyncio.CancelledError:
            pass

    assert build_calls == [True], "registry.build() must be called after the prewarm sleep"


@pytest.mark.asyncio
async def test_prewarm_survives_build_exception():
    """A build() failure must be swallowed so the loop keeps running."""
    from main import _registry_prewarm_loop

    sleep_count = 0

    async def fake_sleep(secs: float):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 2:
            raise asyncio.CancelledError

    mock_registry = MagicMock()
    mock_registry.build = AsyncMock(side_effect=RuntimeError("Tradier timeout"))

    monday_before = datetime(2026, 4, 27, 9, 0, 0, tzinfo=_ET)

    with patch("main.datetime") as mock_dt, \
         patch("main.asyncio.sleep", side_effect=fake_sleep), \
         patch("main.get_registry", return_value=mock_registry):

        mock_dt.now.return_value = monday_before
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        try:
            await _registry_prewarm_loop()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            pytest.fail(f"_registry_prewarm_loop raised unexpectedly: {exc}")
