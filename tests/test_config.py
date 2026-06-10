from app.config import Settings


def test_settings_reads_values_from_env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "60")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db_test")

    settings = Settings()

    assert settings.jwt_secret == "test-secret"
    assert settings.jwt_expire_minutes == 60
    assert settings.database_url.endswith("/db")


def test_settings_reads_google_oauth_values(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://app.example.com/cb")
    monkeypatch.setenv("ENCRYPTION_KEY", "k" * 44)

    settings = Settings()

    assert settings.google_client_id == "cid"
    assert settings.google_client_secret == "secret"
    assert settings.google_redirect_uri == "https://app.example.com/cb"
    assert settings.encryption_key == "k" * 44
    assert "openid" in settings.google_scopes


def test_events_settings_have_defaults(monkeypatch):
    monkeypatch.delenv("WORKSPACE_EVENTS_TOPIC", raising=False)
    monkeypatch.delenv("PUSH_AUDIENCE", raising=False)
    monkeypatch.delenv("PUSH_SERVICE_ACCOUNT_EMAIL", raising=False)
    from app.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    assert s.workspace_events_topic == ""
    assert s.push_audience == ""
    assert s.push_service_account_email == ""
    assert s.subscription_ttl_seconds == 604800
    get_settings.cache_clear()
