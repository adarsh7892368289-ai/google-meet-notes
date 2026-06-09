import pytest
from cryptography.fernet import Fernet

from app.api.deps import get_oauth_client
from app.google.oauth_client import TokenBundle
from app.main import app
from app.security import create_oauth_state


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeOAuthClient:
    def build_authorization_url(self, state: str) -> str:
        return f"https://auth.example/consent?state={state}"

    async def exchange_code(self, code: str) -> TokenBundle:
        return TokenBundle(access_token="at", expires_in=3599, scope="openid email", refresh_token="rt")

    async def refresh(self, refresh_token: str) -> TokenBundle:
        return TokenBundle(access_token="at2", expires_in=3599, scope="openid email")

    async def fetch_userinfo(self, access_token: str) -> str:
        return "person@acme.com"

    async def revoke(self, token: str) -> None:
        return None


@pytest.fixture
def fake_oauth():
    fake = FakeOAuthClient()
    app.dependency_overrides[get_oauth_client] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_oauth_client, None)


async def _register(client) -> str:
    resp = await client.post(
        "/v1/auth/register",
        json={"email": "person@acme.com", "name": "P", "password": "password123"},
    )
    return resp.json()["access_token"]


async def test_start_returns_authorization_url(client, fake_oauth):
    token = await _register(client)
    resp = await client.get(
        "/v1/connections/google/start", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["authorization_url"].startswith("https://auth.example/consent?state=")


async def test_callback_creates_connection(client, fake_oauth):
    token = await _register(client)
    # find the user id from /me
    me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    user_id = me.json()["id"]
    state = create_oauth_state(user_id)

    resp = await client.get(f"/v1/connections/google/callback?code=abc&state={state}")
    assert resp.status_code == 200

    status = await client.get(
        "/v1/connections/google", headers={"Authorization": f"Bearer {token}"}
    )
    body = status.json()
    assert body["connected"] is True
    assert body["google_email"] == "person@acme.com"
    assert body["status"] == "active"


async def test_callback_rejects_bad_state(client, fake_oauth):
    resp = await client.get("/v1/connections/google/callback?code=abc&state=not-valid")
    assert resp.status_code == 400


async def test_status_when_not_connected(client, fake_oauth):
    token = await _register(client)
    resp = await client.get(
        "/v1/connections/google", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["connected"] is False


async def test_delete_connection(client, fake_oauth):
    token = await _register(client)
    me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    state = create_oauth_state(me.json()["id"])
    await client.get(f"/v1/connections/google/callback?code=abc&state={state}")

    resp = await client.delete(
        "/v1/connections/google", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 204

    status = await client.get(
        "/v1/connections/google", headers={"Authorization": f"Bearer {token}"}
    )
    assert status.json()["connected"] is False
