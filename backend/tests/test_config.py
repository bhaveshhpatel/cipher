"""
Regression tests for config.py (pydantic-settings Settings class)

Covers:
  - Default values are correct for all fields
  - ALGORITHM is HS256
  - ACCESS_TOKEN_EXPIRE_MINUTES default is 1440 (24 h)
  - SECRET_KEY default is the dev placeholder string
  - SUPABASE_URL/KEY/SERVICE_KEY default to empty string (not None)
  - TRADIER_BASE_URL and STREAM_URL have correct defaults
  - SWARM_N_AGENTS defaults to 6
  - UNIVERSE_MIN_PRICE defaults to 1.0
  - UNIVERSE_MIN_VOLUME defaults to 500_000
  - REGISTRY_MAX_DTE defaults to 90
  - REGISTRY_ATM_RANGE_PCT defaults to 0.15
  - origins property parses ALLOWED_ORIGINS string correctly
  - origins handles multiple comma-separated values
  - origins strips whitespace around entries
  - Env var override works (monkeypatching os.environ)
"""
import os
import pytest
from unittest.mock import patch


# ── helper: build fresh Settings with specific env overrides ──────────────────

def _settings_with_env(**env_overrides):
    """
    Instantiate a fresh Settings object with specific environment variables
    injected, without relying on a .env file.
    """
    from importlib import reload
    import config as cfg_module

    with patch.dict(os.environ, env_overrides, clear=False):
        reload(cfg_module)
        return cfg_module.Settings()


# ── defaults ──────────────────────────────────────────────────────────────────

def test_algorithm_default():
    from config import Settings
    s = Settings()
    assert s.ALGORITHM == "HS256"


def test_access_token_expire_minutes_default():
    from config import Settings
    s = Settings()
    assert s.ACCESS_TOKEN_EXPIRE_MINUTES == 1440


def test_secret_key_default_is_dev_placeholder():
    from config import Settings
    s = Settings()
    assert s.SECRET_KEY == "dev-secret-change-in-prod"


def test_supabase_fields_default_to_empty_string():
    from config import Settings
    s = Settings()
    assert s.SUPABASE_URL == ""
    assert s.SUPABASE_KEY == ""
    assert s.SUPABASE_SERVICE_KEY == ""


def test_tradier_base_url_default():
    from config import Settings
    s = Settings()
    assert s.TRADIER_BASE_URL == "https://api.tradier.com"


def test_tradier_stream_url_default():
    from config import Settings
    s = Settings()
    assert s.TRADIER_STREAM_URL == "https://stream.tradier.com"


def test_swarm_n_agents_default():
    from config import Settings
    s = Settings()
    assert s.SWARM_N_AGENTS == 6


def test_universe_min_price_default():
    from config import Settings
    s = Settings()
    assert s.UNIVERSE_MIN_PRICE == pytest.approx(1.0)


def test_universe_min_volume_default():
    from config import Settings
    s = Settings()
    assert s.UNIVERSE_MIN_VOLUME == 500_000


def test_registry_max_dte_default():
    from config import Settings
    s = Settings()
    assert s.REGISTRY_MAX_DTE == 90


def test_registry_atm_range_pct_default():
    from config import Settings
    s = Settings()
    assert s.REGISTRY_ATM_RANGE_PCT == pytest.approx(0.15)


def test_registry_refresh_mins_default():
    from config import Settings
    s = Settings()
    assert s.REGISTRY_REFRESH_MINS == 30


def test_registry_min_oi_default():
    from config import Settings
    s = Settings()
    assert s.REGISTRY_MIN_OI == 0


def test_universe_quotes_batch_size_default():
    from config import Settings
    s = Settings()
    assert s.UNIVERSE_QUOTES_BATCH_SIZE == 200


def test_allowed_origins_default():
    from config import Settings
    s = Settings()
    assert s.ALLOWED_ORIGINS == "http://localhost:3000"


# ── origins property ──────────────────────────────────────────────────────────

def test_origins_single_value():
    from config import Settings
    s = Settings(ALLOWED_ORIGINS="https://app.cipher.io")
    assert s.origins == ["https://app.cipher.io"]


def test_origins_multiple_comma_separated():
    from config import Settings
    s = Settings(ALLOWED_ORIGINS="https://app.cipher.io,https://staging.cipher.io,http://localhost:3000")
    assert len(s.origins) == 3
    assert "https://app.cipher.io" in s.origins
    assert "https://staging.cipher.io" in s.origins
    assert "http://localhost:3000" in s.origins


def test_origins_strips_whitespace():
    from config import Settings
    s = Settings(ALLOWED_ORIGINS="https://app.cipher.io , https://staging.cipher.io")
    for origin in s.origins:
        assert origin == origin.strip()


def test_origins_empty_string_returns_empty_list():
    from config import Settings
    s = Settings(ALLOWED_ORIGINS="")
    assert s.origins == []


# ── env var override ──────────────────────────────────────────────────────────

def test_secret_key_overridden_by_env():
    s = _settings_with_env(SECRET_KEY="super-secure-prod-key")
    assert s.SECRET_KEY == "super-secure-prod-key"


def test_swarm_n_agents_overridden_by_env():
    s = _settings_with_env(SWARM_N_AGENTS="12")
    assert s.SWARM_N_AGENTS == 12


def test_universe_min_volume_overridden_by_env():
    s = _settings_with_env(UNIVERSE_MIN_VOLUME="1000000")
    assert s.UNIVERSE_MIN_VOLUME == 1_000_000


def test_registry_max_dte_overridden_by_env():
    s = _settings_with_env(REGISTRY_MAX_DTE="45")
    assert s.REGISTRY_MAX_DTE == 45


# ── type coercion ─────────────────────────────────────────────────────────────

def test_access_token_expire_minutes_is_int():
    from config import Settings
    s = Settings()
    assert isinstance(s.ACCESS_TOKEN_EXPIRE_MINUTES, int)


def test_universe_min_price_is_float():
    from config import Settings
    s = Settings()
    assert isinstance(s.UNIVERSE_MIN_PRICE, float)


def test_universe_stream_eligible_default_is_bool():
    from config import Settings
    s = Settings()
    assert isinstance(s.UNIVERSE_STREAM_ELIGIBLE_DEFAULT, bool)
    assert s.UNIVERSE_STREAM_ELIGIBLE_DEFAULT is True
