"""
B008 regression: stream_worker startup/shutdown and tradier_stream handoff.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch


def test_tradier_stream_importable():
    import services.tradier_stream  # noqa: F401


def test_stream_worker_importable():
    import services.stream_worker  # noqa: F401


@pytest.mark.asyncio
async def test_stream_worker_starts_without_crash():
    import services.stream_worker as sw
    fn = getattr(sw, "start", getattr(sw, "run", None))
    if fn is None:
        pytest.skip("No start/run function found in stream_worker")
    with patch("services.tradier_stream.start_stream", new_callable=AsyncMock), \
         patch("services.tradier_stream._get_session_token",
               new_callable=AsyncMock, return_value="tok"):
        task = asyncio.create_task(fn(["AAPL"]))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_stream_worker_cancels_cleanly():
    import services.stream_worker as sw
    fn = getattr(sw, "start", getattr(sw, "run", None))
    if fn is None:
        pytest.skip("No start/run function")
    with patch("services.tradier_stream.start_stream", new_callable=AsyncMock):
        task = asyncio.create_task(fn(["SPY", "QQQ"]))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    # Reaching here = clean cancel
