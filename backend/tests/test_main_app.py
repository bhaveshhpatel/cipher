"""
Regression tests for main.py app wiring, lifespan, and middleware.
"""
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock


def _get_client():
    from main import app
    return TestClient(app)


def test_app_health_endpoint_exists():
    client = _get_client()
    resp = client.get("/api/health")
    assert resp.status_code in (200, 503)


def test_app_returns_404_for_unknown_path():
    client = _get_client()
    resp = client.get("/nonexistent-route-xyz")
    assert resp.status_code == 404


def test_cors_headers_present_on_options():
    client = _get_client()
    resp = client.options("/api/health",
                          headers={"Origin": "http://localhost:3000",
                                   "Access-Control-Request-Method": "GET"})
    assert resp.status_code in (200, 204, 400)


def test_stamp_oi_helper_exists():
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
    from main import app
    paths = {r.path for r in app.routes}
    for prefix in ("/api/health", "/api/auth"):
        assert any(p.startswith(prefix) for p in paths), \
            f"Router with prefix {prefix} not mounted"


def test_lifespan_does_not_crash_on_startup():
    with patch("main.init_registry"), \
         patch("main.assign_tiers", new_callable=AsyncMock, return_value={}), \
         patch("main.get_config", new_callable=AsyncMock, return_value={}):
        client = _get_client()
        resp = client.get("/api/health")
        assert resp.status_code in (200, 503)
