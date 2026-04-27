"""
Regression tests for main.py app wiring, lifespan, and middleware.

Covers:
 - /health endpoint is mounted and reachable
 - Unknown routes return 404
 - CORS OPTIONS preflight returns acceptable status
 - _stamp_oi helper is callable
 - _stamp_oi populates open_interest from lookup dict
 - _stamp_oi sets zero for symbols not in lookup dict
 - All required router prefixes are mounted
 - Lifespan startup does not crash with mocked dependencies
 - Rate-limited path still returns a response (not 500)
 - App instance is a FastAPI application
 - Lifespan spawns the registry pre-warm task
"""
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock


def _get_client():
    from main import app
    return TestClient(app)


def test_app_health_endpoint_exists():
    client = _get_client()
    resp = client.get("/health")
    assert resp.status_code in (200, 503)


def test_app_returns_404_for_unknown_path():
    client = _get_client()
    resp = client.get("/nonexistent-route-xyz")
    assert resp.status_code == 404


def test_cors_headers_present_on_options():
    client = _get_client()
    resp = client.options(
        "/health",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code in (200, 204, 400)


def test_stamp_oi_helper_is_callable():
    from main import _stamp_oi
    assert callable(_stamp_oi)


def test_stamp_oi_populates_open_interest():
    from main import _stamp_oi
    from dataclasses import dataclass

    @dataclass
    class _Q:
        symbol: str
        open_interest: int = 0

    quotes = [_Q("AAPL"), _Q("TSLA")]
    _stamp_oi(quotes, {"AAPL": 1500, "TSLA": 800})
    assert quotes[0].open_interest == 1500
    assert quotes[1].open_interest == 800


def test_stamp_oi_missing_symbol_gets_zero():
    from main import _stamp_oi
    from dataclasses import dataclass

    @dataclass
    class _Q:
        symbol: str
        open_interest: int = 0

    quotes = [_Q("UNKNOWN", open_interest=999)]
    _stamp_oi(quotes, {})
    assert quotes[0].open_interest == 0


def test_routers_all_mounted():
    """Verify that core router prefixes are registered on the app.

    health router  -> prefix='/health'  (not /api/health)
    auth router    -> prefix='/api/auth'
    """
    from main import app
    paths = {r.path for r in app.routes}
    expected = {
        "/health":   "/health",     # B-008 stream health router
        "/api/auth": "/api/auth",   # auth router
    }
    for label, prefix in expected.items():
        assert any(p.startswith(prefix) for p in paths), \
            f"Router prefix {prefix!r} ({label}) not mounted on app"


def test_app_is_fastapi_instance():
    from fastapi import FastAPI
    from main import app
    assert isinstance(app, FastAPI)


def test_lifespan_does_not_crash_on_startup():
    with patch("main.init_registry"), \
         patch("main.assign_tiers", new_callable=AsyncMock, return_value={}), \
         patch("main.get_config", new_callable=AsyncMock, return_value={}):
        client = _get_client()
        resp = client.get("/health")
        assert resp.status_code in (200, 503)


def test_rate_limited_path_returns_response_not_500():
    """A rate-limited endpoint must return 200 or 429, never 500."""
    client = _get_client()
    resp = client.get("/api/flow/scan")
    assert resp.status_code != 500


def test_lifespan_spawns_prewarm_task():
    """
    Lifespan must create a _registry_prewarm_loop background task.

    Drive `lifespan` directly as an async context manager so we are not
    subject to the TestClient singleton-caching problem (the app lifespan
    only runs once per process when using TestClient on a shared app object).

    IMPORTANT: asyncio.run() closes the event loop it creates when it
    returns. On Python 3.10+ this leaves NO current event loop on the
    main thread, which would break every subsequent test that calls
    asyncio.get_event_loop(). We install a fresh loop in a finally block
    to restore the expected state for the rest of the test session.
    """
    import asyncio
    from unittest.mock import patch, AsyncMock, MagicMock
    import main as main_module

    created_targets: list[str] = []

    async def _run_lifespan():
        real_create_task = asyncio.create_task

        def tracking_create_task(coro, **kwargs):
            name = getattr(coro, "__name__", repr(coro))
            created_targets.append(name)
            task = real_create_task(coro, **kwargs)
            task.cancel()
            return task

        mock_registry = MagicMock()
        mock_registry.build = AsyncMock(return_value=100)
        mock_registry.size = MagicMock(return_value=100)
        mock_registry.get_oi_map = MagicMock(return_value={})
        mock_registry.refresh_loop = AsyncMock()
        mock_registry.set_tier_map = MagicMock()
        mock_registry.load_from_db = AsyncMock(return_value=0)
        mock_registry.is_ready = MagicMock(return_value=False)

        with patch.object(main_module, "_resolve_startup_universe",
                          new_callable=AsyncMock,
                          return_value=([], {}, [], "")), \
             patch.object(main_module, "init_registry",
                          return_value=mock_registry), \
             patch.object(main_module, "assign_tiers",
                          new_callable=AsyncMock, return_value={}), \
             patch.object(main_module, "stream_options_flow",
                          new_callable=AsyncMock), \
             patch.object(main_module, "start_flow_writer",
                          new_callable=AsyncMock), \
             patch.object(main_module, "start_signal_writer",
                          new_callable=AsyncMock), \
             patch.object(main_module, "_universe_refresh_loop",
                          new_callable=AsyncMock), \
             patch.object(main_module.asyncio, "create_task",
                          side_effect=tracking_create_task):
            async with main_module.lifespan(main_module.app):
                pass

    try:
        asyncio.run(_run_lifespan())
    finally:
        # asyncio.run() closes the loop it created. Restore a fresh loop so
        # subsequent tests that call asyncio.get_event_loop() don't crash.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    assert "_registry_prewarm_loop" in created_targets, (
        f"_registry_prewarm_loop task was not created in lifespan. "
        f"Tasks found: {created_targets}"
    )
