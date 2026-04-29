"""
config.py — Application settings loaded from environment variables.

All secrets are read from the environment; no defaults are hardcoded
except for non-sensitive operational parameters.

pydantic-settings reads env vars automatically at instantiation time.
DO NOT wrap field defaults in os.environ.get() — that evaluates at
class-definition time (before the process environment is fully set)
and always wins over pydantic's own env-var injection.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── Auth ──────────────────────────────────────────────────────────────────
    SECRET_KEY: str = "change-me-in-production"
    ALGORITHM:  str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24 h

    # ── Supabase ──────────────────────────────────────────────────────────────
    SUPABASE_URL:              str = "https://placeholder.supabase.co"
    SUPABASE_ANON_KEY:         str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    SUPABASE_SERVICE_KEY:      str = ""  # legacy alias

    # ── Tradier ───────────────────────────────────────────────────────────────
    TRADIER_API_KEY:      str = ""
    TRADIER_STREAM_TOKEN: str = ""
    TRADIER_ACCOUNT_ID:   str = ""

    # REST API host — used for quotes, chains, expirations, session token
    TRADIER_BASE_URL: str = "https://api.tradier.com"

    # Streaming host — DIFFERENT from the REST host.
    # stream_worker.py POSTs to TRADIER_STREAM_URL/v1/markets/events.
    TRADIER_STREAM_URL: str = "https://stream.tradier.com"

    # ── AI / LLM ──────────────────────────────────────────────────────────────
    GROQ_API_KEY:   Optional[str] = None
    SWARM_N_AGENTS: int           = 6

    # ── Universe / stream eligibility ─────────────────────────────────────────
    UNIVERSE_MIN_PRICE:           float = 1.0
    UNIVERSE_MIN_VOLUME:          int   = 100_000
    UNIVERSE_QUOTES_BATCH_SIZE:   int   = 200
    UNIVERSE_QUOTES_CONCURRENCY:  int   = 28

    # ── App ───────────────────────────────────────────────────────────────────
    APP_ENV:   str = "production"
    LOG_LEVEL: str = "INFO"

    # ── CORS ──────────────────────────────────────────────────────────────────
    CORS_ALLOWED_ORIGINS: str = (
        "https://cipher.vercel.app,"
        "https://cipher-git-main.vercel.app,"
        "http://localhost:3000"
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
