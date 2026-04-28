"""
test_ingestion_config_rc3.py

100% coverage of the RC-3 fixes in ingestion_config:

  1. REGISTRY_BUILD_CONCURRENCY present in _DEFAULTS with correct type/value
  2. REGISTRY_MIN_OI raised from 0 → 1 in _DEFAULTS
  3. All 8 expected keys present in _DEFAULTS
  4. _cast() handles int/float/str correctly
  5. get_config() returns REGISTRY_BUILD_CONCURRENCY when DB returns it
  6. get_config() falls back to default when DB row missing
  7. validate_ingestion_config() returns [] when all keys present
  8. validate_ingestion_config() returns missing keys and logs WARNING
  9. validate_ingestion_config() returns [] when Supabase not configured
"""
import pytest
import importlib
from unittest.mock import AsyncMock, MagicMock, patch


def test_registry_build_concurrency_in_defaults():
    from services.ingestion_config import _DEFAULTS
    assert "REGISTRY_BUILD_CONCURRENCY" in _DEFAULTS
    assert _DEFAULTS["REGISTRY_BUILD_CONCURRENCY"] == 50


def test_registry_min_oi_raised_to_one():
    from services.ingestion_config import _DEFAULTS
    assert _DEFAULTS["REGISTRY_MIN_OI"] == 1


def test_all_expected_keys_in_defaults():
    from services.ingestion_config import _DEFAULTS, _EXPECTED_DB_KEYS
    expected = {
        "REGISTRY_MAX_DTE",
        "REGISTRY_ATM_RANGE_PCT",
        "REGISTRY_MIN_OI",
        "REGISTRY_REFRESH_MINS",
        "REGISTRY_EXPIRY_DAY_REFRESH_MINS",
        "REGISTRY_BUILD_CONCURRENCY",
        "UNIVERSE_MIN_PRICE",
        "UNIVERSE_MIN_VOLUME",
    }
    assert expected == set(_DEFAULTS.keys())
    assert expected == _EXPECTED_DB_KEYS


def test_cast_int():
    from services.ingestion_config import _cast
    assert _cast("50", "int") == 50
    assert isinstance(_cast("50", "int"), int)


def test_cast_float():
    from services.ingestion_config import _cast
    assert _cast("0.15", "float") == 0.15
    assert isinstance(_cast("0.15", "float"), float)


def test_cast_str_passthrough():
    from services.ingestion_config import _cast
    assert _cast("hello", "str") == "hello"


def test_cast_invalid_falls_back_to_raw():
    from services.ingestion_config import _cast
    result = _cast("not_a_number", "int")
    assert result == "not_a_number"


@pytest.mark.asyncio
async def test_get_config_returns_build_concurrency_from_db():
    from services import ingestion_config
    ingestion_config._cache = {}
    ingestion_config._cache_ts = 0.0

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [
        {"key": "REGISTRY_BUILD_CONCURRENCY", "value": "75", "value_type": "int"},
    ]

    with patch.dict(
        "os.environ",
        {"SUPABASE_URL": "https://fake.supabase.co", "SUPABASE_SERVICE_ROLE_KEY": "fake-key"},
    ):
        with patch("httpx.AsyncClient") as mock_http:
            mock_http.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            # Re-read env vars by patching module-level vars
            with patch.object(ingestion_config, "_SUPABASE_URL", "https://fake.supabase.co"):
                with patch.object(ingestion_config, "_SUPABASE_KEY", "fake-key"):
                    cfg = await ingestion_config.get_config(force_refresh=True)

    assert cfg["REGISTRY_BUILD_CONCURRENCY"] == 75
    assert isinstance(cfg["REGISTRY_BUILD_CONCURRENCY"], int)


@pytest.mark.asyncio
async def test_get_config_fallback_when_db_missing_key():
    """When DB returns empty list, defaults are used for all keys."""
    from services import ingestion_config
    ingestion_config._cache = {}
    ingestion_config._cache_ts = 0.0

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = []  # no rows in DB

    with patch.object(ingestion_config, "_SUPABASE_URL", "https://fake.supabase.co"):
        with patch.object(ingestion_config, "_SUPABASE_KEY", "fake-key"):
            with patch("httpx.AsyncClient") as mock_http:
                mock_http.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
                cfg = await ingestion_config.get_config(force_refresh=True)

    assert cfg["REGISTRY_BUILD_CONCURRENCY"] == 50
    assert cfg["REGISTRY_MIN_OI"] == 1


@pytest.mark.asyncio
async def test_validate_all_keys_present():
    from services import ingestion_config

    all_keys = list(ingestion_config._EXPECTED_DB_KEYS)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [{"key": k} for k in all_keys]

    with patch.object(ingestion_config, "_SUPABASE_URL", "https://fake.supabase.co"):
        with patch.object(ingestion_config, "_SUPABASE_KEY", "fake-key"):
            with patch("httpx.AsyncClient") as mock_http:
                mock_http.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
                missing = await ingestion_config.validate_ingestion_config()

    assert missing == []


@pytest.mark.asyncio
async def test_validate_returns_missing_keys_and_warns(caplog):
    import logging
    from services import ingestion_config

    present_keys = [k for k in ingestion_config._EXPECTED_DB_KEYS if k != "REGISTRY_BUILD_CONCURRENCY"]
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = [{"key": k} for k in present_keys]

    with patch.object(ingestion_config, "_SUPABASE_URL", "https://fake.supabase.co"):
        with patch.object(ingestion_config, "_SUPABASE_KEY", "fake-key"):
            with patch("httpx.AsyncClient") as mock_http:
                mock_http.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
                with caplog.at_level(logging.WARNING, logger="ingestion_config"):
                    missing = await ingestion_config.validate_ingestion_config()

    assert missing == ["REGISTRY_BUILD_CONCURRENCY"]
    assert any("REGISTRY_BUILD_CONCURRENCY" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_validate_returns_empty_when_not_configured():
    from services import ingestion_config

    with patch.object(ingestion_config, "_SUPABASE_URL", None):
        with patch.object(ingestion_config, "_SUPABASE_KEY", None):
            missing = await ingestion_config.validate_ingestion_config()

    assert missing == []
