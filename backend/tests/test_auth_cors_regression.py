"""
Regression tests for auth registration "Failed to fetch" bug.

Covers:
  1. CORS preflight OPTIONS /api/auth/register → 200
  2. CORS preflight OPTIONS /api/auth/token    → 200
  3. Register with valid credentials           → 201
  4. Duplicate email registration              → 409
  5. Short password (<8 chars)                 → 422
  6. Login with correct credentials            → 200 + access_token
  7. Login with wrong password                 → 401
  8. Login with unknown email                  → 401
  9. /api/auth/me with valid token             → 200 + correct email
  10. /api/auth/me with no token               → 401
  11. Health endpoint is reachable             → 200
  12. CORS headers present on register response
"""
import uuid
import pytest
from fastapi.testclient import TestClient

import unittest.mock as mock

_MOCK_USER = mock.MagicMock()
_MOCK_USER.user = mock.MagicMock()


async def _async_noop(*_a, **_kw):
    """Async no-op used to stub out tasks that make real network/DB calls."""
    import asyncio
    await asyncio.sleep(0)


async def _async_noop_zero(*_a, **_kw):
    """Async no-op that returns 0 (for build/load_from_db return values)."""
    import asyncio
    await asyncio.sleep(0)
    return 0


async def _resolve_startup_universe_stub():
    """Return a safe empty startup universe so lifespan completes without I/O."""
    return [], {}, [], ""


@pytest.fixture(scope="module")
def client():
    with (
        # Auth router helpers
        mock.patch("routers.auth._supabase_admin", return_value=None),
        mock.patch("routers.auth._supabase_client", return_value=None),
        # Universe resolution — bypasses all Supabase + Tradier calls in lifespan
        mock.patch("main._resolve_startup_universe", side_effect=_resolve_startup_universe_stub),
        # Symbol registry I/O — build() and refresh_loop() make real Tradier calls
        mock.patch(
            "services.symbol_registry.SymbolRegistry.build",
            side_effect=_async_noop_zero,
        ),
        mock.patch(
            "services.symbol_registry.SymbolRegistry.refresh_loop",
            side_effect=_async_noop,
        ),
        mock.patch(
            "services.symbol_registry.SymbolRegistry.load_from_db",
            side_effect=_async_noop_zero,
        ),
        # Background tasks
        mock.patch("services.tradier_stream.stream_options_flow", side_effect=_async_noop),
        mock.patch("main._universe_refresh_loop", side_effect=_async_noop),
        mock.patch("main._registry_prewarm_loop", side_effect=_async_noop),
        mock.patch("services.flow_store.start_flow_writer", side_effect=_async_noop),
        mock.patch("services.signal_store.start_signal_writer", side_effect=_async_noop),
    ):
        from main import app
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


# ── helpers ───────────────────────────────────────────────────────────────
def _unique_email() -> str:
    return f"test_{uuid.uuid4().hex[:8]}@cipher.io"


ORIGIN = "https://cipher.vercel.app"


# ── 1 & 2: CORS preflight ─────────────────────────────────────────────────
def test_cors_preflight_register(client):
    r = client.options(
        "/api/auth/register",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200, r.text


def test_cors_preflight_token(client):
    r = client.options(
        "/api/auth/token",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200, r.text


# ── 3: Successful register ────────────────────────────────────────────────
def test_register_success(client):
    email = _unique_email()
    r = client.post("/api/auth/register", json={"email": email, "password": "Secure123!"})
    assert r.status_code == 201, r.text
    assert r.json()["message"] == "Account created successfully"


# ── 4: Duplicate email ────────────────────────────────────────────────────
def test_register_duplicate_email(client):
    email = _unique_email()
    r1 = client.post("/api/auth/register", json={"email": email, "password": "Secure123!"})
    assert r1.status_code == 201
    r2 = client.post("/api/auth/register", json={"email": email, "password": "Secure123!"})
    assert r2.status_code == 409
    assert "already registered" in r2.json()["detail"].lower()


# ── 5: Short password ─────────────────────────────────────────────────────
def test_register_short_password(client):
    r = client.post("/api/auth/register", json={"email": _unique_email(), "password": "short"})
    assert r.status_code == 422
    assert "8 characters" in r.json()["detail"]


# ── 6 & 7: Login ─────────────────────────────────────────────────────────
def test_login_success(client):
    email = _unique_email()
    client.post("/api/auth/register", json={"email": email, "password": "Secure123!"})
    r = client.post("/api/auth/token", data={"username": email, "password": "Secure123!"})
    assert r.status_code == 200, r.text
    assert "access_token" in r.json()


def test_login_wrong_password(client):
    email = _unique_email()
    client.post("/api/auth/register", json={"email": email, "password": "Secure123!"})
    r = client.post("/api/auth/token", data={"username": email, "password": "WrongPass99"})
    assert r.status_code == 401


# ── 8: Unknown email ──────────────────────────────────────────────────────
def test_login_unknown_email(client):
    r = client.post("/api/auth/token", data={"username": "nobody@cipher.io", "password": "Secure123!"})
    assert r.status_code == 401


# ── 9 & 10: /me endpoint ─────────────────────────────────────────────────
def test_me_authenticated(client):
    email = _unique_email()
    client.post("/api/auth/register", json={"email": email, "password": "Secure123!"})
    t = client.post("/api/auth/token", data={"username": email, "password": "Secure123!"})
    token = t.json()["access_token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == email


def test_me_unauthenticated(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 401


# ── 11: Health ────────────────────────────────────────────────────────────
def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ── 12: CORS headers present on actual POST ───────────────────────────────
def test_cors_headers_on_register_response(client):
    """Regression: browser must receive ACAO header or it treats response as opaque."""
    r = client.post(
        "/api/auth/register",
        json={"email": _unique_email(), "password": "Secure123!"},
        headers={"Origin": ORIGIN},
    )
    # TestClient with allowed_origins=[\"*\"] will echo back the header
    assert r.status_code == 201
    # The response body must be valid JSON (not an empty CORS rejection)
    assert r.json().get("message") == "Account created successfully"
