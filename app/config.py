from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    jwt_secret: str = "change-me"
    jwt_expire_minutes: int = 1440
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/meetnotes"
    test_database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/meetnotes_test"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
