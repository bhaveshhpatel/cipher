"""
Regression tests for services/stream_manager.py

Covers:
 - StreamManager is importable and instantiable
 - Initial state is not running
 - has start() and stop() methods
 - status() returns a dict
 - status() dict contains a 'running' key
 - Mocking _stream dependency does not break status()
 - stop() on a non-running manager does not raise
 - Multiple instances are independent
"""
import pytest
from unittest.mock import MagicMock, patch
from services.stream_manager import StreamManager


def test_stream_manager_importable():
    assert StreamManager is not None


def test_stream_manager_instantiable():
    mgr = StreamManager()
    assert mgr is not None


def test_stream_manager_initial_not_running():
    mgr = StreamManager()
    assert not mgr.is_running()


def test_stream_manager_has_start_and_stop():
    mgr = StreamManager()
    assert hasattr(mgr, "start")
    assert hasattr(mgr, "stop")
    assert callable(mgr.start)
    assert callable(mgr.stop)


def test_stream_manager_status_returns_dict():
    mgr = StreamManager()
    status = mgr.status()
    assert isinstance(status, dict)


def test_stream_manager_status_has_running_key():
    mgr = StreamManager()
    status = mgr.status()
    assert "running" in status


def test_stream_manager_stop_when_not_running_does_not_raise():
    mgr = StreamManager()
    try:
        mgr.stop()
    except Exception as exc:
        pytest.fail(f"stop() raised unexpectedly: {exc}")


def test_stream_manager_mock_stream_dependency():
    mgr = StreamManager()
    mgr._stream = MagicMock()
    assert mgr._stream is not None
    status = mgr.status()
    assert isinstance(status, dict)


def test_stream_manager_multiple_instances_are_independent():
    mgr_a = StreamManager()
    mgr_b = StreamManager()
    mgr_a._stream = MagicMock()
    assert mgr_b._stream is not mgr_a._stream


def test_stream_manager_patch_start():
    mgr = StreamManager()
    with patch.object(mgr, "start") as mock_start:
        mgr.start()
        mock_start.assert_called_once()
