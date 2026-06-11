import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_summarizer
from app.config import get_settings
from app.crypto import decrypt
from app.db import get_session
from app.google.gemini_client import Summarizer
from app.models import Conference, Meeting, Notes, OAuthConnection, Transcript, User
from app.schemas.notes import ConferenceResponse, NotesResponse, TranscriptResponse
from app.services import notes_service

router = APIRouter(tags=["notes"])


async def _owned_conference(
    session: AsyncSession, user: User, conference_id: uuid.UUID
) -> Conference | None:
    conf = await session.get(Conference, conference_id)
    if conf is None:
        return None
    conn = await session.get(OAuthConnection, conf.oauth_connection_id)
    if conn is None or conn.user_id != user.id:
        return None
    return conf


async def _owned_meeting(
    session: AsyncSession, user: User, meeting_id: uuid.UUID
) -> Meeting | None:
    meeting = await session.get(Meeting, meeting_id)
    if meeting is None or meeting.user_id != user.id:
        return None
    return meeting


@router.get("/v1/conferences/{conference_id}/notes", response_model=NotesResponse)
async def get_conference_notes(
    conference_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> NotesResponse:
    conf = await _owned_conference(session, current_user, conference_id)
    if conf is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conference not found")
    notes = await session.scalar(select(Notes).where(Notes.conference_id == conf.id))
    if notes is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notes not generated yet")
    return NotesResponse.model_validate(notes)


@router.get("/v1/conferences/{conference_id}/transcript", response_model=TranscriptResponse)
async def get_conference_transcript(
    conference_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> TranscriptResponse:
    conf = await _owned_conference(session, current_user, conference_id)
    if conf is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conference not found")
    transcript = await session.scalar(
        select(Transcript).where(Transcript.conference_id == conf.id)
    )
    if transcript is None or transcript.full_text is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transcript not available")
    return TranscriptResponse(
        conference_id=conf.id,
        language=transcript.language,
        text=decrypt(transcript.full_text),
        speaker_map=transcript.speaker_map,
    )


@router.get("/v1/meetings/{meeting_id}/conferences", response_model=list[ConferenceResponse])
async def list_meeting_conferences(
    meeting_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ConferenceResponse]:
    meeting = await _owned_meeting(session, current_user, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
    rows = await session.scalars(
        select(Conference).where(Conference.meeting_id == meeting.id)
        .order_by(Conference.created_at.desc())
    )
    return [ConferenceResponse.model_validate(c) for c in rows]


@router.get("/v1/meetings/{meeting_id}/notes", response_model=NotesResponse)
async def get_meeting_notes(
    meeting_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> NotesResponse:
    meeting = await _owned_meeting(session, current_user, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
    conf = await session.scalar(
        select(Conference).where(Conference.meeting_id == meeting.id)
        .order_by(Conference.created_at.desc())
    )
    if conf is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No occurrences yet")
    notes = await session.scalar(select(Notes).where(Notes.conference_id == conf.id))
    if notes is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notes not generated yet")
    return NotesResponse.model_validate(notes)


@router.post("/v1/conferences/{conference_id}/notes:regenerate", response_model=NotesResponse)
async def regenerate_notes(
    conference_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    summarizer: Summarizer = Depends(get_summarizer),
) -> NotesResponse:
    conf = await _owned_conference(session, current_user, conference_id)
    if conf is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conference not found")
    transcript = await session.scalar(
        select(Transcript).where(Transcript.conference_id == conf.id)
    )
    if transcript is None or transcript.full_text is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No transcript to summarize for this conference",
        )
    settings = get_settings()
    notes = await notes_service.generate_notes(
        session, conference=conf, summarizer=summarizer, model=settings.gemini_model,
        chunk_threshold=settings.gemini_chunk_token_threshold,
        default_title=settings.notes_default_title,
    )
    return NotesResponse.model_validate(notes)
