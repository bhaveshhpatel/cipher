"""
Tests for B-008: GET /health/stream

Covers:
  1. Unauthenticated request → 401
  2. Authenticated request   → 200 with correct schema
  3. All required fields present in response
  4. Mode defaults to a known value when stream idle
  5. last_tick_at and last_reconnect_at are null when stream has never ticked
  6. uptime_seconds is a non-negative float
  7. Stats counters are non-negative integers
  8. Mocked stats propagate correctly to response fields
  9. _epoch_to_iso helper returns None for None input
  10. _epoch_to_iso helper returns valid ISO-8601 string for a known epoch
"""
import time
import unittest.mock as mock
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


# ── shared mock stats ─────────────────────────────────────────────────────
_IDLE_STATS = {
    "mode":              "idle",
    "active_symbols":   0,
    "ticks":            0,
    "classified":       0,
    "deduped":          0,
    "signals":          0,
    "errors":           0,
    "reconnects":       0,
    "last_tick_at":     None,
    "last_reconnect_at": None,
    "uptime_seconds":   42.0,
}

_LIVE_STATS = {
    "mode":              "live",
    "active_symbols":   150,
    "ticks":            8_000,
    "classified":       7_800,
    "deduped":          200,
    "signals":          320,
    "errors":           3,
    "reconnects":       1,
    "last_tick_at":     1_700_000_000.0,   # known epoch
    "last_reconnect_at": 1_699_999_900.0,
    "uptime_seconds":   3_600.5,
}


async def _async_noop(*_a, **_kw):
    import asyncio
    await asyncio.sleep(0)


@pytest.fixture(scope="module")
def client():
    """TestClient with stream patched to idle stats and auth bypass available."""
    with (
        patch("services.tradier_stream.stream_options_flow", side_effect=_async_noop),
        patch("services.tradier_stream.get_stats", return_value=_IDLE_STATS),
    ):
        from main import app
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


def _token(client) -> str:
    """Register a fresh user and return a valid Bearer token."""
    import uuid
    email = f"health_{uuid.uuid4().hex[:8]}@cipher.io"
    client.post("/api/auth/register", json={"email": email, "password": "Secure123!"})
    r = client.post("/api/auth/token", data={"username": email, "password": "Secure123!"})
    return r.json()["access_token"]


# ── 1: Unauthenticated ────────────────────────────────────────────────────
def test_stream_health_requires_auth(client):
    r = client.get("/health/stream")
    assert r.status_code == 401, r.text


# ── 2: Authenticated → 200 ────────────────────────────────────────────────
def test_stream_health_authenticated_200(client):
    token = _token(client)
    r = client.get("/health/stream", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text


# ── 3: All required fields present ────────────────────────────────────────
def test_stream_health_schema(client):
    token = _token(client)
    r = client.get("/health/stream", headers={"Authorization": f"Bearer {token}"})
    body = r.json()
    required = {
        "mode", "active_symbols", "ticks", "classified", "deduped",
        "signals", "errors", "reconnects",
        "last_tick_at", "last_reconnect_at", "uptime_seconds",
    }
    assert required.issubset(body.keys()), f"Missing keys: {required - body.keys()}"


# ── 4: Mode is a known string ─────────────────────────────────────────────
VALID_MODES = {"starting", "live", "demo", "idle", "reconnecting", "market_closed", "unknown"}

def test_stream_health_mode_valid(client):
    token = _token(client)
    r = client.get("/health/stream", headers={"Authorization": f"Bearer {token}"})
    assert r.json()["mode"] in VALID_MODES


# ── 5: Nulls when stream never ticked ─────────────────────────────────────
def test_stream_health_nulls_when_idle(client):
    token = _token(client)
    r = client.get("/health/stream", headers={"Authorization": f"Bearer {token}"})
    body = r.json()
    assert body["last_tick_at"] is None
    assert body["last_reconnect_at"] is None


# ── 6: uptime_seconds non-negative float ──────────────────────────────────
def test_stream_health_uptime_non_negative(client):
    token = _token(client)
    r = client.get("/health/stream", headers={"Authorization": f"Bearer {token}"})
    assert r.json()["uptime_seconds"] >= 0.0


# ── 7: All counters non-negative ──────────────────────────────────────────
def test_stream_health_counters_non_negative(client):
    token = _token(client)
    r = client.get("/health/stream", headers={"Authorization": f"Bearer {token}"})
    body = r.json()
    for field in ("ticks", "classified", "deduped", "signals", "errors", "reconnects", "active_symbols"):
        assert body[field] >= 0, f"{field} was negative: {body[field]}"


# ── 8: Live stats propagate correctly ─────────────────────────────────────
def test_stream_health_live_stats_propagate():
    """Patch get_stats to live values and verify field-level mapping."""
    async def _noop(*_, **__):
        import asyncio; await asyncio.sleep(0)

    with (
        patch("services.tradier_stream.stream_options_flow", side_effect=_noop),
        patch("services.tradier_stream.get_stats", return_value=_LIVE_STATS),
    ):
        from main import app
        with TestClient(app, raise_server_exceptions=True) as c:
            import uuid
            email = f"live_{uuid.uuid4().hex[:8]}@cipher.io"
            c.post("/api/auth/register", json={"email": email, "password": "Secure123!"})
            t = c.post("/api/auth/token", data={"username": email, "password": "Secure123!"})
            token = t.json()["access_token"]

            r = c.get("/health/stream", headers={"Authorization": f"Bearer {token}"})
            body = r.json()

    assert body["mode"] == "live"
    assert body["active_symbols"] == 150
    assert body["ticks"] == 8_000
    assert body["classified"] == 7_800
    assert body["deduped"] == 200
    assert body["signals"] == 320
    assert body["errors"] == 3
    assert body["reconnects"] == 1
    assert body["uptime_seconds"] == 3_600.5
    # timestamps should be ISO strings (not null)
    assert body["last_tick_at"] is not None
    assert "T" in body["last_tick_at"]
    assert body["last_reconnect_at"] is not None


# ── 9 & 10: _epoch_to_iso helper ──────────────────────────────────────────
def test_epoch_to_iso_none():
    from routers.health import _epoch_to_iso
    assert _epoch_to_iso(None) is None


def test_epoch_to_iso_known_epoch():
    from routers.health import _epoch_to_iso
    # epoch 0 → 1970-01-01T00:00:00+00:00
    result = _epoch_to_iso(0.0)
    assert result is not None
    assert result.startswith("1970-01-01T00:00:00")
    assert "+00:00" in result or "Z" in result or "UTC" in result
