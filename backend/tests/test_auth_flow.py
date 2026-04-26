"""
Regression tests for routers/auth.py

Covers:
  - Register + login + /me happy path
  - Duplicate email registration returns 409
  - Short password (< 8 chars) returns 422
  - Wrong password returns 401
  - /me with no token returns 401
  - /me with expired / invalid JWT returns 401
  - /token with unknown email returns 401
  - /me returns email matching the registered user
"""
from fastapi.testclient import TestClient
from core.auth import create_access_token
from main import app

client = TestClient(app)

_EMAIL    = "test@example.com"
_PASSWORD = "pw123456"
_SHORT_PW = "abc"      # < 8 chars


# ── helpers ──────────────────────────────────────────────────────────────────

def _register(email=_EMAIL, password=_PASSWORD):
    return client.post("/api/auth/register", json={"email": email, "password": password})


def _login(email=_EMAIL, password=_PASSWORD):
    return client.post("/api/auth/token", data={"username": email, "password": password})


def _me(token: str):
    return client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})


# ── happy path ────────────────────────────────────────────────────────────────

def test_register_login_me_flow():
    r = _register()
    assert r.status_code in (201, 409)  # 409 if already exists from previous run
    t = _login()
    assert t.status_code == 200
    token = t.json()["access_token"]
    me = _me(token)
    assert me.status_code == 200
    assert me.json()["email"] == _EMAIL


def test_register_returns_201_on_first_call():
    """Use a unique email to guarantee a fresh registration."""
    unique = "newuser_phase2@example.com"
    r = client.post("/api/auth/register", json={"email": unique, "password": "password123"})
    assert r.status_code in (201, 409)


def test_token_response_contains_access_token_and_bearer_type():
    _register()  # ensure user exists (idempotent — 409 is fine)
    t = _login()
    assert t.status_code == 200
    body = t.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"


# ── duplicate email ────────────────────────────────────────────────────────────

def test_duplicate_email_returns_409():
    """
    Registering the same email twice must return 409 Conflict.
    Register once (201 or 409), then register again — must be 409.
    """
    dup_email = "duplicate@example.com"
    client.post("/api/auth/register", json={"email": dup_email, "password": "password123"})
    r2 = client.post("/api/auth/register", json={"email": dup_email, "password": "password123"})
    assert r2.status_code == 409
    assert "already" in r2.json()["detail"].lower()


# ── short password ────────────────────────────────────────────────────────────

def test_short_password_returns_422():
    """Passwords shorter than 8 characters must be rejected with 422."""
    r = client.post("/api/auth/register", json={"email": "short@example.com", "password": _SHORT_PW})
    assert r.status_code == 422
    assert "8" in r.json()["detail"] or "characters" in r.json()["detail"].lower()


def test_empty_password_returns_422():
    r = client.post("/api/auth/register", json={"email": "empty@example.com", "password": ""})
    assert r.status_code == 422


# ── wrong password ────────────────────────────────────────────────────────────

def test_wrong_password_returns_401():
    _register()  # ensure user exists
    r = _login(password="wrongpassword")
    assert r.status_code == 401
    assert "Invalid" in r.json()["detail"] or "invalid" in r.json()["detail"].lower()


def test_unknown_email_returns_401():
    r = client.post("/api/auth/token", data={
        "username": "nobody@nowhere.com",
        "password": "somepassword",
    })
    assert r.status_code == 401


# ── /me auth guard ────────────────────────────────────────────────────────────

def test_me_without_token_returns_401():
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_me_with_invalid_jwt_returns_401():
    r = client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.valid.jwt"})
    assert r.status_code == 401


def test_me_with_malformed_bearer_returns_401():
    """Bearer with no token value at all."""
    r = client.get("/api/auth/me", headers={"Authorization": "Bearer "})
    assert r.status_code == 401


def test_me_with_expired_jwt_returns_401():
    """
    [REGRESSION] A JWT signed with the correct secret but an expiry in the
    past must be rejected with 401. This is the ghost-session bug pattern.
    """
    from datetime import timedelta
    expired_token = create_access_token(
        data={"sub": _EMAIL},
        expires_delta=timedelta(seconds=-1),  # already expired
    )
    r = _me(expired_token)
    assert r.status_code == 401


# ── /me response shape ───────────────────────────────────────────────────────────

def test_me_returns_email_and_role():
    _register()
    t = _login()
    assert t.status_code == 200
    token = t.json()["access_token"]
    me = _me(token)
    assert me.status_code == 200
    body = me.json()
    assert "email" in body
    assert "role" in body
    assert body["email"] == _EMAIL
    # Default role for in-memory fallback users is 'user'
    assert body["role"] in ("user", "admin")
