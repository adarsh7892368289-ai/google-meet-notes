from datetime import datetime, timezone

import httpx
import pytest
from cryptography.fernet import Fernet

from app.google.calendar_client import CreatedEvent
from app.google.oauth_client import TokenBundle
from app.models import User
from app.schemas.meeting import MeetingCreate
from app.services import connection_service, meeting_service


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeOAuthClient:
    async def refresh(self, refresh_token: str) -> TokenBundle:
        return TokenBundle(access_token="at", expires_in=3599, scope="openid")


class FakeCalendarClient:
    def __init__(self):
        self.deleted = []

    async def create_event(self, access_token, *, summary, description, start, end, attendees):
        return CreatedEvent(
            event_id="evt-1",
            meet_uri="https://meet.google.com/abc-defg-hij",
            meeting_code="abc-defg-hij",
            conference_status="success",
        )

    async def delete_event(self, access_token, event_id):
        self.deleted.append(event_id)


class FakeMeetClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.enabled = []

    async def get_space_name(self, access_token, meeting_code):
        return "spaces/SERVERID"

    async def enable_auto_transcript(self, access_token, space_name):
        if self.fail:
            raise httpx.HTTPStatusError(
                "no", request=httpx.Request("PATCH", "https://x"),
                response=httpx.Response(403, request=httpx.Request("PATCH", "https://x")),
            )
        self.enabled.append(space_name)


async def _user_with_connection(db_session) -> User:
    user = User(email="u@acme.com", name="U", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="u@acme.com"
    )
    return user


def _payload(notes_enabled=False) -> MeetingCreate:
    return MeetingCreate(
        title="Roadmap",
        description="d",
        start_time=datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 6, 12, 11, 0, tzinfo=timezone.utc),
        attendees=["a@acme.com"],
        notes_enabled=notes_enabled,
    )


async def test_create_meeting_without_notes(db_session):
    user = await _user_with_connection(db_session)
    meet = FakeMeetClient()
    meeting, warning = await meeting_service.create_meeting(
        db_session, user=user, payload=_payload(notes_enabled=False),
        oauth_client=FakeOAuthClient(), calendar_client=FakeCalendarClient(), meet_client=meet,
    )
    assert meeting.calendar_event_id == "evt-1"
    assert meeting.meet_join_uri == "https://meet.google.com/abc-defg-hij"
    assert meeting.notes_enabled is False
    assert meet.enabled == []
    assert warning is None


async def test_create_meeting_with_notes_enables_transcript(db_session):
    user = await _user_with_connection(db_session)
    meet = FakeMeetClient()
    meeting, warning = await meeting_service.create_meeting(
        db_session, user=user, payload=_payload(notes_enabled=True),
        oauth_client=FakeOAuthClient(), calendar_client=FakeCalendarClient(), meet_client=meet,
    )
    assert meeting.notes_enabled is True
    assert meet.enabled == ["spaces/SERVERID"]
    assert warning is None


async def test_create_meeting_notes_capability_failure_downgrades(db_session):
    user = await _user_with_connection(db_session)
    meet = FakeMeetClient(fail=True)
    meeting, warning = await meeting_service.create_meeting(
        db_session, user=user, payload=_payload(notes_enabled=True),
        oauth_client=FakeOAuthClient(), calendar_client=FakeCalendarClient(), meet_client=meet,
    )
    assert meeting.notes_enabled is False
    assert warning is not None
    assert meeting.calendar_event_id == "evt-1"  # meeting still created


async def test_create_meeting_without_connection_raises(db_session):
    user = User(email="solo@acme.com", name="S", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    with pytest.raises(meeting_service.NotConnectedError):
        await meeting_service.create_meeting(
            db_session, user=user, payload=_payload(),
            oauth_client=FakeOAuthClient(), calendar_client=FakeCalendarClient(),
            meet_client=FakeMeetClient(),
        )


async def test_list_and_get_meetings(db_session):
    user = await _user_with_connection(db_session)
    await meeting_service.create_meeting(
        db_session, user=user, payload=_payload(),
        oauth_client=FakeOAuthClient(), calendar_client=FakeCalendarClient(), meet_client=FakeMeetClient(),
    )
    meetings = await meeting_service.list_meetings(db_session, user)
    assert len(meetings) == 1
    fetched = await meeting_service.get_meeting(db_session, user, meetings[0].id)
    assert fetched is not None
    assert fetched.id == meetings[0].id


async def test_delete_meeting_removes_and_deletes_event(db_session):
    user = await _user_with_connection(db_session)
    cal = FakeCalendarClient()
    meeting, _ = await meeting_service.create_meeting(
        db_session, user=user, payload=_payload(),
        oauth_client=FakeOAuthClient(), calendar_client=cal, meet_client=FakeMeetClient(),
    )
    ok = await meeting_service.delete_meeting(
        db_session, user=user, meeting_id=meeting.id,
        oauth_client=FakeOAuthClient(), calendar_client=cal,
    )
    assert ok is True
    assert "evt-1" in cal.deleted
    assert await meeting_service.get_meeting(db_session, user, meeting.id) is None
