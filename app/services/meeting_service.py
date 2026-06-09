import logging
import uuid

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.google.calendar_client import CalendarClient
from app.google.meet_client import MeetClient
from app.google.oauth_client import OAuthClient
from app.models import Meeting, User
from app.schemas.meeting import MeetingCreate
from app.services import connection_service

logger = logging.getLogger(__name__)


class NotConnectedError(Exception):
    pass


async def create_meeting(
    session: AsyncSession,
    *,
    user: User,
    payload: MeetingCreate,
    oauth_client: OAuthClient,
    calendar_client: CalendarClient,
    meet_client: MeetClient,
) -> tuple[Meeting, str | None]:
    conn = await connection_service.get_connection(session, user)
    if conn is None:
        raise NotConnectedError("no google account connected")

    access_token = await connection_service.get_valid_access_token(session, conn, oauth_client)

    created = await calendar_client.create_event(
        access_token,
        summary=payload.title,
        description=payload.description,
        start=payload.start_time,
        end=payload.end_time,
        attendees=[str(e) for e in payload.attendees],
    )

    notes_enabled = payload.notes_enabled
    warning: str | None = None
    if notes_enabled:
        if not created.meeting_code:
            notes_enabled = False
            warning = "Meeting created but no Meet link was generated; notes disabled."
        else:
            try:
                space_name = await meet_client.get_space_name(access_token, created.meeting_code)
                await meet_client.enable_auto_transcript(access_token, space_name)
            except httpx.HTTPError:
                logger.warning(
                    "could not enable auto-transcript for meeting code %s (user %s)",
                    created.meeting_code,
                    user.id,
                    exc_info=True,
                )
                notes_enabled = False
                warning = (
                    "Meeting created, but automatic notes could not be enabled. "
                    "This usually means the Google account's plan does not support "
                    "Meet transcripts."
                )

    meeting = Meeting(
        user_id=user.id,
        title=payload.title,
        description=payload.description,
        start_time=payload.start_time,
        end_time=payload.end_time,
        attendees=[str(e) for e in payload.attendees],
        calendar_event_id=created.event_id,
        meet_join_uri=created.meet_uri,
        meeting_code=created.meeting_code,
        notes_enabled=notes_enabled,
        notes_config=payload.notes_config.model_dump(),
        status="scheduled",
    )
    session.add(meeting)
    await session.commit()
    await session.refresh(meeting)
    return meeting, warning


async def list_meetings(session: AsyncSession, user: User) -> list[Meeting]:
    result = await session.scalars(
        select(Meeting).where(Meeting.user_id == user.id).order_by(Meeting.start_time.desc())
    )
    return list(result)


async def get_meeting(
    session: AsyncSession, user: User, meeting_id: uuid.UUID
) -> Meeting | None:
    meeting = await session.get(Meeting, meeting_id)
    if meeting is None or meeting.user_id != user.id:
        return None
    return meeting


async def delete_meeting(
    session: AsyncSession,
    *,
    user: User,
    meeting_id: uuid.UUID,
    oauth_client: OAuthClient,
    calendar_client: CalendarClient,
) -> bool:
    meeting = await get_meeting(session, user, meeting_id)
    if meeting is None:
        return False
    if meeting.calendar_event_id:
        try:
            conn = await connection_service.get_connection(session, user)
            if conn is not None:
                access_token = await connection_service.get_valid_access_token(
                    session, conn, oauth_client
                )
                await calendar_client.delete_event(access_token, meeting.calendar_event_id)
        except Exception:
            logger.warning(
                "failed to delete calendar event for meeting %s", meeting_id, exc_info=True
            )
    await session.delete(meeting)
    await session.commit()
    return True
