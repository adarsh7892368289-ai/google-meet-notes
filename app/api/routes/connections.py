import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_oauth_client
from app.db import get_session
from app.google.oauth_client import OAuthClient
from app.models import User
from app.security import InvalidStateError, create_oauth_state, verify_oauth_state
from app.services import connection_service

router = APIRouter(prefix="/v1/connections/google", tags=["connections"])


@router.get("/start")
async def start(
    current_user: User = Depends(get_current_user),
    oauth_client: OAuthClient = Depends(get_oauth_client),
) -> dict:
    state = create_oauth_state(str(current_user.id))
    return {"authorization_url": oauth_client.build_authorization_url(state)}


@router.get("/callback")
async def callback(
    code: str,
    state: str,
    session: AsyncSession = Depends(get_session),
    oauth_client: OAuthClient = Depends(get_oauth_client),
) -> dict:
    try:
        user_id = verify_oauth_state(state)
    except InvalidStateError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid state")

    user = await session.get(User, uuid.UUID(user_id))
    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown user")

    try:
        bundle = await oauth_client.exchange_code(code)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to exchange authorization code",
        ) from exc
    if not bundle.refresh_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No refresh token returned; re-consent with prompt=consent required",
        )
    try:
        email = await oauth_client.fetch_userinfo(bundle.access_token)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to fetch Google account info",
        ) from exc
    await connection_service.upsert_connection(
        session, user=user, bundle=bundle, google_email=email
    )
    return {"connected": True, "google_email": email}


@router.get("")
async def get_status(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    conn = await connection_service.get_connection(session, current_user)
    if conn is None:
        return {"connected": False}
    return {
        "connected": True,
        "google_email": conn.google_email,
        "status": conn.status,
        "granted_scopes": conn.granted_scopes,
    }


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    oauth_client: OAuthClient = Depends(get_oauth_client),
) -> None:
    conn = await connection_service.get_connection(session, current_user)
    if conn is not None:
        await connection_service.delete_connection(session, conn, oauth_client)
