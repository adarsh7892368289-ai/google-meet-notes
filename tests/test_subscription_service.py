import pytest
from cryptography.fernet import Fernet

from app.google.events_client import SubscriptionResult
from app.google.oauth_client import TokenBundle
from app.models import EventSubscription, User
from app.services import connection_service, subscription_service
from sqlalchemy import select


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


class FakeEventsClient:
    def __init__(self):
        self.created = []
        self.deleted = []

    async def create_subscription(self, access_token, *, google_user_id, topic, ttl_seconds):
        self.created.append((google_user_id, topic, ttl_seconds))
        return SubscriptionResult(
            subscription_name="subscriptions/sub-1",
            expire_time="2026-06-20T00:00:00Z",
            state="ACTIVE",
        )

    async def renew_subscription(self, access_token, *, subscription_name, ttl_seconds):
        return SubscriptionResult(subscription_name, "2026-06-27T00:00:00Z", "ACTIVE")

    async def delete_subscription(self, access_token, *, subscription_name):
        self.deleted.append(subscription_name)


async def _conn(db_session, *, user_id="108"):
    user = User(email="m@acme.com", name="M", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    return await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="m@acme.com", google_user_id=user_id
    )


async def test_create_subscription_persists_row(db_session):
    conn = await _conn(db_session)
    events = FakeEventsClient()
    sub = await subscription_service.create_for_connection(
        db_session, conn=conn, oauth_client=FakeOAuthClient(), events_client=events,
        topic="projects/p/topics/meet-events", ttl_seconds=604800,
    )
    assert sub.subscription_name == "subscriptions/sub-1"
    assert sub.state == "active"
    assert events.created == [("108", "projects/p/topics/meet-events", 604800)]
    row = await db_session.scalar(
        select(EventSubscription).where(EventSubscription.oauth_connection_id == conn.id)
    )
    assert row is not None


async def test_create_subscription_noop_without_user_id(db_session):
    conn = await _conn(db_session, user_id=None)
    events = FakeEventsClient()
    sub = await subscription_service.create_for_connection(
        db_session, conn=conn, oauth_client=FakeOAuthClient(), events_client=events,
        topic="projects/p/topics/meet-events", ttl_seconds=604800,
    )
    assert sub is None
    assert events.created == []


async def test_create_subscription_noop_without_topic(db_session):
    conn = await _conn(db_session)
    events = FakeEventsClient()
    sub = await subscription_service.create_for_connection(
        db_session, conn=conn, oauth_client=FakeOAuthClient(), events_client=events,
        topic="", ttl_seconds=604800,
    )
    assert sub is None
    assert events.created == []


async def test_delete_subscription_removes_row_and_calls_api(db_session):
    conn = await _conn(db_session)
    events = FakeEventsClient()
    await subscription_service.create_for_connection(
        db_session, conn=conn, oauth_client=FakeOAuthClient(), events_client=events,
        topic="projects/p/topics/meet-events", ttl_seconds=604800,
    )
    await subscription_service.delete_for_connection(
        db_session, conn=conn, oauth_client=FakeOAuthClient(), events_client=events,
    )
    assert events.deleted == ["subscriptions/sub-1"]
    row = await db_session.scalar(
        select(EventSubscription).where(EventSubscription.oauth_connection_id == conn.id)
    )
    assert row is None


async def test_get_by_subscription_name(db_session):
    conn = await _conn(db_session)
    events = FakeEventsClient()
    await subscription_service.create_for_connection(
        db_session, conn=conn, oauth_client=FakeOAuthClient(), events_client=events,
        topic="projects/p/topics/meet-events", ttl_seconds=604800,
    )
    found = await subscription_service.get_by_subscription_name(db_session, "subscriptions/sub-1")
    assert found is not None
    assert found.oauth_connection_id == conn.id


class FailingDeleteEventsClient(FakeEventsClient):
    async def delete_subscription(self, access_token, *, subscription_name):
        raise RuntimeError("remote boom")


async def test_delete_for_connection_removes_local_row_even_if_remote_delete_fails(db_session):
    conn = await _conn(db_session)
    events = FailingDeleteEventsClient()
    await subscription_service.create_for_connection(
        db_session, conn=conn, oauth_client=FakeOAuthClient(), events_client=events,
        topic="projects/p/topics/meet-events", ttl_seconds=604800,
    )
    await subscription_service.delete_for_connection(
        db_session, conn=conn, oauth_client=FakeOAuthClient(), events_client=events,
    )
    row = await db_session.scalar(
        select(EventSubscription).where(EventSubscription.oauth_connection_id == conn.id)
    )
    assert row is None


async def test_create_for_connection_deletes_previous_subscription_on_reconnect(db_session):
    conn = await _conn(db_session)
    events = FakeEventsClient()
    await subscription_service.create_for_connection(
        db_session, conn=conn, oauth_client=FakeOAuthClient(), events_client=events,
        topic="projects/p/topics/meet-events", ttl_seconds=604800,
    )
    # second connect (reconnect) for the same connection
    await subscription_service.create_for_connection(
        db_session, conn=conn, oauth_client=FakeOAuthClient(), events_client=events,
        topic="projects/p/topics/meet-events", ttl_seconds=604800,
    )
    # the previous remote subscription was deleted before the new create
    assert events.deleted == ["subscriptions/sub-1"]
    # still exactly one local row
    rows = (await db_session.scalars(
        select(EventSubscription).where(EventSubscription.oauth_connection_id == conn.id)
    )).all()
    assert len(rows) == 1
