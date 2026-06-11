import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.google.events_client import EventsClient
from app.google.oauth_client import OAuthClient
from app.models import EventSubscription, OAuthConnection
from app.services import connection_service

logger = logging.getLogger(__name__)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def get_for_connection(
    session: AsyncSession, conn: OAuthConnection
) -> EventSubscription | None:
    return await session.scalar(
        select(EventSubscription).where(
            EventSubscription.oauth_connection_id == conn.id
        )
    )


async def get_by_subscription_name(
    session: AsyncSession, subscription_name: str
) -> EventSubscription | None:
    return await session.scalar(
        select(EventSubscription).where(
            EventSubscription.subscription_name == subscription_name
        )
    )


async def create_for_connection(
    session: AsyncSession,
    *,
    conn: OAuthConnection,
    oauth_client: OAuthClient,
    events_client: EventsClient,
    topic: str,
    ttl_seconds: int,
) -> EventSubscription | None:
    if not conn.google_user_id or not topic:
        logger.info(
            "skipping events subscription for connection %s (missing user id or topic)",
            conn.id,
        )
        return None

    access_token = await connection_service.get_valid_access_token(
        session, conn, oauth_client
    )

    sub = await get_for_connection(session, conn)
    # On reconnect a row already exists; best-effort delete the previous remote
    # subscription so we don't leak an orphaned (and TTL-lived) duplicate that
    # would fire events under a name no longer mapped to any local row.
    if sub is not None:
        try:
            await events_client.delete_subscription(
                access_token, subscription_name=sub.subscription_name
            )
        except Exception as exc:
            logger.warning(
                "failed to delete stale remote subscription %s: %s",
                sub.subscription_name,
                exc,
            )

    result = await events_client.create_subscription(
        access_token,
        google_user_id=conn.google_user_id,
        topic=topic,
        ttl_seconds=ttl_seconds,
    )

    if sub is None:
        sub = EventSubscription(oauth_connection_id=conn.id)
        session.add(sub)
    sub.subscription_name = result.subscription_name
    sub.expire_time = _parse_time(result.expire_time)
    sub.state = "active"
    await session.commit()
    await session.refresh(sub)
    return sub


async def delete_for_connection(
    session: AsyncSession,
    *,
    conn: OAuthConnection,
    oauth_client: OAuthClient,
    events_client: EventsClient,
) -> None:
    sub = await get_for_connection(session, conn)
    if sub is None:
        return
    try:
        access_token = await connection_service.get_valid_access_token(
            session, conn, oauth_client
        )
        await events_client.delete_subscription(
            access_token, subscription_name=sub.subscription_name
        )
    except Exception as exc:  # best-effort remote delete; always remove local row
        logger.warning(
            "failed to delete remote subscription %s: %s", sub.subscription_name, exc
        )
    await session.delete(sub)
    await session.commit()
