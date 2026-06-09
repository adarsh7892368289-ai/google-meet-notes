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
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/v1/connections/google/callback"
    google_scopes: str = (
        "openid email "
        "https://www.googleapis.com/auth/meetings.space.created "
        "https://www.googleapis.com/auth/meetings.space.settings "
        "https://www.googleapis.com/auth/calendar.events "
        "https://www.googleapis.com/auth/drive.file "
        "https://www.googleapis.com/auth/gmail.send"
    )
    encryption_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
