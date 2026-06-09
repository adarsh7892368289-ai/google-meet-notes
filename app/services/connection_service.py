# app/services/connection_service.py
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import decrypt, encrypt
from app.google.oauth_client import OAuthClient, TokenBundle
from app.models import OAuthConnection, User

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
) -> OAuthConnection:
    conn = await get_connection(session, user)
    if conn is None:
        conn = OAuthConnection(user_id=user.id)
        session.add(conn)

    conn.google_email = google_email
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
    if _is_fresh(conn):
        return conn.access_token_cache  # type: ignore[return-value]

    async with _lock_for(str(conn.id)):
        await session.refresh(conn)
        if _is_fresh(conn):
            return conn.access_token_cache  # type: ignore[return-value]

        refresh_token = decrypt(conn.refresh_token_encrypted)
        try:
            bundle = await oauth_client.refresh(refresh_token)
        except Exception:
            conn.status = "needs_reconnect"
            await session.commit()
            raise

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
    except Exception:
        pass  # best-effort revoke; still remove locally
    await session.delete(conn)
    await session.commit()
    _locks.pop(str(conn.id), None)
