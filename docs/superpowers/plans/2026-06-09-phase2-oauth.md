# Phase 2: Google Account Linking (OAuth) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an authenticated app user connect their Google account via OAuth 2.0, securely store an encrypted refresh token, expose connection status, and provide a reusable "get a valid access token" service (auto-refresh) that later phases use to call Google APIs.

**Architecture:** Builds on Phase 1. All Google network access is funneled through an injectable OAuth client (`app/google/oauth_client.py`) so business logic and routes are testable with a fake. Refresh tokens are encrypted at rest (Fernet). The OAuth `state` is a short-lived signed token carrying the user id, so the unauthenticated browser callback can be mapped back to the user. Token refresh uses a per-connection single-flight lock.

**Tech Stack:** Adds `httpx` (promoted to a runtime dependency) and `cryptography` (Fernet) to the Phase 1 stack (FastAPI, async SQLAlchemy, Alembic, PyJWT, bcrypt, pytest/pytest-asyncio).

**Prerequisites already in place from Phase 1:** `app/config.py` (`Settings`, `get_settings`), `app/db.py` (`Base`, `get_session`), `app/models/User`, `app/security.py` (JWT helpers), `app/api/deps.py` (`get_current_user`), `tests/conftest.py` (`db_session`, `client` fixtures with `get_session` override; `pyproject.toml` has `asyncio_default_fixture_loop_scope = "session"` and `asyncio_default_test_loop_scope = "session"`). PostgreSQL running locally (`meetnotes`, `meetnotes_test`).

---

## File structure (created/modified by this plan)

```
app/
  config.py            # MODIFY: add Google OAuth + encryption settings
  crypto.py            # CREATE: Fernet encrypt/decrypt
  security.py          # MODIFY: add OAuth state sign/verify helpers
  models/
    __init__.py        # MODIFY: export OAuthConnection
    oauth_connection.py# CREATE: OAuthConnection ORM model
  google/
    __init__.py        # CREATE (empty)
    oauth_client.py    # CREATE: TokenBundle, OAuthClient protocol, GoogleOAuthClient
  services/
    connection_service.py # CREATE: store/get/refresh/delete connection
  api/
    deps.py            # MODIFY: add get_oauth_client dependency
    routes/
      connections.py   # CREATE: start/callback/status/delete routes
  main.py              # MODIFY: include connections router
migrations/versions/
  <auto>_oauth_connections.py # CREATE via alembic autogenerate
tests/
  test_crypto.py       # CREATE
  test_oauth_state.py  # CREATE
  test_oauth_client.py # CREATE (httpx MockTransport)
  test_connection_service.py # CREATE
  test_connections_api.py    # CREATE (fake oauth client)
.env.example           # MODIFY: document new env vars
```

**Environment note:** Windows PowerShell — chain commands with `;` not `&&`. Use the venv: `.venv\Scripts\python.exe -m ...`. Running Python is 3.14. `psql` at `C:\Program Files\PostgreSQL\17\bin\psql.exe` (PGPASSWORD=postgres).

---

## Task 1: Dependencies & config

**Files:**
- Modify: `pyproject.toml`
- Modify: `app/config.py`
- Modify: `.env.example`
- Test: `tests/test_config.py` (extend)

- [ ] **Step 1: Add dependencies**

In `pyproject.toml`, move `httpx` from `[project.optional-dependencies].dev` into the main `dependencies` list and add `cryptography`. The main `dependencies` should now include these two lines (add them; keep existing entries):

```toml
    "httpx>=0.27",
    "cryptography>=43.0",
```

Remove the now-duplicate `httpx>=0.27` from the `dev` extras (leave `pytest`, `pytest-asyncio` there).

Then reinstall:

Run: `.venv\Scripts\python.exe -m pip install -e ".[dev]"`
Expected: succeeds; `cryptography` installed.

- [ ] **Step 2: Write the failing config test (extend existing file)**

Append to `tests/test_config.py`:

