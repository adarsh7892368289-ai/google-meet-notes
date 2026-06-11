import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.crypto import encrypt
from app.google.oauth_client import TokenBundle
from app.models import Conference, Meeting, Notes, Transcript, User
from app.schemas.notes import ActionItem, NotesContent
from app.services import connection_service, notes_service


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeSummarizer:
    def __init__(self, *, tokens=10):
        self._tokens = tokens
        self.summarize_calls = []

    async def count_tokens(self, text):
        return self._tokens

    async def summarize(self, transcript):
        self.summarize_calls.append(transcript)
        # echo a stable structure; include marker of how much text it saw
        return NotesContent(
            summary=f"summary of {len(transcript)} chars",
            decisions=["D1"],
            action_items=[ActionItem(who="Alice", what="follow up")],
        )


async def _conf_with_transcript(db_session, *, title="Q3 Sync", text="Alice: hello\nBob: hi", with_meeting=True):
    user = User(email="z@acme.com", name="Z", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="z@acme.com"
    )
    meeting_id = None
    if with_meeting:
        from datetime import datetime, timezone
        m = Meeting(
            user_id=user.id, title=title, start_time=datetime(2026, 6, 11, tzinfo=timezone.utc),
            end_time=datetime(2026, 6, 11, tzinfo=timezone.utc), attendees=[],
            meet_space_name="spaces/S", notes_enabled=True, notes_config={},
        )
        db_session.add(m)
        await db_session.commit()
        await db_session.refresh(m)
        meeting_id = m.id
    conf = Conference(
        oauth_connection_id=conn.id, meeting_id=meeting_id,
        conference_record_name="conferenceRecords/cr-1", pipeline_state="transcript_fetched",
    )
    db_session.add(conf)
    await db_session.commit()
    await db_session.refresh(conf)
    db_session.add(Transcript(conference_id=conf.id, full_text=encrypt(text), language="en-US", speaker_map={}))
    await db_session.commit()
    return conf


async def test_generate_notes_titles_with_meeting_title(db_session):
    conf = await _conf_with_transcript(db_session, title="Q3 Roadmap Sync")
    summarizer = FakeSummarizer()
    notes = await notes_service.generate_notes(
        db_session, conference=conf, summarizer=summarizer, model="gemini-2.5-flash",
        chunk_threshold=600000, default_title="Meeting Notes",
    )
    assert notes.title == "Q3 Roadmap Sync"
    assert notes.summary.startswith("summary of")
    assert notes.decisions == ["D1"]
    assert notes.action_items[0]["who"] == "Alice"
    assert notes.gemini_model == "gemini-2.5-flash"
    assert len(summarizer.summarize_calls) == 1  # small transcript -> single pass


async def test_generate_notes_default_title_without_meeting(db_session):
    conf = await _conf_with_transcript(db_session, with_meeting=False)
    notes = await notes_service.generate_notes(
        db_session, conference=conf, summarizer=FakeSummarizer(), model="m",
        chunk_threshold=600000, default_title="Meeting Notes",
    )
    assert notes.title == "Meeting Notes"


async def test_generate_notes_empty_transcript_skips_summarizer(db_session):
    conf = await _conf_with_transcript(db_session, text="")
    summarizer = FakeSummarizer()
    notes = await notes_service.generate_notes(
        db_session, conference=conf, summarizer=summarizer, model="m",
        chunk_threshold=600000, default_title="Meeting Notes",
    )
    assert summarizer.summarize_calls == []  # nothing to summarize
    assert notes.summary == "No content was captured for this meeting."


async def test_generate_notes_map_reduce_when_over_threshold(db_session):
    conf = await _conf_with_transcript(db_session, text="x" * 5000)
    # threshold tiny so it chunks; tokens reported per call is len(text)
    summarizer = FakeSummarizer(tokens=5000)
    notes = await notes_service.generate_notes(
        db_session, conference=conf, summarizer=summarizer, model="m",
        chunk_threshold=1000, default_title="Meeting Notes",
    )
    # multiple chunk summaries + 1 reduce pass
    assert len(summarizer.summarize_calls) > 1
    assert notes.summary.startswith("summary of")


async def test_generate_notes_is_idempotent_upsert(db_session):
    conf = await _conf_with_transcript(db_session)
    await notes_service.generate_notes(
        db_session, conference=conf, summarizer=FakeSummarizer(), model="m",
        chunk_threshold=600000, default_title="Meeting Notes",
    )
    await notes_service.generate_notes(
        db_session, conference=conf, summarizer=FakeSummarizer(), model="m",
        chunk_threshold=600000, default_title="Meeting Notes",
    )
    rows = (await db_session.scalars(select(Notes).where(Notes.conference_id == conf.id))).all()
    assert len(rows) == 1
