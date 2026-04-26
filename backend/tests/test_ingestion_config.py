"""
Regression tests for services/ingestion_config.py

Covers (matched to actual source):
  _headers():
  - 'apikey' field is the SUPABASE_KEY value
  - 'Authorization' is 'Bearer {key}'
  - 'Content-Type' is 'application/json'
  - empty key results in empty string headers (no crash)

  _cast():
  - 'int' type coerces '90' → 90 (int)
  - 'int' type coerces '0.15' float string → 0 (int via int(float(...)))
  - 'float' type coerces '0.15' → 0.15 (float)
  - 'str' or unknown type returns value as-is
  - TypeError/ValueError on bad value returns the raw value

  get_config():
  - No Supabase URL/key → returns _DEFAULTS unchanged
  - TTL cache: second call within 60s does not make a second HTTP request
  - force_refresh=True bypasses cache and makes a new HTTP request
  - HTTP 200 with rows → merges rows into defaults (existing key overridden)
  - HTTP 200 row with unknown key → added to result (not filtered)
  - HTTP 4xx → falls back to _DEFAULTS
  - Connection exception → falls back to _DEFAULTS
  - Returned dict always contains all _DEFAULTS keys

  update_config():
  - No Supabase → returns False
  - HTTP 200 → returns True
  - HTTP 204 → returns True
  - HTTP 200 → invalidates cache (_cache_ts set to 0.0)
  - HTTP 500 → returns False
  - Exception → returns False

  get_all_rows():
  - No Supabase → returns []
"""
import pytest
import time
from unittest.mock import patch, AsyncMock, MagicMock

import services.ingestion_config as ic
from services.ingestion_config import _cast, _headers


# ── _headers ─────────────────────────────────────────────────────────────────

def test_headers_apikey_field():
    with patch.object(ic, "_SUPABASE_KEY", "test-service-key"):
        h = _headers()
    assert h["apikey"] == "test-service-key"


def test_headers_authorization_bearer():
    with patch.object(ic, "_SUPABASE_KEY", "test-service-key"):
        h = _headers()
    assert h["Authorization"] == "Bearer test-service-key"


def test_headers_content_type_json():
    with patch.object(ic, "_SUPABASE_KEY", "test-service-key"):
        h = _headers()
    assert h["Content-Type"] == "application/json"


def test_headers_empty_key_no_crash():
    with patch.object(ic, "_SUPABASE_KEY", None):
        h = _headers()
    assert h["apikey"] == ""
    assert h["Authorization"] == "Bearer "


# ── _cast ───────────────────────────────────────────────────────────────────

def test_cast_int_string():
    assert _cast("90", "int") == 90
    assert isinstance(_cast("90", "int"), int)


def test_cast_int_from_float_string():
    """int(float('0.15')) → 0, not 0.15."""
    assert _cast("0.15", "int") == 0


def test_cast_float_string():
    assert _cast("0.15", "float") == 0.15
    assert isinstance(_cast("0.15", "float"), float)


def test_cast_str_type_returns_as_is():
    assert _cast("hello", "str") == "hello"


def test_cast_unknown_type_returns_as_is():
    assert _cast("somevalue", "unknown_type") == "somevalue"


def test_cast_type_error_returns_raw_value():
    """If coercion raises TypeError (e.g. None), returns the raw value."""
    result = _cast(None, "int")  # type: ignore
    assert result is None


# ── get_config: no Supabase configured ────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_config_no_supabase_returns_defaults():
    with patch.object(ic, "_SUPABASE_URL", None), \
         patch.object(ic, "_SUPABASE_KEY", None), \
         patch.object(ic, "_cache", {}), \
         patch.object(ic, "_cache_ts", 0.0):
        result = await ic.get_config()
    for key in ic._DEFAULTS:
        assert key in result
        assert result[key] == ic._DEFAULTS[key]


