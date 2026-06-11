import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.crypto import decrypt, encrypt
from app.google.oauth_client import TokenBundle
from app.models import Conference, Transcript, User
from app.services import connection_service


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _conference(db_session) -> Conference:
    user = User(email="t@acme.com", name="T", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="t@acme.com"
    )
    conf = Conference(
        oauth_connection_id=conn.id,
        conference_record_name="conferenceRecords/cr-1",
        pipeline_state="pending",
    )
    db_session.add(conf)
    await db_session.commit()
    await db_session.refresh(conf)
    return conf


async def test_transcript_stores_encrypted_full_text(db_session):
    conf = await _conference(db_session)
    t = Transcript(
        conference_id=conf.id,
        full_text=encrypt("alice: hello\nbob: hi"),
        language="en-US",
        speaker_map={"conferenceRecords/cr-1/participants/p1": "Alice"},
    )
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    assert t.id is not None
    assert decrypt(t.full_text) == "alice: hello\nbob: hi"
    assert t.speaker_map["conferenceRecords/cr-1/participants/p1"] == "Alice"


async def test_transcript_one_per_conference(db_session):
    from sqlalchemy.exc import IntegrityError
    conf = await _conference(db_session)
    db_session.add(Transcript(conference_id=conf.id, full_text=b"x"))
    await db_session.commit()
    db_session.add(Transcript(conference_id=conf.id, full_text=b"y"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
