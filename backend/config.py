from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    SECRET_KEY: str = "dev-secret-change-in-prod"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    SUPABASE_URL: str = ""
    SUPABASE_KEY: str = ""
    SUPABASE_SERVICE_KEY: str = ""

    TRADIER_API_KEY: str = ""
    TRADIER_ACCOUNT_ID: str = ""
    TRADIER_BASE_URL: str = "https://api.tradier.com"
    TRADIER_STREAM_URL: str = "https://stream.tradier.com"

    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    GROQ_API_KEY: str = ""

    REDIS_URL: str = "redis://localhost:6379"
    ALLOWED_ORIGINS: str = "http://localhost:3000"

    # Phase 5A — AI Swarm configuration
    # Number of agents to use: snapped to nearest of [3, 6, 9, 12]. Default: 6.
    # Override via Railway env var SWARM_N_AGENTS.
    SWARM_N_AGENTS: int = 6

    # ---------------------------------------------------------------------------
    # Universe pipeline settings
    # ---------------------------------------------------------------------------
    # NOTE: UNIVERSE_PRIORITY_SYMBOLS removed in C-020.
    # Priority tier is now fully dynamic based on volume thresholds.
    # Tier 1 (Elite):   avg_volume >= TIER1_MIN_VOLUME
    # Tier 2 (Active):  avg_volume >= TIER2_MIN_VOLUME AND last_price >= TIER2_MIN_PRICE
    # Tier 3 (Eligible): avg_volume >= UNIVERSE_MIN_VOLUME AND last_price >= UNIVERSE_MIN_PRICE
    # All tier thresholds are also stored in the `ingestion_config` DB table
    # and can be changed from the admin UI without restarting the service.
    # ---------------------------------------------------------------------------
    UNIVERSE_BATCH_DELAY_MS: int = 0
    UNIVERSE_STREAM_ELIGIBLE_DEFAULT: bool = True

    UNIVERSE_MIN_PRICE: float = 1.0
    UNIVERSE_MIN_VOLUME: int = 500_000
    UNIVERSE_QUOTES_BATCH_SIZE: int = 200
    UNIVERSE_QUOTES_CONCURRENCY: int = 28

    # Tier volume/price thresholds (defaults; overridden by ingestion_config DB at runtime)
    TIER1_MIN_VOLUME: int = 20_000_000
    TIER2_MIN_VOLUME: int = 2_000_000
    TIER2_MIN_PRICE: float = 10.0

    # ---------------------------------------------------------------------------
    # OCC Symbol Registry settings (Layer 1)
    # All knobs are also in `ingestion_config` DB table for live admin control.
    # ---------------------------------------------------------------------------
    # Single-tier fallback (used when tier-specific knobs not in DB)
    REGISTRY_MAX_DTE: int = 90
    REGISTRY_ATM_RANGE_PCT: float = 0.15
    REGISTRY_REFRESH_MINS: int = 30
    REGISTRY_MIN_OI: int = 0
    REGISTRY_EXPIRY_DAY_REFRESH_MINS: int = 15

    # Tier-specific OCC filter knobs (defaults; overridden by ingestion_config DB at runtime)
    REGISTRY_T1_ATM_PCT: float = 0.20
    REGISTRY_T1_MAX_DTE: int = 90
    REGISTRY_T2_ATM_PCT: float = 0.15
    REGISTRY_T2_MAX_DTE: int = 60
    REGISTRY_T3_ATM_PCT: float = 0.10
    REGISTRY_T3_MAX_DTE: int = 30

    # Stream worker connection safety (Fix 1 + 2)
    STREAM_WORKER_STAGGER_MS: int = 200   # ms between worker spawns
    STREAM_SESSION_SEM: int = 3            # max concurrent session token fetches

    # OI enrichment
    OI_REFRESH_MINS: int = 30
    OI_MIN_THRESHOLD: int = 0

    @property
    def origins(self) -> List[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]


settings = Settings()