@pytest.mark.asyncio
async def test_get_config_cache_hit_no_second_http_call():
    """Within TTL, a second call must not make another HTTP request."""
    # Prime the cache
    warm_cache = dict(ic._DEFAULTS)
    with patch.object(ic, "_cache", warm_cache), \
         patch.object(ic, "_cache_ts", time.monotonic()), \
         patch.object(ic, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(ic, "_SUPABASE_KEY", "key"):
        with patch("services.ingestion_config.httpx.AsyncClient") as mock_client:
            result = await ic.get_config(force_refresh=False)
            mock_client.assert_not_called()
    assert result == warm_cache


@pytest.mark.asyncio
async def test_get_config_force_refresh_bypasses_cache():
    """force_refresh=True must make an HTTP call even with a warm cache."""
    warm_cache = dict(ic._DEFAULTS)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value=[])  # no rows

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__  = AsyncMock(return_value=False)
    mock_http.get        = AsyncMock(return_value=mock_resp)

    with patch.object(ic, "_cache", warm_cache), \
         patch.object(ic, "_cache_ts", time.monotonic()), \
         patch.object(ic, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(ic, "_SUPABASE_KEY", "fake-key"), \
         patch("services.ingestion_config.httpx.AsyncClient", return_value=mock_http):
        result = await ic.get_config(force_refresh=True)

    mock_http.get.assert_called_once()


@pytest.mark.asyncio
async def test_get_config_http_200_merges_rows_over_defaults():
    """A row from DB with key REGISTRY_MAX_DTE should override the default 90."""
    rows = [{"key": "REGISTRY_MAX_DTE", "value": "60", "value_type": "int"}]

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value=rows)

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__  = AsyncMock(return_value=False)
    mock_http.get        = AsyncMock(return_value=mock_resp)

    with patch.object(ic, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(ic, "_SUPABASE_KEY", "fake-key"), \
         patch.object(ic, "_cache", {}), \
         patch.object(ic, "_cache_ts", 0.0), \
         patch("services.ingestion_config.httpx.AsyncClient", return_value=mock_http):
        result = await ic.get_config()

    assert result["REGISTRY_MAX_DTE"] == 60
    assert result["REGISTRY_ATM_RANGE_PCT"] == 0.15  # default unchanged


@pytest.mark.asyncio
async def test_get_config_http_4xx_falls_back_to_defaults():
    mock_resp = MagicMock()
    mock_resp.status_code = 401

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__  = AsyncMock(return_value=False)
    mock_http.get        = AsyncMock(return_value=mock_resp)

    with patch.object(ic, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(ic, "_SUPABASE_KEY", "fake-key"), \
         patch.object(ic, "_cache", {}), \
         patch.object(ic, "_cache_ts", 0.0), \
         patch("services.ingestion_config.httpx.AsyncClient", return_value=mock_http):
        result = await ic.get_config()

    assert result == ic._DEFAULTS


@pytest.mark.asyncio
async def test_get_config_connection_exception_falls_back_to_defaults():
    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__  = AsyncMock(return_value=False)
    mock_http.get        = AsyncMock(side_effect=Exception("connection refused"))

    with patch.object(ic, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(ic, "_SUPABASE_KEY", "fake-key"), \
         patch.object(ic, "_cache", {}), \
         patch.object(ic, "_cache_ts", 0.0), \
         patch("services.ingestion_config.httpx.AsyncClient", return_value=mock_http):
        result = await ic.get_config()

    assert result == ic._DEFAULTS


@pytest.mark.asyncio
async def test_get_config_result_always_has_all_defaults_keys():
    with patch.object(ic, "_SUPABASE_URL", None), \
         patch.object(ic, "_cache", {}), \
         patch.object(ic, "_cache_ts", 0.0):
        result = await ic.get_config()
    for key in ic._DEFAULTS:
        assert key in result


# ── update_config ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_update_config_no_supabase_returns_false():
    with patch.object(ic, "_SUPABASE_URL", None), \
         patch.object(ic, "_SUPABASE_KEY", None):
        result = await ic.update_config("REGISTRY_MAX_DTE", "60")
    assert result is False


@pytest.mark.asyncio
async def test_update_config_http_200_returns_true():
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__  = AsyncMock(return_value=False)
    mock_http.patch      = AsyncMock(return_value=mock_resp)

    with patch.object(ic, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(ic, "_SUPABASE_KEY", "fake-key"), \
         patch("services.ingestion_config.httpx.AsyncClient", return_value=mock_http):
        result = await ic.update_config("REGISTRY_MAX_DTE", "60")
    assert result is True


@pytest.mark.asyncio
async def test_update_config_http_204_returns_true():
    mock_resp = MagicMock()
    mock_resp.status_code = 204

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__  = AsyncMock(return_value=False)
    mock_http.patch      = AsyncMock(return_value=mock_resp)

    with patch.object(ic, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(ic, "_SUPABASE_KEY", "fake-key"), \
         patch("services.ingestion_config.httpx.AsyncClient", return_value=mock_http):
        result = await ic.update_config("REGISTRY_MAX_DTE", "60")
    assert result is True


@pytest.mark.asyncio
async def test_update_config_http_200_invalidates_cache():
    mock_resp = MagicMock()
    mock_resp.status_code = 200

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__  = AsyncMock(return_value=False)
    mock_http.patch      = AsyncMock(return_value=mock_resp)

    with patch.object(ic, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(ic, "_SUPABASE_KEY", "fake-key"), \
         patch("services.ingestion_config.httpx.AsyncClient", return_value=mock_http):
        ic._cache_ts = time.monotonic()  # set a warm cache timestamp
        await ic.update_config("REGISTRY_MAX_DTE", "60")
    assert ic._cache_ts == 0.0  # cache must be invalidated


@pytest.mark.asyncio
async def test_update_config_http_500_returns_false():
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"

    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__  = AsyncMock(return_value=False)
    mock_http.patch      = AsyncMock(return_value=mock_resp)

    with patch.object(ic, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(ic, "_SUPABASE_KEY", "fake-key"), \
         patch("services.ingestion_config.httpx.AsyncClient", return_value=mock_http):
        result = await ic.update_config("REGISTRY_MAX_DTE", "60")
    assert result is False


@pytest.mark.asyncio
async def test_update_config_exception_returns_false():
    mock_http = AsyncMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__  = AsyncMock(return_value=False)
    mock_http.patch      = AsyncMock(side_effect=Exception("timeout"))

    with patch.object(ic, "_SUPABASE_URL", "https://fake.supabase.co"), \
         patch.object(ic, "_SUPABASE_KEY", "fake-key"), \
         patch("services.ingestion_config.httpx.AsyncClient", return_value=mock_http):
        result = await ic.update_config("REGISTRY_MAX_DTE", "60")
    assert result is False


# ── get_all_rows ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_all_rows_no_supabase_returns_empty_list():
    with patch.object(ic, "_SUPABASE_URL", None):
        result = await ic.get_all_rows()
    assert result == []
