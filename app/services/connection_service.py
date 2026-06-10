# app/services/connection_service.py
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import decrypt, encrypt
from app.google.oauth_client import OAuthClient, TokenBundle
from app.models import OAuthConnection, User

logger = logging.getLogger(__name__)


class TokenRefreshError(Exception):
    def __init__(self, message: str, *, permanent: bool) -> None:
        super().__init__(message)
        self.permanent = permanent


# refresh a bit early to avoid using a token that expires mid-request
_EXPIRY_SKEW = timedelta(seconds=60)
_locks: dict[str, asyncio.Lock] = {}


def _lock_for(connection_id: str) -> asyncio.Lock:
    lock = _locks.get(connection_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[connection_id] = lock
    return lock


def _expiry_from(expires_in: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=expires_in)


async def get_connection(session: AsyncSession, user: User) -> OAuthConnection | None:
    return await session.scalar(
        select(OAuthConnection).where(OAuthConnection.user_id == user.id)
    )


async def upsert_connection(
    session: AsyncSession,
    *,
    user: User,
    bundle: TokenBundle,
    google_email: str,
    google_user_id: str | None = None,
) -> OAuthConnection:
    conn = await get_connection(session, user)
    if conn is None:
        conn = OAuthConnection(user_id=user.id)
        session.add(conn)

    conn.google_email = google_email
    if google_user_id is not None:
        conn.google_user_id = google_user_id
    if bundle.refresh_token:
        conn.refresh_token_encrypted = encrypt(bundle.refresh_token)
    conn.access_token_cache = bundle.access_token
    conn.access_token_expiry = _expiry_from(bundle.expires_in)
    conn.granted_scopes = bundle.scope.split() if bundle.scope else []
    conn.status = "active"

    await session.commit()
    await session.refresh(conn)
    return conn


def _is_fresh(conn: OAuthConnection) -> bool:
    if conn.access_token_cache is None or conn.access_token_expiry is None:
        return False
    return conn.access_token_expiry - _EXPIRY_SKEW > datetime.now(timezone.utc)


async def get_valid_access_token(
    session: AsyncSession, conn: OAuthConnection, oauth_client: OAuthClient
) -> str:
    if conn.status == "needs_reconnect":
        raise TokenRefreshError("connection needs reconnect", permanent=True)

    if _is_fresh(conn):
        return conn.access_token_cache  # type: ignore[return-value]

    async with _lock_for(str(conn.id)):
        await session.refresh(conn)
        if _is_fresh(conn):
            return conn.access_token_cache  # type: ignore[return-value]

        refresh_token = decrypt(conn.refresh_token_encrypted)
        try:
            bundle = await oauth_client.refresh(refresh_token)
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                conn.status = "needs_reconnect"
                await session.commit()
                logger.warning("permanent token refresh failure for connection %s", conn.id)
                raise TokenRefreshError("refresh rejected", permanent=True) from exc
            logger.warning("transient token refresh failure for connection %s", conn.id)
            raise TokenRefreshError("refresh failed", permanent=False) from exc
        except httpx.RequestError as exc:
            logger.warning("network error during token refresh for connection %s", conn.id)
            raise TokenRefreshError("refresh failed", permanent=False) from exc

        conn.access_token_cache = bundle.access_token
        conn.access_token_expiry = _expiry_from(bundle.expires_in)
        if bundle.scope:
            conn.granted_scopes = bundle.scope.split()
        conn.status = "active"
        await session.commit()
        await session.refresh(conn)
        return conn.access_token_cache  # type: ignore[return-value]


async def delete_connection(
    session: AsyncSession, conn: OAuthConnection, oauth_client: OAuthClient
) -> None:
    try:
        await oauth_client.revoke(decrypt(conn.refresh_token_encrypted))
    except Exception as exc:  # best-effort revoke; never block local disconnect
        logger.warning("failed to revoke token during disconnect of connection %s: %s", conn.id, exc)
    await session.delete(conn)
    await session.commit()
    _locks.pop(str(conn.id), None)
