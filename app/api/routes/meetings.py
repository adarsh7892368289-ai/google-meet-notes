import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_calendar_client,
    get_current_user,
    get_meet_client,
    get_oauth_client,
)
from app.db import get_session
from app.google.calendar_client import CalendarClient
from app.google.meet_client import MeetClient
from app.google.oauth_client import OAuthClient
from app.models import User
from app.schemas.meeting import MeetingCreate, MeetingResponse
from app.services import meeting_service
from app.services.connection_service import TokenRefreshError

router = APIRouter(prefix="/v1/meetings", tags=["meetings"])


@router.post("", response_model=MeetingResponse, status_code=status.HTTP_201_CREATED)
async def create_meeting(
    body: MeetingCreate,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    oauth_client: OAuthClient = Depends(get_oauth_client),
    calendar_client: CalendarClient = Depends(get_calendar_client),
    meet_client: MeetClient = Depends(get_meet_client),
) -> MeetingResponse:
    try:
        meeting, warning = await meeting_service.create_meeting(
            session,
            user=current_user,
            payload=body,
            oauth_client=oauth_client,
            calendar_client=calendar_client,
            meet_client=meet_client,
        )
    except meeting_service.NotConnectedError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No Google account connected. Connect one first.",
        )
    except TokenRefreshError as exc:
        if getattr(exc, "permanent", True):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Google connection needs to be reconnected.",
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Temporary problem reaching Google. Please retry.",
        )
    except httpx.HTTPError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to create the meeting in Google Calendar.",
        )
    response = MeetingResponse.model_validate(meeting)
    response.warning = warning
    return response


@router.get("", response_model=list[MeetingResponse])
async def list_meetings(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[MeetingResponse]:
    meetings = await meeting_service.list_meetings(session, current_user)
    return [MeetingResponse.model_validate(m) for m in meetings]


@router.get("/{meeting_id}", response_model=MeetingResponse)
async def get_meeting(
    meeting_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MeetingResponse:
    meeting = await meeting_service.get_meeting(session, current_user, meeting_id)
    if meeting is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
    return MeetingResponse.model_validate(meeting)


@router.delete("/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meeting(
    meeting_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    oauth_client: OAuthClient = Depends(get_oauth_client),
    calendar_client: CalendarClient = Depends(get_calendar_client),
) -> None:
    deleted = await meeting_service.delete_meeting(
        session,
        user=current_user,
        meeting_id=meeting_id,
        oauth_client=oauth_client,
        calendar_client=calendar_client,
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found")