```python
def test_settings_reads_google_oauth_values(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://app.example.com/cb")
    monkeypatch.setenv("ENCRYPTION_KEY", "k" * 44)

    settings = Settings()

    assert settings.google_client_id == "cid"
    assert settings.google_client_secret == "secret"
    assert settings.google_redirect_uri == "https://app.example.com/cb"
    assert settings.encryption_key == "k" * 44
    assert "openid" in settings.google_scopes
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_config.py::test_settings_reads_google_oauth_values -v`
Expected: FAIL (`AttributeError`/validation — fields don't exist yet)

- [ ] **Step 4: Add settings fields**

In `app/config.py`, add these fields to the `Settings` class (after the existing fields):

```python
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/v1/connections/google/callback"
    google_scopes: str = (
        "openid email "
        "https://www.googleapis.com/auth/meetings.space.created "
        "https://www.googleapis.com/auth/meetings.space.settings "
        "https://www.googleapis.com/auth/calendar.events "
        "https://www.googleapis.com/auth/drive.file "
        "https://www.googleapis.com/auth/gmail.send"
    )
    encryption_key: str = ""
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_config.py -v`
Expected: PASS (both config tests)

- [ ] **Step 6: Document env vars**

Append to `.env.example`:

```dotenv

# Google OAuth
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_REDIRECT_URI=http://localhost:8000/v1/connections/google/callback

# Encryption (generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
ENCRYPTION_KEY=
```

Also generate a real key for local dev and set it in your `.env`:

Run: `.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
Then put the printed value after `ENCRYPTION_KEY=` in `.env` (NOT `.env.example`).

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml app/config.py .env.example tests/test_config.py
git commit -m "feat: google oauth and encryption settings"
```

---

## Task 2: Encryption utility (`app/crypto.py`)

**Files:**
- Create: `app/crypto.py`
- Test: `tests/test_crypto.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_crypto.py
import pytest
from cryptography.fernet import Fernet

from app.crypto import decrypt, encrypt


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_encrypt_decrypt_roundtrip():
    token = encrypt("my-refresh-token")
    assert isinstance(token, bytes)
    assert token != b"my-refresh-token"
    assert decrypt(token) == "my-refresh-token"


def test_decrypt_rejects_tampered_token():
    token = encrypt("secret")
    with pytest.raises(Exception):
        decrypt(token + b"x")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_crypto.py -v`
Expected: FAIL (`ModuleNotFoundError: app.crypto`)

- [ ] **Step 3: Write implementation**

```python
# app/crypto.py
from cryptography.fernet import Fernet

from app.config import get_settings


def _fernet() -> Fernet:
    key = get_settings().encryption_key
    if not key:
        raise RuntimeError("ENCRYPTION_KEY is not configured")
    return Fernet(key.encode())


def encrypt(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt(token: bytes) -> str:
    return _fernet().decrypt(token).decode("utf-8")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_crypto.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add app/crypto.py tests/test_crypto.py
git commit -m "feat: fernet encryption utility"
```

---

## Task 3: OAuth state sign/verify (`app/security.py`)

**Files:**
- Modify: `app/security.py`
- Test: `tests/test_oauth_state.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_oauth_state.py
import pytest

from app.security import (
    InvalidStateError,
    create_oauth_state,
    verify_oauth_state,
)


def test_state_roundtrip():
    state = create_oauth_state("user-123")
    assert verify_oauth_state(state) == "user-123"


def test_verify_rejects_garbage():
    with pytest.raises(InvalidStateError):
        verify_oauth_state("not-a-real-token")


def test_verify_rejects_token_with_wrong_purpose():
    # an access token (no oauth_state purpose) must not be accepted as state
    from app.security import create_access_token

    token = create_access_token(subject="user-123")
    with pytest.raises(InvalidStateError):
        verify_oauth_state(token)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_oauth_state.py -v`
Expected: FAIL (`ImportError`)

- [ ] **Step 3: Add implementation to `app/security.py`**

Add these imports/constants/functions to `app/security.py` (keep existing code):

```python
from datetime import datetime, timedelta, timezone  # already imported at top; do not duplicate

_OAUTH_STATE_PURPOSE = "oauth_state"
_OAUTH_STATE_TTL_MINUTES = 10


class InvalidStateError(Exception):
    pass


def create_oauth_state(user_id: str) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=_OAUTH_STATE_TTL_MINUTES)
    payload = {"sub": user_id, "purpose": _OAUTH_STATE_PURPOSE, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def verify_oauth_state(token: str) -> str:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidStateError("invalid state token") from exc
    if payload.get("purpose") != _OAUTH_STATE_PURPOSE:
        raise InvalidStateError("wrong token purpose")
    subject = payload.get("sub")
    if not subject:
        raise InvalidStateError("missing subject")
    return subject
```

> Note: `datetime`, `timedelta`, `timezone`, `jwt`, `_ALGORITHM`, and `get_settings` are already imported/defined in `security.py` from Phase 1. Do not duplicate the import line shown above — it is only there to indicate the names used. Add only the constants, the `InvalidStateError` class, and the two functions.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_oauth_state.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/security.py tests/test_oauth_state.py
git commit -m "feat: signed oauth state token helpers"
```

---

## Task 4: OAuthConnection model + migration

**Files:**
- Create: `app/models/oauth_connection.py`
- Modify: `app/models/__init__.py`
- Create migration via alembic autogenerate

- [ ] **Step 1: Write the model**

```python
# app/models/oauth_connection.py
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, func
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class OAuthConnection(Base):
    __tablename__ = "oauth_connections"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    google_email: Mapped[str] = mapped_column(String(320), nullable=False)
    refresh_token_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    access_token_cache: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    access_token_expiry: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    granted_scopes: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
```

- [ ] **Step 2: Export it**

Update `app/models/__init__.py`:

```python
# app/models/__init__.py
from app.models.oauth_connection import OAuthConnection
from app.models.user import User

__all__ = ["User", "OAuthConnection"]
```

- [ ] **Step 3: Sanity import**

Run: `.venv\Scripts\python.exe -c "from app.models import OAuthConnection; print(OAuthConnection.__tablename__)"`
Expected: prints `oauth_connections`

- [ ] **Step 4: Autogenerate migration**

Run: `.venv\Scripts\python.exe -m alembic revision --autogenerate -m "create oauth_connections table"`
Expected: a new migration with `op.create_table("oauth_connections", ...)` including the FK to `users`.

- [ ] **Step 5: Apply migration**

Run: `.venv\Scripts\python.exe -m alembic upgrade head`
Expected: success; verify with `& "C:\Program Files\PostgreSQL\17\bin\psql.exe" -U postgres -h localhost -p 5432 -d meetnotes -c "\d oauth_connections"` (PGPASSWORD=postgres) — table exists.

- [ ] **Step 6: Commit**

```bash
git add app/models/oauth_connection.py app/models/__init__.py migrations/versions
git commit -m "feat: oauth_connections model and migration"
```

---

## Task 5: Google OAuth client (`app/google/oauth_client.py`)

**Files:**
- Create: `app/google/__init__.py` (empty)
- Create: `app/google/oauth_client.py`
- Test: `tests/test_oauth_client.py`

- [ ] **Step 1: Write the failing test (uses httpx MockTransport — no real network)**

```python
# tests/test_oauth_client.py
import httpx
import pytest

from app.google.oauth_client import GoogleOAuthClient, TokenBundle


def _client_with_handler(handler) -> GoogleOAuthClient:
    transport = httpx.MockTransport(handler)
    return GoogleOAuthClient(
        client_id="cid",
        client_secret="csecret",
        redirect_uri="https://app/cb",
        scopes="openid email",
        transport=transport,
    )


def test_build_authorization_url_contains_required_params():
    client = _client_with_handler(lambda req: httpx.Response(200))
    url = client.build_authorization_url(state="xyz")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=cid" in url
    assert "response_type=code" in url
    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "state=xyz" in url
    assert "scope=openid+email" in url or "scope=openid%20email" in url


async def test_exchange_code_returns_token_bundle():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        assert b"grant_type=authorization_code" in request.content
        return httpx.Response(
            200,
            json={
                "access_token": "at-1",
                "refresh_token": "rt-1",
                "expires_in": 3599,
                "scope": "openid email",
                "token_type": "Bearer",
            },
        )

    client = _client_with_handler(handler)
    bundle = await client.exchange_code("the-code")
    assert isinstance(bundle, TokenBundle)
    assert bundle.access_token == "at-1"
    assert bundle.refresh_token == "rt-1"
    assert bundle.expires_in == 3599


async def test_refresh_returns_token_bundle():
    def handler(request: httpx.Request) -> httpx.Response:
        assert b"grant_type=refresh_token" in request.content
        return httpx.Response(
            200,
            json={"access_token": "at-2", "expires_in": 3599, "scope": "openid email"},
        )

    client = _client_with_handler(handler)
    bundle = await client.refresh("rt-1")
    assert bundle.access_token == "at-2"
    assert bundle.refresh_token is None


async def test_fetch_userinfo_returns_email():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer at-1"
        return httpx.Response(200, json={"email": "user@acme.com"})

    client = _client_with_handler(handler)
    email = await client.fetch_userinfo("at-1")
    assert email == "user@acme.com"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_oauth_client.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write implementation**

```python
# app/google/oauth_client.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

import httpx

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
REVOKE_URI = "https://oauth2.googleapis.com/revoke"
USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"


@dataclass
class TokenBundle:
    access_token: str
    expires_in: int
    scope: str
    refresh_token: str | None = None


class OAuthClient(Protocol):
    def build_authorization_url(self, state: str) -> str: ...
    async def exchange_code(self, code: str) -> TokenBundle: ...
    async def refresh(self, refresh_token: str) -> TokenBundle: ...
    async def fetch_userinfo(self, access_token: str) -> str: ...
    async def revoke(self, token: str) -> None: ...


class GoogleOAuthClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._scopes = scopes
        self._transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=30.0)

    def build_authorization_url(self, state: str) -> str:
        params = {
            "client_id": self._client_id,
            "redirect_uri": self._redirect_uri,
            "response_type": "code",
            "scope": self._scopes,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        return f"{AUTH_URI}?{urlencode(params)}"

    @staticmethod
    def _to_bundle(data: dict) -> TokenBundle:
        return TokenBundle(
            access_token=data["access_token"],
            expires_in=int(data.get("expires_in", 0)),
            scope=data.get("scope", ""),
            refresh_token=data.get("refresh_token"),
        )

    async def exchange_code(self, code: str) -> TokenBundle:
        form = {
            "code": code,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": self._redirect_uri,
            "grant_type": "authorization_code",
        }
        async with self._http() as http:
            resp = await http.post(TOKEN_URI, data=form)
            resp.raise_for_status()
            return self._to_bundle(resp.json())

    async def refresh(self, refresh_token: str) -> TokenBundle:
        form = {
            "refresh_token": refresh_token,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "grant_type": "refresh_token",
        }
        async with self._http() as http:
            resp = await http.post(TOKEN_URI, data=form)
            resp.raise_for_status()
            return self._to_bundle(resp.json())

    async def fetch_userinfo(self, access_token: str) -> str:
        async with self._http() as http:
            resp = await http.get(
                USERINFO_URI, headers={"Authorization": f"Bearer {access_token}"}
            )
            resp.raise_for_status()
            return resp.json()["email"]

    async def revoke(self, token: str) -> None:
        async with self._http() as http:
            await http.post(REVOKE_URI, data={"token": token})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_oauth_client.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add app/google/__init__.py app/google/oauth_client.py tests/test_oauth_client.py
git commit -m "feat: google oauth client (httpx, injectable)"
```

---

## Task 6: Connection service (`app/services/connection_service.py`)

**Files:**
- Create: `app/services/connection_service.py`
- Test: `tests/test_connection_service.py`

This service persists connections (encrypting the refresh token), returns a valid access token (refreshing when expired), and deletes connections. It depends on an `OAuthClient` (passed in, so tests use a fake).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_connection_service.py
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from app.google.oauth_client import TokenBundle
from app.models import User
from app.services import connection_service


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeOAuthClient:
    def __init__(self):
        self.refresh_calls = 0
        self.revoked = []

    def build_authorization_url(self, state: str) -> str:
        return f"https://auth?state={state}"

    async def exchange_code(self, code: str) -> TokenBundle:
        return TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")

    async def refresh(self, refresh_token: str) -> TokenBundle:
        self.refresh_calls += 1
        return TokenBundle(access_token=f"at-refreshed-{self.refresh_calls}", expires_in=3599, scope="openid")

    async def fetch_userinfo(self, access_token: str) -> str:
        return "user@acme.com"

    async def revoke(self, token: str) -> None:
        self.revoked.append(token)


async def _make_user(db_session) -> User:
    user = User(email="u@acme.com", name="U", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


async def test_create_connection_encrypts_refresh_token(db_session):
    user = await _make_user(db_session)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid email", refresh_token="rt-secret")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="user@acme.com"
    )
    assert conn.google_email == "user@acme.com"
    assert conn.refresh_token_encrypted != b"rt-secret"
    assert conn.status == "active"


async def test_get_valid_access_token_returns_cached_when_fresh(db_session):
    user = await _make_user(db_session)
    bundle = TokenBundle(access_token="at-cached", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="user@acme.com"
    )
    fake = FakeOAuthClient()
    token = await connection_service.get_valid_access_token(db_session, conn, fake)
    assert token == "at-cached"
    assert fake.refresh_calls == 0


async def test_get_valid_access_token_refreshes_when_expired(db_session):
    user = await _make_user(db_session)
    bundle = TokenBundle(access_token="at-old", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="user@acme.com"
    )
    conn.access_token_expiry = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db_session.commit()
    fake = FakeOAuthClient()
    token = await connection_service.get_valid_access_token(db_session, conn, fake)
    assert token == "at-refreshed-1"
    assert fake.refresh_calls == 1
    await db_session.refresh(conn)
    assert conn.access_token_cache == "at-refreshed-1"


async def test_delete_connection_revokes_and_removes(db_session):
    user = await _make_user(db_session)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="user@acme.com"
    )
    fake = FakeOAuthClient()
    await connection_service.delete_connection(db_session, conn, fake)
    assert fake.revoked  # revoke was called
    assert await connection_service.get_connection(db_session, user) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_connection_service.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_connection_service.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/connection_service.py tests/test_connection_service.py
