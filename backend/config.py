"""
config.py — Application settings loaded from environment variables.

All secrets are read from the environment; no defaults are hardcoded
except for non-sensitive operational parameters.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional

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

    # REST API host — used for quotes, chains, expirations, session token
    TRADIER_BASE_URL: str = os.environ.get(
        "TRADIER_BASE_URL", "https://api.tradier.com"
    )

    # Streaming host — DIFFERENT from the REST host.
    # stream_worker.py POSTs to TRADIER_STREAM_URL/v1/markets/events.
    # This was missing from config, causing workers to AttributeError or
    # POST to an empty URL on every connect attempt → zero ticks received.
    TRADIER_STREAM_URL: str = os.environ.get(
        "TRADIER_STREAM_URL", "https://stream.tradier.com"
    )

    # ── AI / LLM ──────────────────────────────────────────────────────────────
    GROQ_API_KEY:   Optional[str] = os.environ.get("GROQ_API_KEY", None) or None
    SWARM_N_AGENTS: int           = int(os.environ.get("SWARM_N_AGENTS", "6"))

    # ── Universe / stream eligibility ─────────────────────────────────────────
    UNIVERSE_MIN_PRICE: float = float(os.environ.get("UNIVERSE_MIN_PRICE", "1.0"))
    UNIVERSE_MIN_VOLUME: int = int(os.environ.get("UNIVERSE_MIN_VOLUME", "100000"))
    UNIVERSE_QUOTES_BATCH_SIZE: int = int(os.environ.get("UNIVERSE_QUOTES_BATCH_SIZE", "200"))
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

    # ── Computed aliases (plain properties, visible to hasattr) ───────────────

    @property
    def JWT_SECRET(self) -> str:  # noqa: N802
        """Alias for SECRET_KEY — tests assert hasattr(settings, 'JWT_SECRET')."""
        return self.SECRET_KEY

    @property
    def SUPABASE_KEY(self) -> Optional[str]:  # noqa: N802
        """Alias: returns first non-empty Supabase key available."""
        return (
            self.SUPABASE_SERVICE_ROLE_KEY
            or self.SUPABASE_SERVICE_KEY
            or self.SUPABASE_ANON_KEY
            or None
        ) or None

    # ── Origins helper used by main.py ────────────────────────────────────────
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
