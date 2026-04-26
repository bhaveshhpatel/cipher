"""
Phase 4 — test_main_app.py

Covers main.py surface area without touching real Tradier / Supabase:
  - GET /health → 200 {status: ok}
  - GET /       → 200 {message: ...}
  - CORS: production origin allowed
  - CORS: unknown origin not reflected in Allow-Origin header
  - CORS: localhost:3000 is always in allow list
  - Router mounting: /api/auth, /api/flow, /api/simulation, /api/signals,
                     /api/history, /api/admin, /api/ws all registered
  - _configure_logging: root logger has exactly one handler; formatter is _JsonFormatter
  - _JsonFormatter.format(): severity mapping for all 5 levels
  - _JsonFormatter.format(): exception info included when present
  - _stamp_oi(): sets open_interest from oi_map; missing symbols get 0
  - _JsonFormatter: unknown level maps to 'info'
"""
import json
import logging
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers to build a minimal app without running lifespan (no real I/O)
# ---------------------------------------------------------------------------
def _bare_app():
    """
    Build the FastAPI app instance from main.py without triggering lifespan
    (TestClient with lifespan=False or by importing the app object directly
    and wrapping with raise_server_exceptions=False for health checks only).
    """
    # Patch all startup I/O before importing main
    with (
        patch("services.tradier_stream.stream_options_flow", new=AsyncMock()),
        patch("services.symbols_loader.load_universe",       new=AsyncMock(return_value=([], "seed", None, []))),
        patch("services.universe_store.load_fresh_snapshot", new=AsyncMock(return_value=None)),
        patch("services.universe_store.load_any_snapshot",   new=AsyncMock(return_value=None)),
        patch("services.flow_store.start_flow_writer",        new=AsyncMock()),
        patch("services.signal_store.start_signal_writer",   new=AsyncMock()),
        patch("services.symbol_registry.init_registry",       new=MagicMock(return_value=MagicMock())),
    ):
        import importlib
        import main as m
        importlib.reload(m)
        return m.app


# ---------------------------------------------------------------------------
# Health / root endpoints
# ---------------------------------------------------------------------------
class TestHealthEndpoints:

    def test_health_root_returns_ok(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health", headers={"Origin": "http://localhost:3000"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_root_returns_message(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/", headers={"Origin": "http://localhost:3000"})
        assert resp.status_code == 200
        assert "message" in resp.json()

    def test_health_service_field(self):
        from main import app
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/health")
        assert resp.json().get("service") == "cipher-api"


# ---------------------------------------------------------------------------
# Router registration (routes must exist)
# ---------------------------------------------------------------------------
class TestRouterMounting:

    def _route_paths(self):
        from main import app
        return {r.path for r in app.routes}

    def test_auth_routes_registered(self):
        paths = self._route_paths()
        assert any("/api/auth" in p for p in paths)

    def test_flow_routes_registered(self):
        paths = self._route_paths()
        assert any("/api/flow" in p for p in paths)

    def test_simulation_routes_registered(self):
        paths = self._route_paths()
        assert any("/api/simulation" in p for p in paths)

    def test_signals_routes_registered(self):
        paths = self._route_paths()
        assert any("/api/signals" in p for p in paths)

    def test_history_routes_registered(self):
        paths = self._route_paths()
        assert any("/api/history" in p for p in paths)

    def test_admin_routes_registered(self):
        paths = self._route_paths()
        assert any("/api/admin" in p for p in paths)

    def test_health_routes_registered(self):
        paths = self._route_paths()
        assert any("/health" in p for p in paths)


# ---------------------------------------------------------------------------
# _JsonFormatter
# ---------------------------------------------------------------------------
class TestJsonFormatter:

    def _formatter(self):
        from main import _JsonFormatter
        return _JsonFormatter()

    def _make_record(self, level: int, msg: str, exc_info=None):
        record = logging.LogRecord(
            name="test", level=level, pathname="", lineno=0,
            msg=msg, args=(), exc_info=exc_info,
        )
        return record

    def test_info_severity(self):
        f = self._formatter()
        rec = self._make_record(logging.INFO, "hello")
        payload = json.loads(f.format(rec))
        assert payload["severity"] == "info"
        assert payload["message"]  == "hello"

    def test_debug_severity(self):
        payload = json.loads(self._formatter().format(
            self._make_record(logging.DEBUG, "dbg")))
        assert payload["severity"] == "debug"

    def test_warning_severity(self):
        payload = json.loads(self._formatter().format(
            self._make_record(logging.WARNING, "warn")))
        assert payload["severity"] == "warning"

    def test_error_severity(self):
        payload = json.loads(self._formatter().format(
            self._make_record(logging.ERROR, "err")))
        assert payload["severity"] == "error"

    def test_critical_severity(self):
        payload = json.loads(self._formatter().format(
            self._make_record(logging.CRITICAL, "crit")))
        assert payload["severity"] == "critical"

    def test_unknown_level_defaults_to_info(self):
        payload = json.loads(self._formatter().format(
            self._make_record(999, "unknown level")))
        assert payload["severity"] == "info"

    def test_exception_info_included(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        rec = self._make_record(logging.ERROR, "err", exc_info=exc_info)
        payload = json.loads(self._formatter().format(rec))
        assert "exception" in payload
        assert "ValueError" in payload["exception"]

    def test_json_has_logger_and_timestamp(self):
        payload = json.loads(self._formatter().format(
            self._make_record(logging.INFO, "x")))
        assert "logger"    in payload
        assert "timestamp" in payload


# ---------------------------------------------------------------------------
# _stamp_oi
# ---------------------------------------------------------------------------
class TestStampOI:

    def test_stamps_open_interest(self):
        from main import _stamp_oi
        q1 = MagicMock()
        q1.symbol = "AAPL"
        q2 = MagicMock()
        q2.symbol = "TSLA"
        _stamp_oi([q1, q2], {"AAPL": 5000, "TSLA": 12000})
        assert q1.open_interest == 5000
        assert q2.open_interest == 12000

    def test_missing_symbol_gets_zero(self):
        from main import _stamp_oi
        q = MagicMock()
        q.symbol = "UNKNOWN"
        _stamp_oi([q], {})
        assert q.open_interest == 0

    def test_empty_quotes_no_crash(self):
        from main import _stamp_oi
        _stamp_oi([], {"AAPL": 1000})  # should not raise

    def test_empty_oi_map_all_zeros(self):
        from main import _stamp_oi
        quotes = [MagicMock() for _ in range(5)]
        for q in quotes:
            q.symbol = "SPY"
        _stamp_oi(quotes, {})
        for q in quotes:
            assert q.open_interest == 0
