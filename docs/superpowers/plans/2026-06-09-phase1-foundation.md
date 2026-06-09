# Phase 1: Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the FastAPI service skeleton with PostgreSQL, migrations, a `users` table, and app-level authentication (register/login + a reusable "current user" dependency).

**Architecture:** Modular monolith. FastAPI app with async SQLAlchemy 2.0 over PostgreSQL, Alembic migrations, JWT bearer auth, bcrypt password hashing. Code is split by responsibility: `config`, `db`, `security`, `models/`, `schemas/`, `services/`, `api/routes/`. Tests are TDD-first with pytest against a real Postgres test database.

**Tech Stack:** Python 3.12, FastAPI, Uvicorn, SQLAlchemy 2.0 (async + asyncpg), Alembic, Pydantic v2 / pydantic-settings, PyJWT, passlib[bcrypt], pytest, pytest-asyncio, httpx.

---

## File structure (created by this plan)

```
Google meet notes/
  pyproject.toml          # deps + tooling config
  .env.example            # documented env vars
  .gitignore
  README.md
  alembic.ini             # alembic config
  app/
    __init__.py
    main.py               # FastAPI app factory + router wiring
    config.py             # Settings (env-driven)
    db.py                 # engine, session factory, Base
    security.py           # password hashing + JWT encode/decode
    models/
      __init__.py
      user.py             # User ORM model
    schemas/
      __init__.py
      auth.py             # request/response Pydantic models
    services/
      __init__.py
      auth_service.py     # create_user / authenticate_user
    api/
      __init__.py
      deps.py             # get_db, get_current_user
      routes/
        __init__.py
        health.py         # GET /healthz
        auth.py           # POST /v1/auth/register, /v1/auth/login
  migrations/
    env.py                # alembic async env
    script.py.mako
    versions/             # migration files
  tests/
    __init__.py
    conftest.py           # async DB + client fixtures
    test_health.py
    test_security.py
    test_auth.py
```

**Prerequisite:** A local PostgreSQL instance is running, with two databases: `meetnotes` and `meetnotes_test`. If you have Docker, run:

```bash
docker run --name meetnotes-pg -e POSTGRES_PASSWORD=postgres -e POSTGRES_USER=postgres -p 5432:5432 -d postgres:16
docker exec -it meetnotes-pg psql -U postgres -c "CREATE DATABASE meetnotes;"
docker exec -it meetnotes-pg psql -U postgres -c "CREATE DATABASE meetnotes_test;"
```

---

## Task 1: Project scaffold & dependencies

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`
- Create: `README.md`
- Create: `app/__init__.py` (empty)

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "google-meet-notes"
version = "0.1.0"
description = "Connect Google Calendar + Meet + Gemini to auto-generate meeting notes"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "sqlalchemy[asyncio]>=2.0.36",
    "asyncpg>=0.30",
    "alembic>=1.14",
    "pydantic>=2.9",
    "pydantic-settings>=2.6",
    "pyjwt>=2.10",
    "passlib[bcrypt]>=1.7.4",
    "python-multipart>=0.0.12",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "httpx>=0.27",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["app*"]
```

- [ ] **Step 2: Create `.gitignore`**

```gitignore
.venv/
__pycache__/
*.pyc
.env
.pytest_cache/
*.egg-info/
.DS_Store
```

- [ ] **Step 3: Create `.env.example`**

```dotenv
# App
APP_ENV=local
JWT_SECRET=change-me-to-a-long-random-string
JWT_EXPIRE_MINUTES=1440

# Database
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/meetnotes
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/meetnotes_test
```

- [ ] **Step 4: Create `README.md`**

```markdown
# Google Meet Notes

API service that connects Google Calendar + Meet + Gemini to auto-generate meeting notes.

See `docs/superpowers/specs/2026-06-09-meet-gemini-notes-design.md` for the full design.

## Setup (Windows PowerShell)

\`\`\`powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env   # then edit values
\`\`\`

## Run

\`\`\`powershell
alembic upgrade head
uvicorn app.main:app --reload
\`\`\`

## Test

\`\`\`powershell
pytest -v
\`\`\`
```

- [ ] **Step 5: Create empty `app/__init__.py`**

```python
```

- [ ] **Step 6: Create virtualenv and install**

