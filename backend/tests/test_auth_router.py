"""
Regression tests for routers/auth.py
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock

from core.auth import get_current_user
from routers.auth import router
import routers.auth as auth_mod


def _make_app(auth_override=None):
    app = FastAPI()
    app.include_router(router)
    if auth_override:
        app.dependency_overrides[get_current_user] = auth_override
    return app


@pytest.fixture
def authed_client():
    from core.auth import TokenData
    async def _override():
        return TokenData(email="user@cipher.app", role="user")
    return TestClient(_make_app(_override))


@pytest.fixture
def raw_client():
    return TestClient(_make_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# /me endpoint
# ---------------------------------------------------------------------------

def test_me_without_token_returns_401(raw_client):
    resp = raw_client.get("/api/auth/me")
    assert resp.status_code == 401


def test_me_with_valid_token_returns_200(authed_client):
    resp = authed_client.get("/api/auth/me")
    assert resp.status_code == 200


def test_me_response_contains_email(authed_client):
    resp = authed_client.get("/api/auth/me")
    assert "email" in resp.json()


def test_me_response_contains_role(authed_client):
    resp = authed_client.get("/api/auth/me")
    assert "role" in resp.json()


# ---------------------------------------------------------------------------
# /login endpoint
# ---------------------------------------------------------------------------

def test_login_missing_body_returns_422(raw_client):
    resp = raw_client.post("/api/auth/login", json={})
    assert resp.status_code == 422


def test_login_invalid_credentials_returns_401(raw_client):
    with patch.object(auth_mod, "_authenticate_user",
                      new_callable=AsyncMock, return_value=None):
        resp = raw_client.post("/api/auth/login",
                               json={"email": "x@x.com", "password": "wrong"})
    assert resp.status_code == 401


def test_login_valid_credentials_returns_token(raw_client):
    fake_user = {"email": "user@cipher.app", "role": "user"}
    with patch.object(auth_mod, "_authenticate_user",
                      new_callable=AsyncMock, return_value=fake_user), \
         patch.object(auth_mod, "_create_access_token", return_value="tok123"):
        resp = raw_client.post("/api/auth/login",
                               json={"email": "user@cipher.app", "password": "pass"})
    assert resp.status_code == 200
    assert "access_token" in resp.json()


def test_login_response_has_token_type(raw_client):
    fake_user = {"email": "user@cipher.app", "role": "user"}
    with patch.object(auth_mod, "_authenticate_user",
                      new_callable=AsyncMock, return_value=fake_user), \
         patch.object(auth_mod, "_create_access_token", return_value="tok123"):
        resp = raw_client.post("/api/auth/login",
                               json={"email": "user@cipher.app", "password": "pass"})
    assert resp.json().get("token_type") == "bearer"
