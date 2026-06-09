import pytest
from cryptography.fernet import Fernet

from app.api.deps import get_calendar_client, get_meet_client, get_oauth_client
from app.google.calendar_client import CreatedEvent
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
    def build_authorization_url(self, state): return f"https://auth?state={state}"
    async def exchange_code(self, code): return TokenBundle("at", 3599, "openid email", "rt")
    async def refresh(self, refresh_token): return TokenBundle("at2", 3599, "openid email")
    async def fetch_userinfo(self, access_token): return "person@acme.com"
    async def revoke(self, token): return None


class FakeCalendarClient:
    def __init__(self): self.deleted = []
    async def create_event(self, access_token, *, summary, description, start, end, attendees):
        return CreatedEvent("evt-1", "https://meet.google.com/abc-defg-hij", "abc-defg-hij", "success")
    async def delete_event(self, access_token, event_id): self.deleted.append(event_id)


class FakeMeetClient:
    def __init__(self, fail=False): self.fail = fail
    async def get_space_name(self, access_token, meeting_code): return "spaces/SID"
    async def enable_auto_transcript(self, access_token, space_name):
        if self.fail:
            import httpx
            raise httpx.HTTPStatusError("x", request=httpx.Request("PATCH", "https://x"),
                                        response=httpx.Response(403, request=httpx.Request("PATCH", "https://x")))


@pytest.fixture
def fakes():
    cal, meet, oauth = FakeCalendarClient(), FakeMeetClient(), FakeOAuthClient()
    app.dependency_overrides[get_calendar_client] = lambda: cal
    app.dependency_overrides[get_meet_client] = lambda: meet
    app.dependency_overrides[get_oauth_client] = lambda: oauth
    yield {"cal": cal, "meet": meet, "oauth": oauth}
    for dep in (get_calendar_client, get_meet_client, get_oauth_client):
        app.dependency_overrides.pop(dep, None)


async def _register_and_connect(client) -> str:
    reg = await client.post(
        "/v1/auth/register",
        json={"email": "person@acme.com", "name": "P", "password": "password123"},
    )
    token = reg.json()["access_token"]
    me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    state = create_oauth_state(me.json()["id"])
    await client.get(f"/v1/connections/google/callback?code=abc&state={state}")
    return token


def _body(notes=False):
    return {
        "title": "Q3 Roadmap",
        "description": "plan",
        "start_time": "2026-06-12T10:00:00+00:00",
        "end_time": "2026-06-12T11:00:00+00:00",
        "attendees": ["a@acme.com"],
        "notes_enabled": notes,
    }


async def test_create_meeting_requires_connection(client, fakes):
    reg = await client.post(
        "/v1/auth/register",
        json={"email": "noconn@acme.com", "name": "N", "password": "password123"},
    )
    token = reg.json()["access_token"]
    resp = await client.post("/v1/meetings", json=_body(), headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 409


async def test_create_meeting_success(client, fakes):
    token = await _register_and_connect(client)
    resp = await client.post(
        "/v1/meetings", json=_body(notes=True), headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["meet_join_uri"] == "https://meet.google.com/abc-defg-hij"
    assert body["notes_enabled"] is True
    assert body["warning"] is None


async def test_create_meeting_notes_capability_downgrade(client, fakes):
    fakes["meet"].fail = True
    token = await _register_and_connect(client)
    resp = await client.post(
        "/v1/meetings", json=_body(notes=True), headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["notes_enabled"] is False
    assert body["warning"]


async def test_create_meeting_validates_times(client, fakes):
    token = await _register_and_connect(client)
    bad = _body()
    bad["end_time"] = "2026-06-12T09:00:00+00:00"  # before start
    resp = await client.post("/v1/meetings", json=bad, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 422


async def test_list_get_delete_meeting(client, fakes):
    token = await _register_and_connect(client)
    created = await client.post("/v1/meetings", json=_body(), headers={"Authorization": f"Bearer {token}"})
    mid = created.json()["id"]

    listed = await client.get("/v1/meetings", headers={"Authorization": f"Bearer {token}"})
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    got = await client.get(f"/v1/meetings/{mid}", headers={"Authorization": f"Bearer {token}"})
    assert got.status_code == 200

    deleted = await client.delete(f"/v1/meetings/{mid}", headers={"Authorization": f"Bearer {token}"})
    assert deleted.status_code == 204

    missing = await client.get(f"/v1/meetings/{mid}", headers={"Authorization": f"Bearer {token}"})
    assert missing.status_code == 404
