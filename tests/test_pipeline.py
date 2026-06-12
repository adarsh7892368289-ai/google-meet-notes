import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.crypto import encrypt
from app.google.meet_client import (
    ConferenceRecordInfo,
    ParticipantInfo,
    TranscriptEntryInfo,
)
from app.google.oauth_client import TokenBundle
from app.models import Conference, Notes, Transcript, User
from app.schemas.notes import NotesContent
from app.services import connection_service, pipeline


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
    def __init__(self):
        self.entry_calls = 0

    async def get_conference_record(self, access_token, conference_record_name):
        return ConferenceRecordInfo(conference_record_name, "spaces/S", None, None)

    async def list_participants(self, access_token, conference_record_name):
        return [ParticipantInfo(name="conferenceRecords/cr-1/participants/p1", display_name="Alice")]

    async def list_transcript_entries(self, access_token, transcript_resource_name):
        self.entry_calls += 1
        return [TranscriptEntryInfo(participant="conferenceRecords/cr-1/participants/p1", text="hello", language_code="en-US")]


class FakeSummarizer:
    def __init__(self):
        self.calls = 0

    async def count_tokens(self, text):
        return 10

    async def summarize(self, transcript):
        self.calls += 1
        return NotesContent(summary="recap", decisions=[], action_items=[])


async def _conf(db_session, *, state="pending", with_transcript_resource=True):
    user = User(email="p@acme.com", name="P", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="p@acme.com"
    )
    conf = Conference(
        oauth_connection_id=conn.id,
        conference_record_name="conferenceRecords/cr-1",
        transcript_resource_name="conferenceRecords/cr-1/transcripts/t-1" if with_transcript_resource else None,
        pipeline_state=state,
    )
    db_session.add(conf)
    await db_session.commit()
    await db_session.refresh(conf)
    return conf


async def test_run_pipeline_full_advances_to_notes_generated(db_session):
    conf = await _conf(db_session)
    meet = FakeMeetClient()
    summ = FakeSummarizer()
    await pipeline.run_pipeline(
        db_session, conference_id=conf.id, oauth_client=FakeOAuthClient(),
        meet_client=meet, summarizer=summ, model="m", chunk_threshold=600000,
        default_title="Meeting Notes",
    )
    await db_session.refresh(conf)
    assert conf.pipeline_state == "notes_generated"
    assert await db_session.scalar(select(Transcript).where(Transcript.conference_id == conf.id)) is not None
    assert await db_session.scalar(select(Notes).where(Notes.conference_id == conf.id)) is not None


async def test_run_pipeline_resumes_from_transcript_fetched(db_session):
    conf = await _conf(db_session, state="transcript_fetched")
    # seed an existing transcript so stage 1 is already done
    db_session.add(Transcript(conference_id=conf.id, full_text=encrypt("Alice: hi"), language="en-US", speaker_map={}))
    await db_session.commit()
    meet = FakeMeetClient()
    summ = FakeSummarizer()
    await pipeline.run_pipeline(
        db_session, conference_id=conf.id, oauth_client=FakeOAuthClient(),
        meet_client=meet, summarizer=summ, model="m", chunk_threshold=600000,
        default_title="Meeting Notes",
    )
    assert meet.entry_calls == 0  # stage 1 skipped
    assert summ.calls == 1
    await db_session.refresh(conf)
    assert conf.pipeline_state == "notes_generated"


async def test_run_pipeline_already_done_is_noop(db_session):
    conf = await _conf(db_session, state="notes_generated")
    meet = FakeMeetClient()
    summ = FakeSummarizer()
    await pipeline.run_pipeline(
        db_session, conference_id=conf.id, oauth_client=FakeOAuthClient(),
        meet_client=meet, summarizer=summ, model="m", chunk_threshold=600000,
        default_title="Meeting Notes",
    )
    assert meet.entry_calls == 0
    assert summ.calls == 0


async def test_run_pipeline_records_failure_and_reraises(db_session):
    class BoomMeet(FakeMeetClient):
        async def list_transcript_entries(self, access_token, transcript_resource_name):
            raise RuntimeError("meet api down")

    conf = await _conf(db_session)
    with pytest.raises(RuntimeError):
        await pipeline.run_pipeline(
            db_session, conference_id=conf.id, oauth_client=FakeOAuthClient(),
            meet_client=BoomMeet(), summarizer=FakeSummarizer(), model="m",
            chunk_threshold=600000, default_title="Meeting Notes",
        )
    await db_session.refresh(conf)
    assert conf.pipeline_state == "failed"
    assert conf.attempts == 1
    assert "meet api down" in (conf.last_error or "")


async def test_run_pipeline_no_transcript_resource_fails(db_session):
    conf = await _conf(db_session, with_transcript_resource=False)
    with pytest.raises(Exception):
        await pipeline.run_pipeline(
            db_session, conference_id=conf.id, oauth_client=FakeOAuthClient(),
            meet_client=FakeMeetClient(), summarizer=FakeSummarizer(), model="m",
            chunk_threshold=600000, default_title="Meeting Notes",
        )
    await db_session.refresh(conf)
    assert conf.pipeline_state == "failed"
    assert conf.attempts == 1
    assert conf.last_error


async def test_run_pipeline_missing_conference_is_noop(db_session):
    import uuid
    # should not raise
    await pipeline.run_pipeline(
        db_session, conference_id=uuid.uuid4(), oauth_client=FakeOAuthClient(),
        meet_client=FakeMeetClient(), summarizer=FakeSummarizer(), model="m",
        chunk_threshold=600000, default_title="Meeting Notes",
    )


async def test_run_pipeline_stage2_fails_then_retry_resumes(db_session):
    class BoomSummarizer(FakeSummarizer):
        def __init__(self):
            super().__init__()
            self.should_fail = True

        async def summarize(self, transcript):
            if self.should_fail:
                raise RuntimeError("gemini quota exceeded")
            return await super().summarize(transcript)

    conf = await _conf(db_session)
    meet = FakeMeetClient()
    summ = BoomSummarizer()

    with pytest.raises(RuntimeError):
        await pipeline.run_pipeline(
            db_session, conference_id=conf.id, oauth_client=FakeOAuthClient(),
            meet_client=meet, summarizer=summ, model="m", chunk_threshold=600000,
            default_title="Meeting Notes",
        )
    await db_session.refresh(conf)
    assert conf.pipeline_state == "failed"
    assert conf.attempts == 1
    assert await db_session.scalar(select(Transcript).where(Transcript.conference_id == conf.id)) is not None
    assert meet.entry_calls == 1

    summ.should_fail = False
    await pipeline.run_pipeline(
        db_session, conference_id=conf.id, oauth_client=FakeOAuthClient(),
        meet_client=meet, summarizer=summ, model="m", chunk_threshold=600000,
        default_title="Meeting Notes",
    )
    await db_session.refresh(conf)
    assert conf.pipeline_state == "notes_generated"
    assert meet.entry_calls == 2  # stage 1 re-ran (wasteful but safe)
    assert await db_session.scalar(select(Notes).where(Notes.conference_id == conf.id)) is not None
    assert conf.last_error is None  # last_error cleared on success
    assert conf.attempts == 1  # attempts not re-incremented on success
