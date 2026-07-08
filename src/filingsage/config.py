"""Typed application settings, loaded from environment variables (12-factor).

Defaults point at the Docker Compose service names so containers work with
zero configuration; a local `.env` file overrides them for host-side runs.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql://filingsage:filingsage@postgres:5432/filingsage"
    redis_url: str = "redis://redis:6379/0"

    # SEC EDGAR fair-access policy requires a declared User-Agent with a
    # real contact. Used by the EDGAR connector; must be set before ingestion.
    sec_contact_email: str = "change-me@example.com"


@lru_cache
def get_settings() -> Settings:
    """Cached singleton — settings are read once per process."""
    return Settings()
