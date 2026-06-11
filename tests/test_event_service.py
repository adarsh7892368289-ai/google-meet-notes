import base64
import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.events.parser import parse_push
from app.google.oauth_client import TokenBundle
from app.models import Conference, EventSubscription, ProcessedEvent, User
from app.queue import NullJobQueue
from app.services import connection_service, event_service


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _envelope(*, message_id, ce_type, ce_source, data):
    return {
        "subscription": "projects/p/subscriptions/s",
        "message": {
            "data": base64.b64encode(json.dumps(data).encode()).decode(),
            "messageId": message_id,
            "attributes": {"ce-type": ce_type, "ce-source": ce_source},
        },
    }


def _transcript_env(message_id="msg-1", sub="subscriptions/sub-1", cr="cr-1"):
    return _envelope(
        message_id=message_id,
        ce_type="google.workspace.meet.transcript.v2.fileGenerated",
        ce_source=f"//workspaceevents.googleapis.com/{sub}",
        data={"transcript": {"name": f"conferenceRecords/{cr}/transcripts/t-1"}},
    )


async def _conn_with_sub(db_session, *, sub_name="subscriptions/sub-1"):
    user = User(email="m@acme.com", name="M", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="m@acme.com", google_user_id="108"
    )
    sub = EventSubscription(
        oauth_connection_id=conn.id, subscription_name=sub_name, state="active"
    )
    db_session.add(sub)
    await db_session.commit()
    return conn


async def test_handle_creates_conference_and_enqueues(db_session):
    conn = await _conn_with_sub(db_session)
    queue = NullJobQueue()
    ev = parse_push(_transcript_env())
    result = await event_service.handle_event(db_session, ev, queue)
    assert result == "enqueued"

    conf = await db_session.scalar(
        select(Conference).where(Conference.conference_record_name == "conferenceRecords/cr-1")
    )
    assert conf is not None
    assert conf.oauth_connection_id == conn.id
    assert conf.transcript_resource_name == "conferenceRecords/cr-1/transcripts/t-1"
    assert conf.pipeline_state == "pending"
    assert queue.enqueued == [str(conf.id)]

    ledger = await db_session.scalar(
        select(ProcessedEvent).where(ProcessedEvent.message_id == "msg-1")
    )
    assert ledger is not None


async def test_handle_dedupes_duplicate_message(db_session):
    await _conn_with_sub(db_session)
    queue = NullJobQueue()
    ev = parse_push(_transcript_env(message_id="dup"))
    first = await event_service.handle_event(db_session, ev, queue)
    second = await event_service.handle_event(
        db_session, parse_push(_transcript_env(message_id="dup")), queue
    )
    assert first == "enqueued"
    assert second == "duplicate"
    confs = (await db_session.scalars(select(Conference))).all()
    assert len(confs) == 1
    assert queue.enqueued == [str(confs[0].id)]


async def test_handle_second_event_same_conference_no_duplicate_conference(db_session):
    await _conn_with_sub(db_session)
    queue = NullJobQueue()
    started = _envelope(
        message_id="m-started",
        ce_type="google.workspace.meet.conference.v2.started",
        ce_source="//workspaceevents.googleapis.com/subscriptions/sub-1",
        data={"conferenceRecord": {"name": "conferenceRecords/cr-7"}},
    )
    await event_service.handle_event(db_session, parse_push(started), queue)
    transcript = _transcript_env(message_id="m-trans", cr="cr-7")
    await event_service.handle_event(db_session, parse_push(transcript), queue)

    confs = (
        await db_session.scalars(
            select(Conference).where(
                Conference.conference_record_name == "conferenceRecords/cr-7"
            )
        )
    ).all()
    assert len(confs) == 1
    assert confs[0].transcript_resource_name == "conferenceRecords/cr-7/transcripts/t-1"


async def test_handle_unmappable_subscription_acks_without_conference(db_session):
    queue = NullJobQueue()
    ev = parse_push(_transcript_env(sub="subscriptions/unknown"))
    result = await event_service.handle_event(db_session, ev, queue)
    assert result == "ignored"
    confs = (await db_session.scalars(select(Conference))).all()
    assert confs == []


async def test_handle_event_without_conference_record_is_ignored(db_session):
    await _conn_with_sub(db_session)
    queue = NullJobQueue()
    env = _envelope(
        message_id="m-life",
        ce_type="google.workspace.events.subscription.v1.expirationReminder",
        ce_source="//workspaceevents.googleapis.com/subscriptions/sub-1",
        data={"subscription": {"name": "subscriptions/sub-1"}},
    )
    result = await event_service.handle_event(db_session, parse_push(env), queue)
    assert result == "ignored"
    assert (await db_session.scalars(select(Conference))).all() == []
    ledger = await db_session.scalar(
        select(ProcessedEvent).where(ProcessedEvent.message_id == "m-life")
    )
    assert ledger is not None


async def test_transcript_preserved_when_later_event_has_no_transcript(db_session):
    await _conn_with_sub(db_session)
    queue = NullJobQueue()
    # transcript event first sets the transcript
    await event_service.handle_event(
        db_session, parse_push(_transcript_env(message_id="m1", cr="cr-keep")), queue
    )
    # a later conference.started event for the SAME conference carries no transcript
    started = _envelope(
        message_id="m2",
        ce_type="google.workspace.meet.conference.v2.started",
        ce_source="//workspaceevents.googleapis.com/subscriptions/sub-1",
        data={"conferenceRecord": {"name": "conferenceRecords/cr-keep"}},
    )
    await event_service.handle_event(db_session, parse_push(started), queue)
    conf = await db_session.scalar(
        select(Conference).where(Conference.conference_record_name == "conferenceRecords/cr-keep")
    )
    assert conf.transcript_resource_name == "conferenceRecords/cr-keep/transcripts/t-1"
