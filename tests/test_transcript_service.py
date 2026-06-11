import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.crypto import decrypt
from app.google.meet_client import (
    ConferenceRecordInfo,
    ParticipantInfo,
    TranscriptEntryInfo,
)
from app.google.oauth_client import TokenBundle
from app.models import Conference, Meeting, Transcript, User
from app.services import connection_service, transcript_service


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeOAuthClient:
    async def refresh(self, refresh_token):
        return TokenBundle(access_token="at", expires_in=3599, scope="openid")


class FakeMeetClient:
    def __init__(self, *, space="spaces/SERVERID"):
        self._space = space

    async def get_conference_record(self, access_token, conference_record_name):
        return ConferenceRecordInfo(
            name=conference_record_name, space=self._space,
            start_time="2026-06-11T10:00:00Z", end_time="2026-06-11T11:00:00Z",
        )

    async def list_participants(self, access_token, conference_record_name):
        return [
            ParticipantInfo(name="conferenceRecords/cr-1/participants/p1", display_name="Alice"),
            ParticipantInfo(name="conferenceRecords/cr-1/participants/p2", display_name="Bob"),
        ]

    async def list_transcript_entries(self, access_token, transcript_resource_name):
        return [
            TranscriptEntryInfo(participant="conferenceRecords/cr-1/participants/p1", text="hello", language_code="en-US"),
            TranscriptEntryInfo(participant="conferenceRecords/cr-1/participants/p2", text="hi there", language_code="en-US"),
            TranscriptEntryInfo(participant=None, text="(unknown)", language_code="en-US"),
        ]


async def _conf_with_meeting(db_session, *, space="spaces/SERVERID", with_meeting=True):
    user = User(email="x@acme.com", name="X", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="x@acme.com"
    )
    if with_meeting:
        from datetime import datetime, timezone
        m = Meeting(
            user_id=user.id, title="Q3 Sync", start_time=datetime(2026, 6, 11, tzinfo=timezone.utc),
            end_time=datetime(2026, 6, 11, tzinfo=timezone.utc), attendees=[],
            meet_space_name=space, notes_enabled=True, notes_config={},
        )
        db_session.add(m)
        await db_session.commit()
    conf = Conference(
        oauth_connection_id=conn.id,
        conference_record_name="conferenceRecords/cr-1",
        transcript_resource_name="conferenceRecords/cr-1/transcripts/t-1",
        pipeline_state="pending",
    )
    db_session.add(conf)
    await db_session.commit()
    await db_session.refresh(conf)
    return conf


async def test_fetch_assembles_attributed_text_and_maps_meeting(db_session):
    conf = await _conf_with_meeting(db_session)
    await transcript_service.fetch_transcript(
        db_session, conference=conf, oauth_client=FakeOAuthClient(), meet_client=FakeMeetClient()
    )
    t = await db_session.scalar(select(Transcript).where(Transcript.conference_id == conf.id))
    assert t is not None
    text = decrypt(t.full_text)
    assert "Alice: hello" in text
    assert "Bob: hi there" in text
    assert "(unknown)" in text  # entries with no participant still included
    assert t.language == "en-US"
    assert t.speaker_map["conferenceRecords/cr-1/participants/p1"] == "Alice"
    # meeting was mapped via space
    await db_session.refresh(conf)
    assert conf.meeting_id is not None


async def test_fetch_without_matching_meeting_leaves_meeting_id_none(db_session):
    conf = await _conf_with_meeting(db_session, with_meeting=False)
    await transcript_service.fetch_transcript(
        db_session, conference=conf, oauth_client=FakeOAuthClient(), meet_client=FakeMeetClient()
    )
    await db_session.refresh(conf)
    assert conf.meeting_id is None  # no meeting with that space; allowed


async def test_fetch_empty_transcript_stores_empty(db_session):
    class EmptyMeet(FakeMeetClient):
        async def list_transcript_entries(self, access_token, transcript_resource_name):
            return []

    conf = await _conf_with_meeting(db_session)
    await transcript_service.fetch_transcript(
        db_session, conference=conf, oauth_client=FakeOAuthClient(), meet_client=EmptyMeet()
    )
    t = await db_session.scalar(select(Transcript).where(Transcript.conference_id == conf.id))
    assert t is not None
    assert decrypt(t.full_text) == ""
