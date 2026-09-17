"""
Application configuration.

Reads infrastructure configuration from environment variables, per
Deployment/Environment-Config.md. Business configuration (retry policy,
agent persona/script, etc.) is NOT read here -- it lives in the database
(`retry_policy`, `agent_config` tables) and is set at runtime via the
admin dashboard, not via deployment.

Every setting has a safe, non-functional default so the service can boot
in a local/dev environment without any secrets configured. Production
deployments must override these via real environment variables / a
secrets manager -- never via committed files.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Service metadata ---
    app_name: str = "AI Calling Agent API"
    environment: str = "development"
    log_level: str = "debug"

    # --- API / auth (Environment-Config.md §2.4) ---
    jwt_signing_key: str = "dev-only-insecure-key-change-me"
    jwt_expiry_seconds: int = 3600
    api_rate_limit_per_minute: int = 60

    # --- Database & data layer (Environment-Config.md §2.1) ---
    primary_db_url: str = (
        "postgresql+psycopg://postgres:postgres@localhost:5432/ai_calling_agent"
    )
    primary_db_pool_size: int = 5


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance -- environment is read once per process."""
    return Settings()
