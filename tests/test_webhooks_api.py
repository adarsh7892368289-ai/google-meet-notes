import base64
import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.api.deps import get_job_queue, get_push_verifier
from app.events.oidc import PushVerificationError, VerifiedPush
from app.google.oauth_client import TokenBundle
from app.main import app
from app.models import Conference, EventSubscription, User
from app.queue import NullJobQueue
from app.services import connection_service


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class AllowVerifier:
    def verify(self, authorization_header):
        return VerifiedPush(email="pusher@x")


class DenyVerifier:
    def verify(self, authorization_header):
        raise PushVerificationError("nope")


@pytest.fixture
def shared_queue():
    q = NullJobQueue()
    app.dependency_overrides[get_job_queue] = lambda: q
    yield q
    app.dependency_overrides.pop(get_job_queue, None)


@pytest.fixture
def allow_verifier():
    app.dependency_overrides[get_push_verifier] = lambda: AllowVerifier()
    yield
    app.dependency_overrides.pop(get_push_verifier, None)


@pytest.fixture
def deny_verifier():
    app.dependency_overrides[get_push_verifier] = lambda: DenyVerifier()
    yield
    app.dependency_overrides.pop(get_push_verifier, None)


def _push_body(message_id="msg-1", sub="subscriptions/sub-1", cr="cr-1"):
    data = {"transcript": {"name": f"conferenceRecords/{cr}/transcripts/t-1"}}
    return {
        "subscription": "projects/p/subscriptions/s",
        "message": {
            "data": base64.b64encode(json.dumps(data).encode()).decode(),
            "messageId": message_id,
            "attributes": {
                "ce-type": "google.workspace.meet.transcript.v2.fileGenerated",
                "ce-source": f"//workspaceevents.googleapis.com/{sub}",
            },
        },
    }


async def _seed_subscription(db_session, sub_name="subscriptions/sub-1"):
    user = User(email="m@acme.com", name="M", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="m@acme.com", google_user_id="108"
    )
    db_session.add(
        EventSubscription(oauth_connection_id=conn.id, subscription_name=sub_name, state="active")
    )
    await db_session.commit()


async def test_webhook_accepts_and_creates_conference(
    client, db_session, allow_verifier, shared_queue
):
    await _seed_subscription(db_session)
    resp = await client.post(
        "/v1/webhooks/google/events",
        json=_push_body(),
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    conf = await db_session.scalar(
        select(Conference).where(Conference.conference_record_name == "conferenceRecords/cr-1")
    )
    assert conf is not None
    assert shared_queue.enqueued == [str(conf.id)]


async def test_webhook_rejects_bad_oidc(client, deny_verifier, shared_queue):
    resp = await client.post(
        "/v1/webhooks/google/events",
        json=_push_body(),
        headers={"Authorization": "Bearer bad"},
    )
    assert resp.status_code == 401


async def test_webhook_acks_duplicate(client, db_session, allow_verifier, shared_queue):
    await _seed_subscription(db_session)
    body = _push_body(message_id="dup")
    r1 = await client.post("/v1/webhooks/google/events", json=body,
                           headers={"Authorization": "Bearer t"})
    r2 = await client.post("/v1/webhooks/google/events", json=body,
                           headers={"Authorization": "Bearer t"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    confs = (await db_session.scalars(select(Conference))).all()
    assert len(confs) == 1


async def test_webhook_acks_unparseable_body(client, allow_verifier, shared_queue):
    resp = await client.post(
        "/v1/webhooks/google/events",
        json={"message": {"data": "!!notbase64!!", "messageId": "m", "attributes": {}}},
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 200


async def test_webhook_acks_unmapped_subscription(client, allow_verifier, shared_queue):
    resp = await client.post(
        "/v1/webhooks/google/events",
        json=_push_body(sub="subscriptions/unknown"),
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 200


async def test_webhook_acks_non_json_body(client, allow_verifier, shared_queue):
    resp = await client.post(
        "/v1/webhooks/google/events",
        content=b"<html>not json</html>",
        headers={"Authorization": "Bearer t", "Content-Type": "text/html"},
    )
    assert resp.status_code == 200
