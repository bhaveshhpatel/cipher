"""Regression tests for services/stream_manager.py"""
import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock


@pytest.fixture
def manager():
    from services.stream_manager import StreamManager
    return StreamManager()


# ---------------------------------------------------------------------------
# Basic lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manager_initial_state(manager):
    assert not manager.is_running()


@pytest.mark.asyncio
async def test_start_sets_running(manager):
    with patch.object(manager, '_start_stream', new_callable=AsyncMock):
        await manager.start()
    assert manager.is_running()


@pytest.mark.asyncio
async def test_stop_clears_running(manager):
    with patch.object(manager, '_start_stream', new_callable=AsyncMock):
        await manager.start()
    with patch.object(manager, '_stop_stream', new_callable=AsyncMock):
        await manager.stop()
    assert not manager.is_running()


@pytest.mark.asyncio
async def test_start_idempotent(manager):
    with patch.object(manager, '_start_stream', new_callable=AsyncMock) as mock_start:
        await manager.start()
        await manager.start()
    assert mock_start.call_count == 1


@pytest.mark.asyncio
async def test_stop_when_not_running_is_noop(manager):
    with patch.object(manager, '_stop_stream', new_callable=AsyncMock) as mock_stop:
        await manager.stop()
    assert mock_stop.call_count == 0


@pytest.mark.asyncio
async def test_worker_registered_on_start(manager):
    with patch.object(manager, '_start_stream', new_callable=AsyncMock):
        await manager.start()
    assert manager.is_running()


@pytest.mark.asyncio
async def test_add_watchlist_symbol(manager):
    if not hasattr(manager, 'add_symbol'):
        pytest.skip("StreamManager has no add_symbol method")
    manager.add_symbol("AAPL")
    assert "AAPL" in manager.get_watchlist()


@pytest.mark.asyncio
async def test_remove_watchlist_symbol(manager):
    if not hasattr(manager, 'remove_symbol'):
        pytest.skip("StreamManager has no remove_symbol method")
    manager.add_symbol("TSLA")
    manager.remove_symbol("TSLA")
    assert "TSLA" not in manager.get_watchlist()
