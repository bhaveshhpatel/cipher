"""
config.py — Application settings loaded from environment variables.

All secrets are read from the environment; no defaults are hardcoded
except for non-sensitive operational parameters.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

from pydantic import computed_field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Auth ──────────────────────────────────────────────────────────────────
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "change-me-in-production")
    ALGORITHM:  str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24 h

    # ── Supabase ──────────────────────────────────────────────────────────────
    SUPABASE_URL:              str = os.environ.get("SUPABASE_URL",              "https://placeholder.supabase.co")
    SUPABASE_ANON_KEY:         str = os.environ.get("SUPABASE_ANON_KEY",         "")
    SUPABASE_SERVICE_ROLE_KEY: str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    SUPABASE_SERVICE_KEY:      str = os.environ.get("SUPABASE_SERVICE_KEY",      "")  # legacy alias

    # ── Tradier ───────────────────────────────────────────────────────────────
    TRADIER_API_KEY:      str = os.environ.get("TRADIER_API_KEY",      "")
    TRADIER_STREAM_TOKEN: str = os.environ.get("TRADIER_STREAM_TOKEN", "")
    TRADIER_ACCOUNT_ID:   str = os.environ.get("TRADIER_ACCOUNT_ID",   "")
    # Base URL for Tradier REST API (sandbox vs production)
    # Production: https://api.tradier.com
    # Sandbox:    https://sandbox.tradier.com
    TRADIER_BASE_URL: str = os.environ.get(
        "TRADIER_BASE_URL", "https://api.tradier.com"
    )

    # ── Universe / stream eligibility ─────────────────────────────────────────
    # Minimum last price (USD) for a symbol to be stream-eligible
    UNIVERSE_MIN_PRICE: float = float(os.environ.get("UNIVERSE_MIN_PRICE", "1.0"))
    # Minimum daily volume for a symbol to be stream-eligible
    UNIVERSE_MIN_VOLUME: int = int(os.environ.get("UNIVERSE_MIN_VOLUME", "100000"))
    # How many symbols per Tradier /v1/markets/quotes batch request
    UNIVERSE_QUOTES_BATCH_SIZE: int = int(os.environ.get("UNIVERSE_QUOTES_BATCH_SIZE", "200"))
    # Max concurrent batch requests to Tradier quotes endpoint
    UNIVERSE_QUOTES_CONCURRENCY: int = int(os.environ.get("UNIVERSE_QUOTES_CONCURRENCY", "28"))

    # ── App ───────────────────────────────────────────────────────────────────
    APP_ENV:   str = os.environ.get("APP_ENV",   "production")
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")

    # ── CORS ──────────────────────────────────────────────────────────────────
    CORS_ALLOWED_ORIGINS: str = os.environ.get(
        "CORS_ALLOWED_ORIGINS",
        "https://cipher.vercel.app,https://cipher-git-main.vercel.app,http://localhost:3000",
    )

    # ── Symbol registry / OI build ────────────────────────────────────────────
    REGISTRY_T1_DTE:                  int   = 90
    REGISTRY_T2_DTE:                  int   = 60
    REGISTRY_T3_DTE:                  int   = 30
    REGISTRY_ATM_RANGE_PCT:           float = 0.15
    REGISTRY_REFRESH_MINS:            int   = 30
    REGISTRY_MIN_OI:                  int   = 0
    REGISTRY_EXPIRY_DAY_REFRESH_MINS: int   = 15

    # ── JWT_SECRET: real field alias so hasattr(settings, 'JWT_SECRET') is True
    # pydantic v2 @property is not visible to hasattr — use computed_field.
    @computed_field  # type: ignore[prop-decorator]
    @property
    def JWT_SECRET(self) -> str:  # noqa: N802
        """Alias for SECRET_KEY — tests assert hasattr(settings, 'JWT_SECRET')."""
        return self.SECRET_KEY

    # ── origins helper used by main.py ────────────────────────────────────────
    @property
    def origins(self) -> list[str]:
        """Return the configured CORS origins as a list."""
        return [o.strip() for o in self.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]

    class Config:
        env_file = ".env"
        extra    = "ignore"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
