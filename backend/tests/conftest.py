"""
conftest.py — shared pytest fixtures.
"""
import sys
import os
import pytest

# ---------------------------------------------------------------------------
# Ensure the backend package root is on sys.path so all source modules
# (core, signals, services, parsers, utils, simulation, etc.) are importable
# from every test file, regardless of where pytest is invoked from.
# ---------------------------------------------------------------------------
_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)


@pytest.fixture(autouse=True)
def _reset_supabase_env(monkeypatch):
    """Ensure Supabase env vars are absent unless a test sets them."""
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_KEY", raising=False)
    yield