git commit -m "feat: connection service with token refresh and encryption"
```

---

## Task 7: API routes (`app/api/routes/connections.py`)

**Files:**
- Modify: `app/api/deps.py` (add `get_oauth_client`)
- Create: `app/api/routes/connections.py`
- Modify: `app/main.py` (include router)
- Test: `tests/test_connections_api.py`

- [ ] **Step 1: Add the OAuth client dependency to `app/api/deps.py`**

Append to `app/api/deps.py`:

```python
from app.config import get_settings
from app.google.oauth_client import GoogleOAuthClient, OAuthClient


def get_oauth_client() -> OAuthClient:
    settings = get_settings()
    return GoogleOAuthClient(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        redirect_uri=settings.google_redirect_uri,
        scopes=settings.google_scopes,
    )
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_connections_api.py
import pytest
from cryptography.fernet import Fernet

from app.api.deps import get_oauth_client
from app.google.oauth_client import TokenBundle
from app.main import app
from app.security import create_oauth_state


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeOAuthClient:
    def build_authorization_url(self, state: str) -> str:
        return f"https://auth.example/consent?state={state}"

    async def exchange_code(self, code: str) -> TokenBundle:
        return TokenBundle(access_token="at", expires_in=3599, scope="openid email", refresh_token="rt")

    async def refresh(self, refresh_token: str) -> TokenBundle:
        return TokenBundle(access_token="at2", expires_in=3599, scope="openid email")

    async def fetch_userinfo(self, access_token: str) -> str:
        return "person@acme.com"

    async def revoke(self, token: str) -> None:
        return None


