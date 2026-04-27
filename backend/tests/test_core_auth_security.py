"""
P1 security-boundary tests for core/auth.py.

Covers the previously-untested paths:
  - get_current_user: malformed JWT → 401
  - get_current_user: expired JWT → 401
  - get_current_user: missing 'sub' claim → 401
  - _fetch_role: Supabase exception → graceful 'user' fallback
  - _fetch_role: result.data[0] missing 'role' key → 'user' fallback
  - _fetch_role: no rows returned → 'user' fallback
  - _fetch_role: missing SUPABASE_URL/SERVICE_KEY → 'user', no Supabase call
"""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, AsyncMock
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from jose import jwt as jose_jwt

from config import settings
from core.auth import (
    get_current_user,
    create_access_token,
    TokenData,
    _fetch_role,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app():
    app = FastAPI()

    @app.get("/probe")
    async def probe(user: TokenData = Depends(get_current_user)):
        return {"email": user.email, "role": user.role}

    return app


@pytest.fixture
def client():
    return TestClient(_make_app(), raise_server_exceptions=False)


def _valid_token(email: str = "user@cipher.app") -> str:
    return create_access_token({"sub": email})


def _expired_token(email: str = "user@cipher.app") -> str:
    payload = {
        "sub": email,
        "exp": datetime.now(timezone.utc) - timedelta(seconds=10),
    }
    return jose_jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


# ---------------------------------------------------------------------------
# get_current_user — JWT error paths
# ---------------------------------------------------------------------------

def test_get_current_user_no_token_returns_401(client):
    resp = client.get("/probe")
    assert resp.status_code == 401


def test_get_current_user_malformed_token_returns_401(client):
    resp = client.get("/probe", headers={"Authorization": "Bearer not.a.valid.jwt"})
    assert resp.status_code == 401


def test_get_current_user_expired_token_returns_401(client):
    token = _expired_token()
    resp = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_get_current_user_missing_sub_returns_401(client):
    # Valid signature but no 'sub' claim
    payload = {"exp": datetime.now(timezone.utc) + timedelta(minutes=5)}
    token = jose_jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    resp = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_get_current_user_valid_token_returns_200(client):
    token = _valid_token()
    with patch("core.auth._fetch_role", new=AsyncMock(return_value="user")):
        resp = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "user@cipher.app"


def test_get_current_user_propagates_role_from_fetch_role(client):
    token = _valid_token("admin@cipher.app")
    with patch("core.auth._fetch_role", new=AsyncMock(return_value="admin")):
        resp = client.get("/probe", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"


# ---------------------------------------------------------------------------
# _fetch_role — graceful fallbacks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_role_returns_user_when_creds_missing():
    with patch.object(settings, "SUPABASE_URL", ""), \
         patch.object(settings, "SUPABASE_SERVICE_KEY", ""):
        role = await _fetch_role("someone@cipher.app")
    assert role == "user"


@pytest.mark.asyncio
async def test_fetch_role_no_supabase_call_when_creds_missing():
    # create_client is imported inside _fetch_role body from supabase,
    # so we patch the source module "supabase.create_client".
    with patch.object(settings, "SUPABASE_URL", ""), \
         patch.object(settings, "SUPABASE_SERVICE_KEY", ""), \
         patch("supabase.create_client") as mock_create:
        await _fetch_role("someone@cipher.app")
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_role_supabase_exception_returns_user():
    mock_client = MagicMock()
    mock_client.table.side_effect = Exception("DB exploded")
    with patch.object(settings, "SUPABASE_URL", "http://fake"), \
         patch.object(settings, "SUPABASE_SERVICE_KEY", "fake-key"), \
         patch("supabase.create_client", return_value=mock_client):
        role = await _fetch_role("user@cipher.app")
    assert role == "user"


@pytest.mark.asyncio
async def test_fetch_role_no_rows_returns_user():
    mock_client = MagicMock()
    q = MagicMock()
    q.select.return_value = q
    q.eq.return_value = q
    q.limit.return_value = q
    q.execute.return_value = MagicMock(data=[])
    mock_client.table.return_value = q
    with patch.object(settings, "SUPABASE_URL", "http://fake"), \
         patch.object(settings, "SUPABASE_SERVICE_KEY", "fake-key"), \
         patch("supabase.create_client", return_value=mock_client):
        role = await _fetch_role("user@cipher.app")
    assert role == "user"


@pytest.mark.asyncio
async def test_fetch_role_row_missing_role_key_returns_user():
    mock_client = MagicMock()
    q = MagicMock()
    q.select.return_value = q
    q.eq.return_value = q
    q.limit.return_value = q
    # Row exists but has no 'role' key
    q.execute.return_value = MagicMock(data=[{"email": "user@cipher.app"}])
    mock_client.table.return_value = q
    with patch.object(settings, "SUPABASE_URL", "http://fake"), \
         patch.object(settings, "SUPABASE_SERVICE_KEY", "fake-key"), \
         patch("supabase.create_client", return_value=mock_client):
        role = await _fetch_role("user@cipher.app")
    assert role == "user"


@pytest.mark.asyncio
async def test_fetch_role_returns_correct_role_from_db():
    mock_client = MagicMock()
    q = MagicMock()
    q.select.return_value = q
    q.eq.return_value = q
    q.limit.return_value = q
    q.execute.return_value = MagicMock(data=[{"role": "admin"}])
    mock_client.table.return_value = q
    with patch.object(settings, "SUPABASE_URL", "http://fake"), \
         patch.object(settings, "SUPABASE_SERVICE_KEY", "fake-key"), \
         patch("supabase.create_client", return_value=mock_client):
        role = await _fetch_role("admin@cipher.app")
    assert role == "admin"
