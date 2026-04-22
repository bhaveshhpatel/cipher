
from pydantic_settings import BaseSettings
from typing import List

class Settings(BaseSettings):
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

    GROQ_API_KEY: str = ""
    OPENAI_API_KEY: str = ""      # kept for future use
    ANTHROPIC_API_KEY: str = ""   # kept for future use

    REDIS_URL: str = "redis://localhost:6379"
    ALLOWED_ORIGINS: str = "http://localhost:3000"

    @property
    def origins(self) -> List[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(',') if o.strip()]

    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
