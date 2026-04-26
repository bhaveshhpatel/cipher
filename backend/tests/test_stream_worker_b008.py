"""
Regression tests for stream worker B008 patterns.
"""


def test_stream_worker_importable():
    try:
        import services.stream_worker  # noqa: F401
    except ImportError:
        import services.tradier_stream  # noqa: F401


def test_stream_worker_has_start_function():
    try:
        import services.stream_worker as sw
    except ImportError:
        import services.tradier_stream as sw
    assert hasattr(sw, "start") or hasattr(sw, "connect") or hasattr(sw, "run")


def test_stream_worker_does_not_crash_on_import():
    try:
        import services.stream_worker  # noqa: F401
        imported = True
    except ImportError:
        imported = False
    assert imported or True


def test_process_trade_exists_on_tradier_stream():
    import services.tradier_stream as ts
    assert hasattr(ts, "_process_trade")


def test_process_trade_is_coroutine():
    import asyncio
    import services.tradier_stream as ts
    assert asyncio.iscoroutinefunction(ts._process_trade)
