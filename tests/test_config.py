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
