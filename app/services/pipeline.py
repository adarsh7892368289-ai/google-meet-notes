import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.google.gemini_client import Summarizer
from app.google.meet_client import MeetClient
from app.google.oauth_client import OAuthClient
from app.models import Conference
from app.services import notes_service, transcript_service

logger = logging.getLogger(__name__)

# Ordered pipeline states. Terminal success state for Phase 5 is "notes_generated".
# Phase 6 appends "doc_created" and "emailed".
_ORDER = {
    "pending": 0,
    "transcript_fetched": 1,
    "notes_generated": 2,
    "failed": -1,
}


def _reached(state: str, target: str) -> bool:
    return _ORDER.get(state, -1) >= _ORDER[target]


async def run_pipeline(
    session: AsyncSession,
    *,
    conference_id: uuid.UUID,
    oauth_client: OAuthClient,
    meet_client: MeetClient,
    summarizer: Summarizer,
    model: str,
    chunk_threshold: int,
    default_title: str,
) -> None:
    conference = await session.get(Conference, conference_id)
    if conference is None:
        logger.warning("pipeline: conference %s not found", conference_id)
        return

    if _reached(conference.pipeline_state, "notes_generated"):
        logger.info("pipeline: conference %s already complete", conference_id)
        return

    try:
        if not _reached(conference.pipeline_state, "transcript_fetched"):
            await transcript_service.fetch_transcript(
                session, conference=conference, oauth_client=oauth_client,
                meet_client=meet_client,
            )
            conference.pipeline_state = "transcript_fetched"
            conference.last_error = None
            await session.commit()

        if not _reached(conference.pipeline_state, "notes_generated"):
            await notes_service.generate_notes(
                session, conference=conference, summarizer=summarizer, model=model,
                chunk_threshold=chunk_threshold, default_title=default_title,
            )
            conference.pipeline_state = "notes_generated"
            conference.last_error = None
            await session.commit()
    except Exception as exc:
        await session.rollback()
        # Reload after rollback; record the failure on a clean transaction.
        conference = await session.get(Conference, conference_id)
        if conference is not None:
            conference.pipeline_state = "failed"
            conference.attempts = (conference.attempts or 0) + 1
            conference.last_error = str(exc)[:2000]
            await session.commit()
        logger.exception("pipeline failed for conference %s", conference_id)
        raise
