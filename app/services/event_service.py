import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.events.parser import MeetEvent
from app.models import Conference, ProcessedEvent
from app.queue import JobQueue
from app.services import subscription_service

logger = logging.getLogger(__name__)


async def _already_processed(session: AsyncSession, message_id: str) -> bool:
    existing = await session.scalar(
        select(ProcessedEvent).where(ProcessedEvent.message_id == message_id)
    )
    return existing is not None


async def handle_event(
    session: AsyncSession, event: MeetEvent, queue: JobQueue
) -> str:
    if await _already_processed(session, event.message_id):
        logger.info("duplicate event %s ignored", event.message_id)
        return "duplicate"

    # Record the message first so retries/duplicates are dropped even if the rest fails.
    # Tradeoff: if the conference step below fails non-transiently the ledger still
    # dedups the redelivery, so the Phase 7 sweeper/reconciliation must backfill any
    # conference that never reached "enqueued".
    ledger = ProcessedEvent(
        message_id=event.message_id,
        event_type=event.event_type,
        conference_record_name=event.conference_record_name,
    )
    session.add(ledger)
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent duplicate beat us to it.
        await session.rollback()
        logger.info("race duplicate event %s ignored", event.message_id)
        return "duplicate"

    if event.conference_record_name is None:
        logger.info("event %s has no conference record; acking", event.message_id)
        return "ignored"

    if event.subscription_name is None:
        logger.warning("event %s has no subscription source; acking", event.message_id)
        return "ignored"

    sub = await subscription_service.get_by_subscription_name(
        session, event.subscription_name
    )
    if sub is None:
        logger.warning(
            "event %s references unknown subscription %s; acking",
            event.message_id,
            event.subscription_name,
        )
        return "ignored"

    conf = await session.scalar(
        select(Conference).where(
            Conference.conference_record_name == event.conference_record_name
        )
    )
    if conf is None:
        conf = Conference(
            oauth_connection_id=sub.oauth_connection_id,
            conference_record_name=event.conference_record_name,
            pipeline_state="pending",
        )
        session.add(conf)

    if event.transcript_resource_name is not None:
        conf.transcript_resource_name = event.transcript_resource_name

    try:
        await session.commit()
    except IntegrityError:
        # Another worker inserted the same conference concurrently; reload it.
        await session.rollback()
        conf = await session.scalar(
            select(Conference).where(
                Conference.conference_record_name == event.conference_record_name
            )
        )
        if conf is None:
            logger.warning("conference vanished after race for %s", event.message_id)
            return "ignored"
        # The rolled-back insert discarded our transcript assignment; re-apply it
        # to the winner's row if it doesn't already have one.
        if (
            event.transcript_resource_name is not None
            and conf.transcript_resource_name is None
        ):
            conf.transcript_resource_name = event.transcript_resource_name
            await session.commit()

    await session.refresh(conf)
    await queue.enqueue_notes_pipeline(str(conf.id))
    return "enqueued"
