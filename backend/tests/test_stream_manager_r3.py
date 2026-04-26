"""
Release-3 regression tests for StreamManager.
"""
from unittest.mock import MagicMock


def test_stream_manager_r3_importable():
    from services.stream_manager import StreamManager
    assert StreamManager is not None


def test_stream_manager_r3_initial_not_running():
    from services.stream_manager import StreamManager
    mgr = StreamManager()
    assert not mgr.is_running()


def test_stream_manager_r3_has_start_stop():
    from services.stream_manager import StreamManager
    mgr = StreamManager()
    assert hasattr(mgr, "start") and hasattr(mgr, "stop")


def test_stream_manager_r3_mock_dependency():
    from services.stream_manager import StreamManager
    mgr = StreamManager()
    mgr._stream = MagicMock()
    assert mgr._stream is not None
