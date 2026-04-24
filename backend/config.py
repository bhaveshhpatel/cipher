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

    # Universe pipeline settings
    UNIVERSE_PRIORITY_SYMBOLS: str = "SPY,QQQ,AAPL,TSLA,NVDA,MSFT,AMZN,META,GOOGL,AMD"
    UNIVERSE_BATCH_DELAY_MS: int = 0
    UNIVERSE_STREAM_ELIGIBLE_DEFAULT: bool = True

    UNIVERSE_MIN_PRICE: float = 1.0
    UNIVERSE_MIN_VOLUME: int = 500_000
    UNIVERSE_QUOTES_BATCH_SIZE: int = 200
    UNIVERSE_QUOTES_CONCURRENCY: int = 28

    @property
    def origins(self) -> List[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

    @property
    def priority_symbols(self) -> List[str]:
        return [s.strip() for s in self.UNIVERSE_PRIORITY_SYMBOLS.split(",") if s.strip()]


settings = Settings()
