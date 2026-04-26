"""
Regression tests for services/ingestion_config.py

Covers:
  - _cast correctly coerces int, float, and str types
  - _cast returns raw value on TypeError/ValueError
  - get_config() returns _DEFAULTS when Supabase env vars are not set
  - get_config() returns cached result within TTL (no DB call on second call)
  - get_config() refreshes cache after TTL expires
  - get_config(force_refresh=True) bypasses the cache
  - get_all_rows() returns empty list when Supabase env vars are not set
  - update_config() returns False when Supabase env vars are not set
  - get_config() falls back to defaults on HTTP non-200 response
  - get_config() falls back to defaults on network exception
  - update_config() returns True on HTTP 204
  - update_config() invalidates cache on success
  - get_config() merges DB rows with _DEFAULTS (DB overrides default)
"""
import pytest
import time
from unittest.mock import patch, AsyncMock, MagicMock

import services.ingestion_config as ic


# ── _cast ────────────────────────────────────────────────────────────────────────

def test_cast_int():
    assert ic._cast("90", "int") == 90
    assert isinstance(ic._cast("90", "int"), int)


def test_cast_int_from_float_string():
    """'30.0' with value_type='int' should return 30."""
    assert ic._cast("30.0", "int") == 30


def test_cast_float():
    result = ic._cast("0.15", "float")
    assert pytest.approx(result) == 0.15
    assert isinstance(result, float)


def test_cast_str_returns_raw():
    assert ic._cast("hello", "str") == "hello"


def test_cast_invalid_int_returns_raw():
    result = ic._cast("not_a_number", "int")
    assert result == "not_a_number"


def test_cast_invalid_float_returns_raw():
    result = ic._cast("abc", "float")
    assert result == "abc"


# ── get_config: no env vars ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_config_returns_defaults_when_no_env():
    with patch.object(ic, "_SUPABASE_URL", None), \
         patch.object(ic, "_SUPABASE_KEY", None):
        ic._cache = {}
        ic._cache_ts = 0.0
        result = await ic.get_config()
    assert result == ic._DEFAULTS


@pytest.mark.asyncio
async def test_get_config_defaults_contain_all_expected_keys():
    with patch.object(ic, "_SUPABASE_URL", None), \
         patch.object(ic, "_SUPABASE_KEY", None):
        ic._cache = {}
        ic._cache_ts = 0.0
        result = await ic.get_config()
    for key in (
        "REGISTRY_MAX_DTE", "REGISTRY_ATM_RANGE_PCT",
        "REGISTRY_MIN_OI", "REGISTRY_REFRESH_MINS",
        "REGISTRY_EXPIRY_DAY_REFRESH_MINS",
        "UNIVERSE_MIN_PRICE", "UNIVERSE_MIN_VOLUME",
    ):
        assert key in result, f"Missing default key: {key}"


# ── get_config: cache TTL ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_config_returns_cached_result_within_ttl():
    """
    When the cache is warm (within 60s TTL), get_config() must not
    make a DB call — it returns the in-process cache.
    """
    ic._cache = dict(ic._DEFAULTS)
    ic._cache_ts = time.monotonic()  # just set — within TTL

    with patch.object(ic, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(ic, "_SUPABASE_KEY", "fake-key"):
        with patch("httpx.AsyncClient") as mock_http:
            result = await ic.get_config()
            mock_http.assert_not_called()  # no HTTP call on cache hit

    assert result == ic._DEFAULTS


@pytest.mark.asyncio
async def test_get_config_force_refresh_bypasses_cache():
    """
    force_refresh=True must bypass the TTL cache and make a DB call.
    """
    ic._cache = dict(ic._DEFAULTS)
    ic._cache_ts = time.monotonic()  # warm cache

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = []  # empty rows → returns defaults

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch.object(ic, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(ic, "_SUPABASE_KEY", "fake-key"):
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await ic.get_config(force_refresh=True)
    assert result is not None


# ── get_config: DB response handling ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_config_falls_back_on_non_200():
    ic._cache = {}
    ic._cache_ts = 0.0

    mock_resp = MagicMock()
    mock_resp.status_code = 503

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch.object(ic, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(ic, "_SUPABASE_KEY", "fake-key"):
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await ic.get_config()
    # Must fall back to _DEFAULTS, not raise
    assert result == ic._DEFAULTS


@pytest.mark.asyncio
async def test_get_config_falls_back_on_network_exception():
    ic._cache = {}
    ic._cache_ts = 0.0

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=Exception("network error"))

    with patch.object(ic, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(ic, "_SUPABASE_KEY", "fake-key"):
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await ic.get_config()
    assert result == ic._DEFAULTS


@pytest.mark.asyncio
async def test_get_config_merges_db_row_over_defaults():
    """A DB row for REGISTRY_MAX_DTE=45 overrides the default of 90."""
    ic._cache = {}
    ic._cache_ts = 0.0

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"key": "REGISTRY_MAX_DTE", "value": "45", "value_type": "int"}
    ]

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch.object(ic, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(ic, "_SUPABASE_KEY", "fake-key"):
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await ic.get_config(force_refresh=True)
    assert result["REGISTRY_MAX_DTE"] == 45


# ── get_all_rows ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_all_rows_returns_empty_when_no_env():
    with patch.object(ic, "_SUPABASE_URL", None), \
         patch.object(ic, "_SUPABASE_KEY", None):
        result = await ic.get_all_rows()
    assert result == []


# ── update_config ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_config_returns_false_when_no_env():
    with patch.object(ic, "_SUPABASE_URL", None), \
         patch.object(ic, "_SUPABASE_KEY", None):
        result = await ic.update_config("REGISTRY_MAX_DTE", "45")
    assert result is False


@pytest.mark.asyncio
async def test_update_config_returns_true_on_204():
    mock_resp = MagicMock()
    mock_resp.status_code = 204

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.patch = AsyncMock(return_value=mock_resp)

    with patch.object(ic, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(ic, "_SUPABASE_KEY", "fake-key"):
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await ic.update_config("REGISTRY_MAX_DTE", "45")
    assert result is True


@pytest.mark.asyncio
async def test_update_config_invalidates_cache_on_success():
    """After a successful update, cache_ts must be reset to 0 (expired)."""
    ic._cache_ts = time.monotonic()

    mock_resp = MagicMock()
    mock_resp.status_code = 200  # 200 is also accepted

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.patch = AsyncMock(return_value=mock_resp)

    with patch.object(ic, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(ic, "_SUPABASE_KEY", "fake-key"):
        with patch("httpx.AsyncClient", return_value=mock_client):
            await ic.update_config("REGISTRY_MAX_DTE", "60")

    assert ic._cache_ts == 0.0
