"""
Regression tests for config.py — Settings class and `settings` singleton.

Covers:
  - All field default values match documented/expected values
  - origins property: single origin, multi-origin CSV, whitespace trimmed,
    empty string returns [], duplicate entries preserved
  - SWARM_N_AGENTS default is 6
  - REGISTRY_* defaults (MAX_DTE=90, ATM_RANGE_PCT=0.15, REFRESH_MINS=30,
    MIN_OI=0, EXPIRY_DAY_REFRESH_MINS=15)
  - UNIVERSE_* defaults (MIN_PRICE=1.0, MIN_VOLUME=500_000,
    QUOTES_BATCH_SIZE=200, QUOTES_CONCURRENCY=28)
  - SECRET_KEY default is the dev placeholder (signals unsafe prod use)
  - ALGORITHM default is 'HS256'
  - ACCESS_TOKEN_EXPIRE_MINUTES default is 1440 (24 hours)
  - TRADIER_BASE_URL default is production URL
  - TRADIER_STREAM_URL default is production stream URL
  - Settings can be instantiated with env overrides (no .env file required)
"""
import os
import pytest
from unittest.mock import patch


# Import the module-level singleton and class
from config import Settings, settings


# ── JWT / auth defaults ─────────────────────────────────────────────────────────

def test_secret_key_default_is_dev_placeholder():
    """Default SECRET_KEY is the dev placeholder — not safe for production."""
    s = Settings(_env_file=None)
    assert s.SECRET_KEY == "dev-secret-change-in-prod"


def test_algorithm_default_is_hs256():
    s = Settings(_env_file=None)
    assert s.ALGORITHM == "HS256"


def test_access_token_expire_minutes_default():
    """Default token lifetime is 1440 minutes (24 hours)."""
    s = Settings(_env_file=None)
    assert s.ACCESS_TOKEN_EXPIRE_MINUTES == 1440


# ── Tradier defaults ───────────────────────────────────────────────────────────

def test_tradier_base_url_default():
    s = Settings(_env_file=None)
    assert s.TRADIER_BASE_URL == "https://api.tradier.com"


def test_tradier_stream_url_default():
    s = Settings(_env_file=None)
    assert s.TRADIER_STREAM_URL == "https://stream.tradier.com"


def test_tradier_api_key_default_is_empty():
    s = Settings(_env_file=None)
    assert s.TRADIER_API_KEY == ""


def test_tradier_account_id_default_is_empty():
    s = Settings(_env_file=None)
    assert s.TRADIER_ACCOUNT_ID == ""


# ── AI swarm default ───────────────────────────────────────────────────────────

def test_swarm_n_agents_default_is_6():
    s = Settings(_env_file=None)
    assert s.SWARM_N_AGENTS == 6


# ── REGISTRY_* defaults ──────────────────────────────────────────────────────

def test_registry_max_dte_default_is_90():
    s = Settings(_env_file=None)
    assert s.REGISTRY_MAX_DTE == 90


def test_registry_atm_range_pct_default_is_015():
    s = Settings(_env_file=None)
    assert s.REGISTRY_ATM_RANGE_PCT == 0.15


def test_registry_refresh_mins_default_is_30():
    s = Settings(_env_file=None)
    assert s.REGISTRY_REFRESH_MINS == 30


def test_registry_min_oi_default_is_0():
    s = Settings(_env_file=None)
    assert s.REGISTRY_MIN_OI == 0


def test_registry_expiry_day_refresh_mins_default_is_15():
    s = Settings(_env_file=None)
    assert s.REGISTRY_EXPIRY_DAY_REFRESH_MINS == 15


# ── UNIVERSE_* defaults ──────────────────────────────────────────────────────

def test_universe_min_price_default_is_1():
    s = Settings(_env_file=None)
    assert s.UNIVERSE_MIN_PRICE == 1.0


def test_universe_min_volume_default_is_500k():
    s = Settings(_env_file=None)
    assert s.UNIVERSE_MIN_VOLUME == 500_000


def test_universe_quotes_batch_size_default_is_200():
    s = Settings(_env_file=None)
    assert s.UNIVERSE_QUOTES_BATCH_SIZE == 200


def test_universe_quotes_concurrency_default_is_28():
    s = Settings(_env_file=None)
    assert s.UNIVERSE_QUOTES_CONCURRENCY == 28


# ── origins property ────────────────────────────────────────────────────────────

def test_origins_single_value():
    s = Settings(_env_file=None, ALLOWED_ORIGINS="http://localhost:3000")
    assert s.origins == ["http://localhost:3000"]


def test_origins_multiple_csv():
    s = Settings(
        _env_file=None,
        ALLOWED_ORIGINS="http://localhost:3000,https://cipher.app,https://staging.cipher.app"
    )
    assert s.origins == [
        "http://localhost:3000",
        "https://cipher.app",
        "https://staging.cipher.app",
    ]


def test_origins_whitespace_trimmed():
    s = Settings(
        _env_file=None,
        ALLOWED_ORIGINS="http://localhost:3000 , https://cipher.app , "
    )
    result = s.origins
    assert "http://localhost:3000" in result
    assert "https://cipher.app" in result
    # trailing empty string after strip should be excluded
    assert "" not in result


def test_origins_empty_string_returns_empty_list():
    s = Settings(_env_file=None, ALLOWED_ORIGINS="")
    assert s.origins == []


def test_origins_default_is_localhost_3000():
    s = Settings(_env_file=None)
    assert "http://localhost:3000" in s.origins


# ── env override ───────────────────────────────────────────────────────────────

def test_swarm_n_agents_can_be_overridden():
    """SWARM_N_AGENTS accepts int override at construction time."""
    s = Settings(_env_file=None, SWARM_N_AGENTS=12)
    assert s.SWARM_N_AGENTS == 12


def test_registry_max_dte_can_be_overridden():
    s = Settings(_env_file=None, REGISTRY_MAX_DTE=45)
    assert s.REGISTRY_MAX_DTE == 45


def test_groq_api_key_default_is_empty():
    s = Settings(_env_file=None)
    assert s.GROQ_API_KEY == ""


def test_supabase_url_default_is_empty():
    s = Settings(_env_file=None)
    assert s.SUPABASE_URL == ""
