import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import encrypt
from app.google.meet_client import MeetClient
from app.google.oauth_client import OAuthClient
from app.models import Conference, Meeting, OAuthConnection, Transcript
from app.services import connection_service

logger = logging.getLogger(__name__)


class TranscriptNotReadyError(Exception):
    """No transcript resource is associated with the conference yet."""


async def _access_token(
    session: AsyncSession, conference: Conference, oauth_client: OAuthClient
) -> str:
    conn = await session.get(OAuthConnection, conference.oauth_connection_id)
    if conn is None:
        raise TranscriptNotReadyError("conference has no connection")
    return await connection_service.get_valid_access_token(session, conn, oauth_client)


async def _map_meeting(session: AsyncSession, conference: Conference, space: str | None) -> None:
    if conference.meeting_id is not None or not space:
        return
    conn = await session.get(OAuthConnection, conference.oauth_connection_id)
    if conn is None:
        return
    # Scope to the conference owner's meetings: a conference only ever maps to a
    # meeting created by the same user, even though Meet space names are globally
    # unique. This keeps the ownership invariant explicit rather than assumed.
    meeting = await session.scalar(
        select(Meeting).where(
            Meeting.meet_space_name == space, Meeting.user_id == conn.user_id
        )
    )
    if meeting is not None:
        conference.meeting_id = meeting.id


def _assemble(entries, speaker_map: dict) -> tuple[str, str | None]:
    lines: list[str] = []
    language: str | None = None
    for e in entries:
        if language is None and e.language_code:
            language = e.language_code
        speaker = speaker_map.get(e.participant or "", "Unknown") if e.participant else "Unknown"
        lines.append(f"{speaker}: {e.text}")
    return "\n".join(lines), language


async def fetch_transcript(
    session: AsyncSession,
    *,
    conference: Conference,
    oauth_client: OAuthClient,
    meet_client: MeetClient,
) -> Transcript:
    if not conference.transcript_resource_name:
        raise TranscriptNotReadyError("conference has no transcript resource yet")

    access_token = await _access_token(session, conference, oauth_client)

    record = await meet_client.get_conference_record(
        access_token, conference.conference_record_name
    )
    await _map_meeting(session, conference, record.space)

    participants = await meet_client.list_participants(
        access_token, conference.conference_record_name
    )
    speaker_map = {p.name: p.display_name for p in participants}

    entries = await meet_client.list_transcript_entries(
        access_token, conference.transcript_resource_name
    )
    full_text, language = _assemble(entries, speaker_map)

    existing = await session.scalar(
        select(Transcript).where(Transcript.conference_id == conference.id)
    )
    if existing is None:
        existing = Transcript(conference_id=conference.id)
        session.add(existing)
    existing.full_text = encrypt(full_text)
    existing.language = language
    existing.speaker_map = speaker_map

    await session.commit()
    await session.refresh(existing)
    return existing
