import uuid
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

from app.api.deps import get_summarizer
from app.crypto import encrypt
from app.google.oauth_client import TokenBundle
from app.main import app
from app.models import Conference, Meeting, Notes, Transcript, User
from app.schemas.notes import ActionItem, NotesContent
from app.services import connection_service


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeSummarizer:
    async def count_tokens(self, text):
        return 5

    async def summarize(self, transcript):
        return NotesContent(summary="regenerated", decisions=["D9"], action_items=[ActionItem(who="Bob", what="x")])


@pytest.fixture
def fake_summarizer():
    app.dependency_overrides[get_summarizer] = lambda: FakeSummarizer()
    yield
    app.dependency_overrides.pop(get_summarizer, None)


async def _register(client) -> tuple[str, uuid.UUID]:
    resp = await client.post(
        "/v1/auth/register",
        json={"email": "owner@acme.com", "name": "O", "password": "password123"},
    )
    token = resp.json()["access_token"]
    me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    return token, uuid.UUID(me.json()["id"])


async def _seed(db_session, user_id, *, with_notes=True, with_transcript=True, state="notes_generated"):
    user = await db_session.get(User, user_id)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="owner@acme.com"
    )
    meeting = Meeting(
        user_id=user_id, title="Q3 Sync", start_time=datetime(2026, 6, 11, tzinfo=timezone.utc),
        end_time=datetime(2026, 6, 11, tzinfo=timezone.utc), attendees=[],
        meet_space_name="spaces/S", notes_enabled=True, notes_config={},
    )
    db_session.add(meeting)
    await db_session.commit()
    await db_session.refresh(meeting)
    conf = Conference(
        oauth_connection_id=conn.id, meeting_id=meeting.id,
        conference_record_name="conferenceRecords/cr-1", pipeline_state=state,
    )
    db_session.add(conf)
    await db_session.commit()
    await db_session.refresh(conf)
    if with_transcript:
        db_session.add(Transcript(conference_id=conf.id, full_text=encrypt("Alice: hello"), language="en-US", speaker_map={}))
    if with_notes:
        db_session.add(Notes(conference_id=conf.id, title="Q3 Sync", summary="recap", decisions=["D1"], action_items=[], gemini_model="m"))
    await db_session.commit()
    return meeting, conf


async def test_get_conference_notes(client, db_session):
    token, uid = await _register(client)
    _, conf = await _seed(db_session, uid)
    resp = await client.get(f"/v1/conferences/{conf.id}/notes", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["summary"] == "recap"
    assert resp.json()["title"] == "Q3 Sync"


async def test_get_conference_transcript(client, db_session):
    token, uid = await _register(client)
    _, conf = await _seed(db_session, uid)
    resp = await client.get(f"/v1/conferences/{conf.id}/transcript", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["text"] == "Alice: hello"


async def test_get_notes_404_when_absent(client, db_session):
    token, uid = await _register(client)
    _, conf = await _seed(db_session, uid, with_notes=False)
    resp = await client.get(f"/v1/conferences/{conf.id}/notes", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


async def test_list_meeting_conferences(client, db_session):
    token, uid = await _register(client)
    meeting, conf = await _seed(db_session, uid)
    resp = await client.get(f"/v1/meetings/{meeting.id}/conferences", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["pipeline_state"] == "notes_generated"


async def test_meeting_notes_latest_occurrence(client, db_session):
    token, uid = await _register(client)
    meeting, conf = await _seed(db_session, uid)
    resp = await client.get(f"/v1/meetings/{meeting.id}/notes", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["summary"] == "recap"


async def test_regenerate_notes(client, db_session, fake_summarizer):
    token, uid = await _register(client)
    _, conf = await _seed(db_session, uid)
    resp = await client.post(
        f"/v1/conferences/{conf.id}/notes:regenerate", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["summary"] == "regenerated"
    assert resp.json()["decisions"] == ["D9"]


async def test_cannot_access_other_users_conference(client, db_session):
    token, uid = await _register(client)
    # second user owns the conference
    other = User(email="other@acme.com", name="X", hashed_password="x")
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)
    _, conf = await _seed(db_session, other.id)
    resp = await client.get(f"/v1/conferences/{conf.id}/notes", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404
