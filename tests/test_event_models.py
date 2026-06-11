import uuid
from datetime import datetime, timezone

from app.models import Conference, EventSubscription, ProcessedEvent, User
from app.services import connection_service
from app.google.oauth_client import TokenBundle
from cryptography.fernet import Fernet
import pytest


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _conn(db_session):
    user = User(email="m@acme.com", name="M", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    return await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="m@acme.com"
    )


async def test_event_subscription_round_trips(db_session):
    conn = await _conn(db_session)
    sub = EventSubscription(
        oauth_connection_id=conn.id,
        subscription_name="subscriptions/abc123",
        expire_time=datetime(2026, 6, 20, tzinfo=timezone.utc),
        state="active",
    )
    db_session.add(sub)
    await db_session.commit()
    await db_session.refresh(sub)
    assert sub.id is not None
    assert sub.state == "active"


async def test_conference_unique_record_name(db_session):
    conn = await _conn(db_session)
    c = Conference(
        oauth_connection_id=conn.id,
        conference_record_name="conferenceRecords/xyz",
        pipeline_state="pending",
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    assert c.meeting_id is None  # nullable in this phase
    assert c.attempts == 0


async def test_processed_event_unique_message_id(db_session):
    ev = ProcessedEvent(
        message_id="msg-1",
        event_type="google.workspace.meet.transcript.v2.fileGenerated",
        conference_record_name="conferenceRecords/xyz",
    )
    db_session.add(ev)
    await db_session.commit()
    await db_session.refresh(ev)
    assert ev.id is not None


async def test_conference_record_name_is_unique(db_session):
    from sqlalchemy.exc import IntegrityError
    conn = await _conn(db_session)
    db_session.add(Conference(
        oauth_connection_id=conn.id, conference_record_name="conferenceRecords/dup",
        pipeline_state="pending",
    ))
    await db_session.commit()
    db_session.add(Conference(
        oauth_connection_id=conn.id, conference_record_name="conferenceRecords/dup",
        pipeline_state="pending",
    ))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_processed_event_message_id_is_unique(db_session):
    from sqlalchemy.exc import IntegrityError
    db_session.add(ProcessedEvent(message_id="dup", event_type="t"))
    await db_session.commit()
    db_session.add(ProcessedEvent(message_id="dup", event_type="t"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
