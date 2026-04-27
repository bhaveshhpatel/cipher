"""
tests/test_apex_gate_router.py

Unit tests for routers/apex_gate.py.
Tests GET and PATCH /api/apex/gate-config with admin auth.
"""
import pytest
from unittest.mock import patch, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers.apex_gate import router
from signals import signal_gate
from signals.signal_gate import reset_stats, reset_aggression_override


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    app = FastAPI()
    # Override require_admin to always pass
    from core.auth import require_admin
    app.dependency_overrides[require_admin] = lambda: "admin@test.com"
    app.include_router(router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean():
    reset_stats()
    reset_aggression_override()
    yield
    reset_stats()
    reset_aggression_override()


# ---------------------------------------------------------------------------
# GET /api/apex/gate-config
# ---------------------------------------------------------------------------

def test_get_config_default(client):
    r = client.get("/api/apex/gate-config")
    assert r.status_code == 200
    body = r.json()
    assert body["hard_reject"] is False
    assert body["source"] == "env"
    assert "max_aggression_penalty" in body
    assert "flat_aggression_penalty" in body
    assert "stats" in body


def test_get_config_after_override(client):
    signal_gate.set_aggression_hard_reject(True)
    r = client.get("/api/apex/gate-config")
    body = r.json()
    assert body["hard_reject"] is True
    assert body["source"] == "override"


# ---------------------------------------------------------------------------
# PATCH /api/apex/gate-config
# ---------------------------------------------------------------------------

def test_patch_sets_hard_reject_true(client):
    r = client.patch("/api/apex/gate-config", json={"hard_reject": True})
    assert r.status_code == 200
    body = r.json()
    assert body["hard_reject"] is True
    assert body["source"] == "override"
    assert signal_gate.get_aggression_hard_reject() is True


def test_patch_sets_hard_reject_false(client):
    signal_gate.set_aggression_hard_reject(True)
    r = client.patch("/api/apex/gate-config", json={"hard_reject": False})
    assert r.status_code == 200
    assert r.json()["hard_reject"] is False


def test_patch_returns_updated_stats(client):
    r = client.patch("/api/apex/gate-config", json={"hard_reject": True})
    assert "stats" in r.json()
    assert r.json()["stats"]["aggression_hard_reject"] is True


def test_patch_idempotent(client):
    client.patch("/api/apex/gate-config", json={"hard_reject": True})
    r = client.patch("/api/apex/gate-config", json={"hard_reject": True})
    assert r.status_code == 200
    assert r.json()["hard_reject"] is True