@pytest.fixture
def fake_oauth():
    fake = FakeOAuthClient()
    app.dependency_overrides[get_oauth_client] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_oauth_client, None)


async def _register(client) -> str:
    resp = await client.post(
        "/v1/auth/register",
        json={"email": "person@acme.com", "name": "P", "password": "password123"},
    )
    return resp.json()["access_token"]


async def test_start_returns_authorization_url(client, fake_oauth):
    token = await _register(client)
    resp = await client.get(
        "/v1/connections/google/start", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["authorization_url"].startswith("https://auth.example/consent?state=")


async def test_callback_creates_connection(client, fake_oauth):
    token = await _register(client)
    # find the user id from /me
    me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    user_id = me.json()["id"]
    state = create_oauth_state(user_id)

    resp = await client.get(f"/v1/connections/google/callback?code=abc&state={state}")
    assert resp.status_code == 200

    status = await client.get(
        "/v1/connections/google", headers={"Authorization": f"Bearer {token}"}
    )
    body = status.json()
    assert body["connected"] is True
    assert body["google_email"] == "person@acme.com"
    assert body["status"] == "active"


async def test_callback_rejects_bad_state(client, fake_oauth):
    resp = await client.get("/v1/connections/google/callback?code=abc&state=not-valid")
    assert resp.status_code == 400


async def test_status_when_not_connected(client, fake_oauth):
    token = await _register(client)
    resp = await client.get(
        "/v1/connections/google", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["connected"] is False


async def test_delete_connection(client, fake_oauth):
    token = await _register(client)
    me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    state = create_oauth_state(me.json()["id"])
    await client.get(f"/v1/connections/google/callback?code=abc&state={state}")

    resp = await client.delete(
        "/v1/connections/google", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 204

    status = await client.get(
        "/v1/connections/google", headers={"Authorization": f"Bearer {token}"}
    )
    assert status.json()["connected"] is False
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_connections_api.py -v`
Expected: FAIL (404 — routes not mounted)

- [ ] **Step 4: Write the router**

```python
# app/api/routes/connections.py
import uuid

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

    bundle = await oauth_client.exchange_code(code)
    if not bundle.refresh_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No refresh token returned; re-consent with prompt=consent required",
        )
    email = await oauth_client.fetch_userinfo(bundle.access_token)
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
```

- [ ] **Step 5: Wire the router in `app/main.py`**

Update `create_app` to include the connections router:

```python
# app/main.py
from fastapi import FastAPI

from app.api.routes import auth, connections, health


def create_app() -> FastAPI:
    application = FastAPI(title="Google Meet Notes")
    application.include_router(health.router)
    application.include_router(auth.router)
    application.include_router(connections.router)
    return application


app = create_app()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_connections_api.py -v`
Expected: PASS (5 tests)

- [ ] **Step 7: Commit**

```bash
git add app/api/deps.py app/api/routes/connections.py app/main.py tests/test_connections_api.py
git commit -m "feat: google connection endpoints (start/callback/status/disconnect)"
```

---

## Task 8: Full suite green + verification

**Files:** none (verification only)

- [ ] **Step 1: Run the whole suite**

Run: `.venv\Scripts\python.exe -m pytest -v`
Expected: ALL pass (Phase 1 tests + the new Phase 2 tests; ~30 total).

- [ ] **Step 2: Confirm migrations are current**

Run: `.venv\Scripts\python.exe -m alembic upgrade head`
Expected: "Running upgrade ..." or already at head; no errors. `oauth_connections` table present.

- [ ] **Step 3: Commit any final tidy-ups (only if needed)**

```bash
git add -A
git commit -m "chore: phase 2 oauth complete"
```

---

## Self-review (completed during planning)

- **Spec coverage (Phase 2 slice):** `oauth_connections` table ✓ (Task 4), OAuth connect start ✓ + callback storing encrypted refresh token ✓ (Tasks 5-7), connection status ✓ + disconnect/revoke ✓ (Task 7), encrypted-at-rest refresh tokens ✓ (Task 2/6), single-flight refresh + `needs_reconnect` on failure ✓ (Task 6), scope minimization (the 5 product scopes + openid/email) ✓ (Task 1). Deferred (by design, to their phases): creating the Workspace Events subscription on connect (Phase 4) and the Workspace-tier capability check (Phase 3). The `callback` returns JSON; a production deployment may later swap this for an HTML redirect to a frontend — out of scope here.
- **Placeholder scan:** none — every step has complete code and exact commands.
- **Type consistency:** `OAuthClient` Protocol methods (`build_authorization_url`, `exchange_code`, `refresh`, `fetch_userinfo`, `revoke`) match `GoogleOAuthClient` and both fakes; `TokenBundle` fields (`access_token`, `expires_in`, `scope`, `refresh_token`) are used consistently across client, service, and tests; `connection_service` function names (`get_connection`, `upsert_connection`, `get_valid_access_token`, `delete_connection`) match their call sites in routes and tests; `create_oauth_state`/`verify_oauth_state`/`InvalidStateError` match across `security.py`, routes, and tests; `get_oauth_client` dependency is overridden by name in the API tests.
- **Known caveats for the implementer:** (1) `get_settings` is `lru_cache`d, so tests that set `ENCRYPTION_KEY` must `get_settings.cache_clear()` (the fixtures do this). (2) The single-flight `_locks` dict is process-local — fine for the single-worker dev/test setup; a multi-worker deployment would need a distributed lock (note for a later hardening phase). (3) `pytest-asyncio` is configured session-scoped from Phase 1, so async tests here need no extra markers.
