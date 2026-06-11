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
    workspace_events_topic: str = ""
    push_audience: str = ""
    push_service_account_email: str = ""
    subscription_ttl_seconds: int = 604800
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_chunk_token_threshold: int = 600000
    redis_url: str = ""
    notes_default_title: str = "Meeting Notes"


@lru_cache
def get_settings() -> Settings:
    return Settings()
