from pydantic_settings import BaseSettings
from typing import List

class Settings(BaseSettings):
    # JWT
    SECRET_KEY:                   str  = "dev-secret-change-in-prod"
    ALGORITHM:                    str  = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES:  int  = 1440

    # Supabase
    SUPABASE_URL:         str = ""
    SUPABASE_KEY:         str = ""
    SUPABASE_SERVICE_KEY: str = ""

    # Tradier
    TRADIER_API_KEY:     str = ""
    TRADIER_ACCOUNT_ID:  str = ""
    TRADIER_BASE_URL:    str = "https://api.tradier.com"
    TRADIER_STREAM_URL:  str = "https://stream.tradier.com"

    # AI
    OPENAI_API_KEY:    str = ""
    ANTHROPIC_API_KEY: str = ""

    # Redis
    REDIS_URL: str = "redis://localhost:6379"

    # CORS
    ALLOWED_ORIGINS: str = "http://localhost:3000"

    @property
    def origins(self) -> List[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",")]

    class Config:
        env_file = ".env"
        extra    = "ignore"

settings = Settings()
