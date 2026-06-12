import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.events.oidc import GooglePushVerifier, PushVerifier
from app.google.calendar_client import CalendarClient, GoogleCalendarClient
from app.google.events_client import EventsClient, GoogleEventsClient
from app.google.gemini_client import GeminiSummarizer, Summarizer
from app.google.meet_client import GoogleMeetClient, MeetClient
from app.google.oauth_client import GoogleOAuthClient, OAuthClient
from app.models import User
from app.queue import JobQueue, NullJobQueue
from app.security import decode_access_token

_bearer = HTTPBearer(auto_error=True)

# The active job queue. Defaults to the in-process no-op; the app lifespan
# (see app/main.py) swaps in a Redis-backed RealJobQueue at startup when
# REDIS_URL is configured.
_job_queue: JobQueue = NullJobQueue()


def set_job_queue(queue: JobQueue) -> None:
    global _job_queue
    _job_queue = queue


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    creds_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_access_token(credentials.credentials)
        if payload.get("purpose") != "access":
            raise creds_exc
        user_uuid = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise creds_exc

    user = await session.get(User, user_uuid)
    if user is None:
        raise creds_exc
    return user


def get_oauth_client() -> OAuthClient:
    settings = get_settings()
    return GoogleOAuthClient(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        redirect_uri=settings.google_redirect_uri,
        scopes=settings.google_scopes,
    )


def get_calendar_client() -> CalendarClient:
    return GoogleCalendarClient()


def get_meet_client() -> MeetClient:
    return GoogleMeetClient()


def get_events_client() -> EventsClient:
    return GoogleEventsClient()


def get_push_verifier() -> PushVerifier:
    settings = get_settings()
    return GooglePushVerifier(
        expected_audience=settings.push_audience,
        expected_email=settings.push_service_account_email,
    )


def get_job_queue() -> JobQueue:
    # Redis-backed queue only when configured; otherwise the in-process no-op
    # queue (durable `conferences` row remains the source of truth).
    return _job_queue


def get_summarizer() -> Summarizer:
    from google import genai

    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_api_key)
    return GeminiSummarizer(client=client, model=settings.gemini_model)
