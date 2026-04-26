"""
Regression tests for routers/auth.py and core/auth.py

Strategy:
  - Use FastAPI TestClient with the full auth router mounted.
  - In-memory fallback path tested (no Supabase configured).
  - JWT encode/decode tested directly via create_access_token.

Covers:
  POST /api/auth/register (in-memory fallback):
  - Password < 8 chars → 422
  - Successful registration → 201 + {message: 'Account created successfully'}
  - Duplicate email → 409

  OPTIONS /api/auth/register → 200
  OPTIONS /api/auth/token → 200

  POST /api/auth/token (in-memory fallback):
  - Wrong password → 401
  - Correct password → 200 + {access_token, token_type: 'bearer'}
  - token_type is always 'bearer'
  - access_token is a non-empty string

  GET /api/auth/me:
  - No Authorization header → 401
  - Malformed token → 401
  - Expired token → 401
  - Valid token, Supabase not configured → 200 + {email, role: 'user'}

  core/auth.py utilities:
  - create_access_token: payload sub round-trips through JWT decode
  - Expired token raises exception on decode
  - hash_password + verify_password: correct password verifies True
  - Wrong password verifies False
  - Hash is never the plain-text password
"""
import pytest
from datetime import timedelta
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch
from jose import jwt, JWTError

from routers.auth import router
from core.auth import (
    create_access_token,
    hash_password,
    verify_password,
    TokenData,
)
from config import settings


# ---------------------------------------------------------------------------
# App fixture — Supabase patched out so only in-memory fallback runs
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    # Patch both Supabase factory functions to return None → forces in-memory path
    with patch("routers.auth._supabase_admin", return_value=None), \
         patch("routers.auth._supabase_client", return_value=None):
        with TestClient(app) as c:
            yield c


# ---------------------------------------------------------------------------
# POST /api/auth/register
# ---------------------------------------------------------------------------

def test_register_short_password_returns_422(client):
    resp = client.post("/api/auth/register", json={"email": "a@b.com", "password": "short"})
    assert resp.status_code == 422


def test_register_success_returns_201(client):
    resp = client.post("/api/auth/register", json={"email": "new@cipher.app", "password": "securepass1"})
    assert resp.status_code == 201
    assert resp.json()["message"] == "Account created successfully"


def test_register_duplicate_email_returns_409(client):
    client.post("/api/auth/register", json={"email": "dup@cipher.app", "password": "securepass1"})
    resp = client.post("/api/auth/register", json={"email": "dup@cipher.app", "password": "securepass1"})
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# OPTIONS preflight
# ---------------------------------------------------------------------------

def test_options_register_returns_200(client):
    resp = client.options("/api/auth/register")
    assert resp.status_code == 200


def test_options_token_returns_200(client):
    resp = client.options("/api/auth/token")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST /api/auth/token
# ---------------------------------------------------------------------------

def _register_and_login(client, email: str, password: str):
    client.post("/api/auth/register", json={"email": email, "password": password})
    return client.post(
        "/api/auth/token",
        data={"username": email, "password": password},
    )


def test_login_wrong_password_returns_401(client):
    client.post("/api/auth/register", json={"email": "auth@cipher.app", "password": "correct_pass1"})
    resp = client.post(
        "/api/auth/token",
        data={"username": "auth@cipher.app", "password": "wrong_password"},
    )
    assert resp.status_code == 401


def test_login_correct_password_returns_200(client):
    resp = _register_and_login(client, "login@cipher.app", "mypassword1")
    assert resp.status_code == 200


def test_login_returns_bearer_token_type(client):
    resp = _register_and_login(client, "bearer@cipher.app", "mypassword1")
    assert resp.json()["token_type"] == "bearer"


def test_login_returns_non_empty_access_token(client):
    resp = _register_and_login(client, "token@cipher.app", "mypassword1")
    token = resp.json().get("access_token", "")
    assert isinstance(token, str) and len(token) > 10


# ---------------------------------------------------------------------------
# GET /api/auth/me
# ---------------------------------------------------------------------------

def test_me_no_auth_returns_401(client):
    resp = client.get("/api/auth/me")
    assert resp.status_code == 401


def test_me_malformed_token_returns_401(client):
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.real.jwt"})
    assert resp.status_code == 401


def test_me_expired_token_returns_401(client):
    expired_token = create_access_token(
        {"sub": "expired@cipher.app"},
        expires_delta=timedelta(seconds=-1),
    )
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired_token}"})
    assert resp.status_code == 401


def test_me_valid_token_returns_200(client):
    """Valid JWT, Supabase not configured → role defaults to 'user'."""
    token = create_access_token({"sub": "valid@cipher.app"})
    with patch("core.auth._fetch_role", return_value="user"):
        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "valid@cipher.app"
    assert "role" in body


def test_me_role_defaults_to_user_when_no_supabase(client):
    token = create_access_token({"sub": "norole@cipher.app"})
    with patch("core.auth.settings") as ms:
        ms.SUPABASE_URL = ""
        ms.SUPABASE_SERVICE_KEY = ""
        ms.SECRET_KEY = settings.SECRET_KEY
        ms.ALGORITHM  = settings.ALGORITHM
        resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "user"


# ---------------------------------------------------------------------------
# core/auth.py utilities
# ---------------------------------------------------------------------------

def test_create_access_token_sub_round_trips():
    token = create_access_token({"sub": "roundtrip@cipher.app"})
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    assert payload["sub"] == "roundtrip@cipher.app"


def test_expired_token_raises_jwterror():
    token = create_access_token(
        {"sub": "exp@cipher.app"},
        expires_delta=timedelta(seconds=-1),
    )
    with pytest.raises(JWTError):
        jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])


def test_hash_password_verify_correct():
    pw = "myS3cur3Pass!"
    h  = hash_password(pw)
    assert verify_password(pw, h) is True


def test_hash_password_verify_wrong():
    h = hash_password("correct")
    assert verify_password("wrong", h) is False


def test_hash_is_not_plaintext():
    pw = "plaintext_pass"
    assert hash_password(pw) != pw
