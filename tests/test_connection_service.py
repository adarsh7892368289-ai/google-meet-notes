# tests/test_connection_service.py
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.fernet import Fernet

from app.crypto import decrypt
from app.google.oauth_client import TokenBundle
from app.models import User
from app.services import connection_service
from app.services.connection_service import TokenRefreshError


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeOAuthClient:
    def __init__(self):
        self.refresh_calls = 0
        self.revoked = []

    def build_authorization_url(self, state: str) -> str:
        return f"https://auth?state={state}"

    async def exchange_code(self, code: str) -> TokenBundle:
        return TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")

    async def refresh(self, refresh_token: str) -> TokenBundle:
        self.refresh_calls += 1
        return TokenBundle(access_token=f"at-refreshed-{self.refresh_calls}", expires_in=3599, scope="openid")

    async def fetch_userinfo(self, access_token: str) -> str:
        return "user@acme.com"

    async def revoke(self, token: str) -> None:
        self.revoked.append(token)


async def _make_user(db_session) -> User:
    user = User(email="u@acme.com", name="U", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def test_create_connection_encrypts_refresh_token(db_session):
    user = await _make_user(db_session)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid email", refresh_token="rt-secret")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="user@acme.com"
    )
    assert conn.google_email == "user@acme.com"
    assert conn.refresh_token_encrypted != b"rt-secret"
    assert conn.status == "active"


async def test_get_valid_access_token_returns_cached_when_fresh(db_session):
    user = await _make_user(db_session)
    bundle = TokenBundle(access_token="at-cached", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="user@acme.com"
    )
    fake = FakeOAuthClient()
    token = await connection_service.get_valid_access_token(db_session, conn, fake)
    assert token == "at-cached"
    assert fake.refresh_calls == 0


async def test_get_valid_access_token_refreshes_when_expired(db_session):
    user = await _make_user(db_session)
    bundle = TokenBundle(access_token="at-old", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="user@acme.com"
    )
    conn.access_token_expiry = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()
    fake = FakeOAuthClient()
    token = await connection_service.get_valid_access_token(db_session, conn, fake)
    assert token == "at-refreshed-1"
    assert fake.refresh_calls == 1
    await db_session.refresh(conn)
    assert conn.access_token_cache == "at-refreshed-1"


async def test_delete_connection_revokes_and_removes(db_session):
    user = await _make_user(db_session)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="user@acme.com"
    )
    fake = FakeOAuthClient()
    await connection_service.delete_connection(db_session, conn, fake)
    assert fake.revoked  # revoke was called
    assert await connection_service.get_connection(db_session, user) is None


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://x")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError("bad", request=request, response=response)


class FailingRefreshClient(FakeOAuthClient):
    def __init__(self, exc: Exception):
        super().__init__()
        self._exc = exc

    async def refresh(self, refresh_token: str) -> TokenBundle:
        self.refresh_calls += 1
        raise self._exc


async def test_get_valid_access_token_short_circuits_when_needs_reconnect(db_session):
    user = await _make_user(db_session)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="user@acme.com"
    )
    conn.status = "needs_reconnect"
    await db_session.commit()

    fake = FakeOAuthClient()
    with pytest.raises(TokenRefreshError) as excinfo:
        await connection_service.get_valid_access_token(db_session, conn, fake)
    assert excinfo.value.permanent is True
    assert fake.refresh_calls == 0


async def test_refresh_permanent_failure_marks_needs_reconnect(db_session):
    user = await _make_user(db_session)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="user@acme.com"
    )
    conn.access_token_expiry = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()

    fake = FailingRefreshClient(_http_status_error(400))
    with pytest.raises(TokenRefreshError) as excinfo:
        await connection_service.get_valid_access_token(db_session, conn, fake)
    assert excinfo.value.permanent is True
    assert conn.status == "needs_reconnect"


async def test_refresh_transient_failure_keeps_active(db_session):
    user = await _make_user(db_session)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="user@acme.com"
    )
    conn.access_token_expiry = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()

    fake = FailingRefreshClient(_http_status_error(503))
    with pytest.raises(TokenRefreshError) as excinfo:
        await connection_service.get_valid_access_token(db_session, conn, fake)
    assert excinfo.value.permanent is False
    assert conn.status == "active"


async def test_upsert_preserves_refresh_token_when_bundle_has_none(db_session):
    user = await _make_user(db_session)
    first = TokenBundle(access_token="at-1", expires_in=3599, scope="openid", refresh_token="rt-original")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=first, google_email="user@acme.com"
    )
    original_encrypted = conn.refresh_token_encrypted

    second = TokenBundle(access_token="at-2", expires_in=3599, scope="openid", refresh_token=None)
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=second, google_email="user@acme.com"
    )
    assert conn.refresh_token_encrypted == original_encrypted
    assert decrypt(conn.refresh_token_encrypted) == "rt-original"
    assert conn.access_token_cache == "at-2"


async def test_oauth_connection_stores_google_user_id(db_session):
    user = await _make_user(db_session)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="user@acme.com",
        google_user_id="108200000000000000001",
    )
    assert conn.google_user_id == "108200000000000000001"