Run (PowerShell):
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```
Expected: installs succeed, `pip show fastapi` returns a version.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .gitignore .env.example README.md app/__init__.py
git commit -m "chore: project scaffold and dependencies"
```

---

## Task 2: Configuration (`app/config.py`)

**Files:**
- Create: `app/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py
from app.config import Settings


def test_settings_reads_values_from_env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    monkeypatch.setenv("JWT_EXPIRE_MINUTES", "60")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db_test")

    settings = Settings()

    assert settings.jwt_secret == "test-secret"
    assert settings.jwt_expire_minutes == 60
    assert settings.database_url.endswith("/db")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.config'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/config.py
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    jwt_secret: str = "change-me"
    jwt_expire_minutes: int = 1440
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/meetnotes"
    test_database_url: str = (
        "postgresql+asyncpg://postgres:postgres@localhost:5432/meetnotes_test"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/config.py tests/test_config.py
git commit -m "feat: env-driven settings"
```

---

## Task 3: Database layer (`app/db.py`)

**Files:**
- Create: `app/db.py`

This task has no standalone unit test (it's exercised by every later DB test). It defines the declarative `Base`, the async engine, and a session factory.

- [ ] **Step 1: Write implementation**

```python
# app/db.py
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
engine = create_async_engine(_settings.database_url, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
```

- [ ] **Step 2: Sanity import check**

Run: `python -c "import app.db; print('ok')"`
Expected: prints `ok`

- [ ] **Step 3: Commit**

```bash
git add app/db.py
git commit -m "feat: async sqlalchemy base, engine, session factory"
```

---

## Task 4: User model (`app/models/user.py`)

**Files:**
- Create: `app/models/__init__.py`
- Create: `app/models/user.py`

- [ ] **Step 1: Write implementation**

```python
# app/models/user.py
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

- [ ] **Step 2: Re-export from package `app/models/__init__.py`**

```python
# app/models/__init__.py
from app.models.user import User

__all__ = ["User"]
```

- [ ] **Step 3: Sanity import check**

Run: `python -c "from app.models import User; print(User.__tablename__)"`
Expected: prints `users`

- [ ] **Step 4: Commit**

```bash
git add app/models/__init__.py app/models/user.py
git commit -m "feat: User ORM model"
```

---

## Task 5: Alembic migrations

**Files:**
- Create: `alembic.ini`
- Create: `migrations/env.py`
- Create: `migrations/script.py.mako`
- Create: `migrations/versions/` (directory, add a `.gitkeep`)

- [ ] **Step 1: Create `alembic.ini`**

```ini
[alembic]
script_location = migrations
prepend_sys_path = .

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console
qualname =

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
datefmt = %H:%M:%S
```

- [ ] **Step 2: Create `migrations/script.py.mako`**

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""
from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
```

- [ ] **Step 3: Create `migrations/env.py` (async)**

```python
# migrations/env.py
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db import Base
from app import models  # noqa: F401  ensures models are imported for autogenerate

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(_url(), future=True)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
```

- [ ] **Step 4: Keep the versions directory tracked**

Create `migrations/versions/.gitkeep`:
```text
```

- [ ] **Step 5: Autogenerate the initial migration**

Run (PowerShell, venv active, `.env` configured, DB running):
```powershell
alembic revision --autogenerate -m "create users table"
```
Expected: a new file appears under `migrations/versions/` containing `op.create_table("users", ...)`.

- [ ] **Step 6: Apply the migration**

Run: `alembic upgrade head`
Expected: completes without error; `users` table exists in the `meetnotes` DB.

- [ ] **Step 7: Commit**

```bash
git add alembic.ini migrations
git commit -m "feat: alembic setup and initial users migration"
```

---

## Task 6: Security helpers (`app/security.py`)

**Files:**
- Create: `app/security.py`
- Test: `tests/test_security.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_security.py
import time

import pytest

from app.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_password_hash_roundtrip():
    hashed = hash_password("s3cret")
    assert hashed != "s3cret"
    assert verify_password("s3cret", hashed) is True
    assert verify_password("wrong", hashed) is False


def test_jwt_roundtrip():
    token = create_access_token(subject="user-123")
    payload = decode_access_token(token)
    assert payload["sub"] == "user-123"


def test_jwt_rejects_tampered_token():
    token = create_access_token(subject="user-123")
    with pytest.raises(Exception):
        decode_access_token(token + "tampered")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_security.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.security'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/security.py
from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext

from app.config import get_settings

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
_ALGORITHM = "HS256"


def hash_password(plain: str) -> str:
    return _pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd.verify(plain, hashed)


def create_access_token(subject: str) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": subject, "exp": expire}
    return jwt.encode(payload, settings.jwt_secret, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> dict:
    settings = get_settings()
    return jwt.decode(token, settings.jwt_secret, algorithms=[_ALGORITHM])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_security.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/security.py tests/test_security.py
git commit -m "feat: password hashing and JWT helpers"
```

---

## Task 7: Auth schemas (`app/schemas/auth.py`)

**Files:**
- Create: `app/schemas/__init__.py` (empty)
- Create: `app/schemas/auth.py`

- [ ] **Step 1: Write implementation**

```python
# app/schemas/auth.py
import uuid

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: uuid.UUID
    email: EmailStr
    name: str

    model_config = {"from_attributes": True}
```

- [ ] **Step 2: Create empty `app/schemas/__init__.py`**

```python
```

- [ ] **Step 3: Add `email-validator` dependency (required by `EmailStr`)**

Edit `pyproject.toml` `dependencies` list — add the line `"email-validator>=2.2",` after the `pydantic-settings` entry, then reinstall:

Run: `pip install -e ".[dev]"`
Expected: `email-validator` installed.

- [ ] **Step 4: Sanity import check**

Run: `python -c "from app.schemas.auth import RegisterRequest; print('ok')"`
Expected: prints `ok`

- [ ] **Step 5: Commit**

```bash
git add app/schemas/__init__.py app/schemas/auth.py pyproject.toml
git commit -m "feat: auth request/response schemas"
```

---

## Task 8: Auth service (`app/services/auth_service.py`)

**Files:**
- Create: `app/services/__init__.py` (empty)
- Create: `app/services/auth_service.py`
- Test: `tests/test_auth_service.py`

Defines the errors and the two core operations. Uses the `db_session` fixture introduced in Task 9's conftest — so this task's test is written now but **run after Task 9** (note in Step 2).

- [ ] **Step 1: Write implementation**

```python
# app/services/auth_service.py
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User
from app.security import hash_password, verify_password


class EmailAlreadyExistsError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


async def create_user(session: AsyncSession, *, email: str, name: str, password: str) -> User:
    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise EmailAlreadyExistsError(email)

    user = User(email=email, name=name, hashed_password=hash_password(password))
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def authenticate_user(session: AsyncSession, *, email: str, password: str) -> User:
    user = await session.scalar(select(User).where(User.email == email))
    if user is None or not verify_password(password, user.hashed_password):
        raise InvalidCredentialsError(email)
    return user
```

- [ ] **Step 2: Create empty `app/services/__init__.py`**

```python
```

- [ ] **Step 3: Write the test (run it in Task 9 after conftest exists)**

```python
# tests/test_auth_service.py
import pytest

from app.services.auth_service import (
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    authenticate_user,
    create_user,
)


async def test_create_user_persists_and_hashes(db_session):
    user = await create_user(
        db_session, email="a@acme.com", name="Alice", password="password123"
    )
    assert user.id is not None
    assert user.hashed_password != "password123"


async def test_create_user_rejects_duplicate_email(db_session):
    await create_user(db_session, email="dup@acme.com", name="A", password="password123")
    with pytest.raises(EmailAlreadyExistsError):
        await create_user(db_session, email="dup@acme.com", name="B", password="password123")


async def test_authenticate_user_success(db_session):
    await create_user(db_session, email="b@acme.com", name="Bob", password="password123")
    user = await authenticate_user(db_session, email="b@acme.com", password="password123")
    assert user.email == "b@acme.com"


async def test_authenticate_user_wrong_password(db_session):
    await create_user(db_session, email="c@acme.com", name="C", password="password123")
    with pytest.raises(InvalidCredentialsError):
        await authenticate_user(db_session, email="c@acme.com", password="nope")
```

- [ ] **Step 4: Commit**

```bash
git add app/services/__init__.py app/services/auth_service.py tests/test_auth_service.py
git commit -m "feat: auth service (create/authenticate user)"
```

---

## Task 9: Test harness (`tests/conftest.py`) + run service tests

**Files:**
- Create: `tests/__init__.py` (empty)
- Create: `tests/conftest.py`

Provides: a session-scoped engine on the **test** DB, table create/drop, a per-test `db_session`, and an httpx `client` whose `get_session` dependency is overridden to use the test session.

- [ ] **Step 1: Write `tests/conftest.py`**

```python
# tests/conftest.py
import asyncio
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db import Base, get_session
from app.main import app


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def _engine():
    engine = create_async_engine(get_settings().test_database_url, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(_engine) -> AsyncGenerator:
    sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    async with sessionmaker() as session:
        yield session
        # clean tables between tests for isolation
        for table in reversed(Base.metadata.sorted_tables):
            await session.execute(table.delete())
        await session.commit()


@pytest_asyncio.fixture
async def client(db_session) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
```

- [ ] **Step 2: Create empty `tests/__init__.py`**

```python
```

- [ ] **Step 3: Run the auth service tests from Task 8**

Run: `pytest tests/test_auth_service.py -v`
Expected: PASS (4 tests). (`app.main:app` must import cleanly — it's created in Task 10; if running strictly in order, do Task 10 first then return here. See note below.)

> **Ordering note:** `conftest.py` imports `app.main.app`, which is created in Task 10. If you execute strictly top-to-bottom, swap the order: do Task 10's Steps 1–3 (create `app/main.py` + health route) before running this step. The plan keeps them adjacent for this reason.

- [ ] **Step 4: Commit**

```bash
git add tests/__init__.py tests/conftest.py
git commit -m "test: async db + client fixtures"
```

---

## Task 10: App factory + health route

**Files:**
- Create: `app/api/__init__.py` (empty)
- Create: `app/api/routes/__init__.py` (empty)
- Create: `app/api/routes/health.py`
- Create: `app/main.py`
- Test: `tests/test_health.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_health.py
async def test_healthz_ok(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 2: Write the health route**

```python
# app/api/routes/health.py
from fastapi import APIRouter

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
```

- [ ] **Step 3: Write the app factory `app/main.py`**

```python
# app/main.py
from fastapi import FastAPI

from app.api.routes import health


def create_app() -> FastAPI:
    application = FastAPI(title="Google Meet Notes")
    application.include_router(health.router)
    return application


app = create_app()
```

- [ ] **Step 4: Create empty `app/api/__init__.py` and `app/api/routes/__init__.py`**

```python
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_health.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add app/api app/main.py tests/test_health.py
git commit -m "feat: app factory and health endpoint"
```

---

## Task 11: `get_current_user` dependency (`app/api/deps.py`)

**Files:**
- Create: `app/api/deps.py`

- [ ] **Step 1: Write implementation**

```python
# app/api/deps.py
import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import User
from app.security import decode_access_token

_bearer = HTTPBearer(auto_error=True)


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
        user_id = payload["sub"]
    except (jwt.PyJWTError, KeyError):
        raise creds_exc

    user = await session.get(User, uuid.UUID(user_id))
    if user is None:
        raise creds_exc
    return user
```

- [ ] **Step 2: Sanity import check**

Run: `python -c "from app.api.deps import get_current_user; print('ok')"`
Expected: prints `ok`

- [ ] **Step 3: Commit**

```bash
git add app/api/deps.py
git commit -m "feat: get_current_user auth dependency"
```

---

## Task 12: Register & login routes (`app/api/routes/auth.py`)

**Files:**
- Create: `app/api/routes/auth.py`
- Modify: `app/main.py` (wire the auth router)
- Test: `tests/test_auth.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_auth.py
async def test_register_returns_token(client):
    resp = await client.post(
        "/v1/auth/register",
        json={"email": "new@acme.com", "name": "New", "password": "password123"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


async def test_register_duplicate_email_conflicts(client):
    payload = {"email": "dup@acme.com", "name": "Dup", "password": "password123"}
    first = await client.post("/v1/auth/register", json=payload)
    assert first.status_code == 201
    second = await client.post("/v1/auth/register", json=payload)
    assert second.status_code == 409


async def test_login_success(client):
    await client.post(
        "/v1/auth/register",
        json={"email": "log@acme.com", "name": "Log", "password": "password123"},
    )
    resp = await client.post(
        "/v1/auth/login", json={"email": "log@acme.com", "password": "password123"}
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]


async def test_login_wrong_password_unauthorized(client):
    await client.post(
        "/v1/auth/register",
        json={"email": "w@acme.com", "name": "W", "password": "password123"},
    )
    resp = await client.post(
        "/v1/auth/login", json={"email": "w@acme.com", "password": "wrong"}
    )
    assert resp.status_code == 401


async def test_me_requires_auth_and_returns_user(client):
    reg = await client.post(
        "/v1/auth/register",
        json={"email": "me@acme.com", "name": "Me", "password": "password123"},
    )
    token = reg.json()["access_token"]
    resp = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "me@acme.com"

    unauth = await client.get("/v1/auth/me")
    assert unauth.status_code in (401, 403)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_auth.py -v`
Expected: FAIL (404s — routes not mounted yet)

- [ ] **Step 3: Write the auth router**

```python
# app/api/routes/auth.py
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db import get_session
from app.models import User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from app.security import create_access_token
from app.services.auth_service import (
    EmailAlreadyExistsError,
    InvalidCredentialsError,
    authenticate_user,
    create_user,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest, session: AsyncSession = Depends(get_session)
) -> TokenResponse:
    try:
        user = await create_user(
            session, email=body.email, name=body.name, password=body.password
        )
    except EmailAlreadyExistsError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        )
    return TokenResponse(access_token=create_access_token(subject=str(user.id)))


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, session: AsyncSession = Depends(get_session)
) -> TokenResponse:
    try:
        user = await authenticate_user(session, email=body.email, password=body.password)
    except InvalidCredentialsError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials"
        )
    return TokenResponse(access_token=create_access_token(subject=str(user.id)))


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)) -> User:
    return current_user
