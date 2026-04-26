"""
Regression tests for services/stream_manager.py
"""


def test_stream_manager_importable():
    from services.stream_manager import StreamManager
    assert StreamManager is not None


def test_stream_manager_initial_not_running():
    from services.stream_manager import StreamManager
    mgr = StreamManager()
    assert not mgr.is_running()


def test_stream_manager_has_start_stop():
    from services.stream_manager import StreamManager
    mgr = StreamManager()
    assert hasattr(mgr, "start") and hasattr(mgr, "stop")


def test_stream_manager_status_dict():
    from services.stream_manager import StreamManager
    mgr = StreamManager()
    status = mgr.status()
    assert isinstance(status, dict)