```

- [ ] **Step 4: Wire the router in `app/main.py`**

Replace the body of `create_app` so it reads:

```python
# app/main.py
from fastapi import FastAPI

from app.api.routes import auth, health


def create_app() -> FastAPI:
    application = FastAPI(title="Google Meet Notes")
    application.include_router(health.router)
    application.include_router(auth.router)
    return application


app = create_app()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_auth.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add app/api/routes/auth.py app/main.py tests/test_auth.py
git commit -m "feat: register, login, and me endpoints"
```

---

## Task 13: Full suite green + manual smoke

**Files:** none (verification only)

- [ ] **Step 1: Run the whole test suite**

Run: `pytest -v`
Expected: ALL pass (config, security, auth_service, health, auth).

- [ ] **Step 2: Manual smoke test the running server**

Run (terminal A): `uvicorn app.main:app --reload`
Run (terminal B, PowerShell):
```powershell
curl http://127.0.0.1:8000/healthz
curl -Method POST http://127.0.0.1:8000/v1/auth/register -ContentType "application/json" -Body '{"email":"smoke@acme.com","name":"Smoke","password":"password123"}'
```
Expected: health returns `{"status":"ok"}`; register returns a JSON body with `access_token`.

- [ ] **Step 3: Commit any final tidy-ups (if needed)**

```bash
git add -A
git commit -m "chore: phase 1 foundation complete"
```

---

## Self-review (completed during planning)

- **Spec coverage (Phase 1 slice):** FastAPI app ✓ (Task 10), PostgreSQL + async SQLAlchemy ✓ (Task 3), migrations ✓ (Task 5), `users` table ✓ (Task 4), app auth register/login ✓ (Task 12), reusable current-user dependency ✓ (Task 11). Later-phase tables (oauth_connections, meetings, etc.) are intentionally deferred to their own phases (created with their feature).
- **Placeholder scan:** none — every code/test step contains complete code and exact commands.
- **Type consistency:** `get_session` (db.py) is the dependency overridden in tests and used by routes/deps; `create_access_token(subject=...)` / `decode_access_token` signatures match across `security.py`, `deps.py`, and `auth.py`; `User` fields used in service/schemas match the model.
- **Known ordering caveat:** `conftest.py` imports `app.main.app`; Task 9 documents running Task 10 first if executing strictly in order.
