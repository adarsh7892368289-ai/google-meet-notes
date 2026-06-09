# Phase 3: Meeting Creation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a connected user create a meeting through our API — which creates a Google Calendar event with a Google Meet link and (when notes are requested and the account supports it) turns on Meet auto-transcript — then list/get/delete those meetings.

**Architecture:** Builds on Phases 1-2. Two new injectable Google clients (`app/google/calendar_client.py`, `app/google/meet_client.py`) wrap the Calendar and Meet REST APIs behind Protocols so the meeting service and routes are testable with fakes. A `meeting_service` orchestrates: get a valid access token (Phase 2), create the Calendar event, optionally enable auto-transcript, and persist a `meetings` row. All Google network access stays funneled through `app/google/`.

**Tech Stack:** Same as Phase 2 (FastAPI, async SQLAlchemy, Alembic, httpx, cryptography, PyJWT, bcrypt, pytest/pytest-asyncio). No new dependencies.

**Key API facts (verified):**
- Create Meet link: `POST https://www.googleapis.com/calendar/v3/calendars/primary/events?conferenceDataVersion=1` with body containing `conferenceData.createRequest.conferenceSolutionKey.type = "hangoutsMeet"` and a unique `requestId`. The response `conferenceData.entryPoints[]` (entryPointType `video`) holds the Meet URI (e.g. `https://meet.google.com/abc-defg-hij`); the meeting code is the last path segment.
- Enable auto-transcript: resolve the space with `GET https://meet.googleapis.com/v2/spaces/{meetingCode}` (meeting code alias is allowed; response `name` is the canonical `spaces/{id}`), then `PATCH https://meet.googleapis.com/v2/{spaceName}?updateMask=config.artifactConfig` with `{"config":{"artifactConfig":{"transcriptionConfig":{"autoTranscriptionGeneration":"ON"}}}}`. Requires the `meetings.space.settings` scope (already in our consent set).

**Capability handling (deliberate refinement of the spec's 422):** If notes are requested but enabling auto-transcript fails (account plan lacks transcription, or permission error), we do NOT fail the request and orphan the Calendar event. Instead we persist the meeting with `notes_enabled=false` and return a `warning` in the response. The meeting is still created.

**Prerequisites in place:** Phase 2 `connection_service.get_valid_access_token`, `get_connection`, `TokenRefreshError`; `app/api/deps.py` (`get_current_user`, `get_oauth_client`); the `oauth_connections` table; conftest fixtures; Postgres running.

---

## File structure (created/modified)

```
app/
  models/
    __init__.py          # MODIFY: export Meeting
    meeting.py           # CREATE: Meeting ORM model
  schemas/
    meeting.py           # CREATE: MeetingCreate, NotesConfig, MeetingResponse
  google/
    calendar_client.py   # CREATE: CreatedEvent, CalendarClient protocol, GoogleCalendarClient
    meet_client.py       # CREATE: MeetClient protocol, GoogleMeetClient
  services/
    meeting_service.py   # CREATE: create/list/get/delete orchestration
  api/
    deps.py              # MODIFY: add get_calendar_client, get_meet_client
    routes/
      meetings.py        # CREATE: POST/GET/GET{id}/DELETE
  main.py                # MODIFY: include meetings router
migrations/versions/
  <auto>_meetings.py     # CREATE via alembic autogenerate
tests/
  test_calendar_client.py     # CREATE (httpx MockTransport)
  test_meet_client.py         # CREATE (httpx MockTransport)
  test_meeting_service.py     # CREATE (fakes + db)
  test_meetings_api.py        # CREATE (fakes via dependency override)
```

**Environment:** Windows PowerShell (chain with `;`). venv: `.venv\Scripts\python.exe -m ...`. Postgres on localhost:5432 (postgres/postgres; `meetnotes`, `meetnotes_test`). `psql` at `C:\Program Files\PostgreSQL\17\bin\psql.exe` (PGPASSWORD=postgres). pytest-asyncio is session-scoped (no per-test markers).

---

## Task 1: Meeting model, schemas, migration

**Files:**
- Create: `app/models/meeting.py`
- Modify: `app/models/__init__.py`
- Create: `app/schemas/meeting.py`
- Migration via autogenerate

- [ ] **Step 1: Write the model**

```python
# app/models/meeting.py
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attendees: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    calendar_event_id: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    meet_join_uri: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    meeting_code: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    notes_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduled")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
```

- [ ] **Step 2: Export it**

```python
# app/models/__init__.py
from app.models.meeting import Meeting
from app.models.oauth_connection import OAuthConnection
from app.models.user import User

__all__ = ["User", "OAuthConnection", "Meeting"]
```

- [ ] **Step 3: Write the schemas**

```python
# app/schemas/meeting.py
import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, model_validator


class NotesConfig(BaseModel):
    language: str = "en"
    style: str = "detailed"  # detailed | concise | action_items_only
    extra_recipients: list[EmailStr] = Field(default_factory=list)


class MeetingCreate(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    description: str | None = None
    start_time: datetime
    end_time: datetime
    attendees: list[EmailStr] = Field(default_factory=list)
    notes_enabled: bool = False
    notes_config: NotesConfig = Field(default_factory=NotesConfig)

    @model_validator(mode="after")
    def _check_times(self) -> "MeetingCreate":
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class MeetingResponse(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    start_time: datetime
    end_time: datetime
    attendees: list[str]
    meet_join_uri: str | None
    calendar_event_id: str | None
    meeting_code: str | None
    notes_enabled: bool
    status: str
    warning: str | None = None

    model_config = {"from_attributes": True}
```

- [ ] **Step 4: Sanity import**

Run: `.venv\Scripts\python.exe -c "from app.models import Meeting; from app.schemas.meeting import MeetingCreate; print(Meeting.__tablename__)"`
Expected: prints `meetings`

- [ ] **Step 5: Autogenerate + apply migration**

Run: `.venv\Scripts\python.exe -m alembic revision --autogenerate -m "create meetings table"`
Then: `.venv\Scripts\python.exe -m alembic upgrade head`
Expected: migration has `op.create_table("meetings", ...)`; table exists (verify with psql `\d meetings`).

- [ ] **Step 6: Commit**

```bash
git add app/models/meeting.py app/models/__init__.py app/schemas/meeting.py migrations/versions
git commit -m "feat: meeting model, schemas, and migration"
```

---

## Task 2: Calendar client (`app/google/calendar_client.py`)

**Files:**
- Create: `app/google/calendar_client.py`
- Test: `tests/test_calendar_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_calendar_client.py
from datetime import datetime, timezone

import httpx

from app.google.calendar_client import CreatedEvent, GoogleCalendarClient


def _client(handler) -> GoogleCalendarClient:
    return GoogleCalendarClient(transport=httpx.MockTransport(handler))


async def test_create_event_parses_meet_link_and_code():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/calendars/primary/events")
        assert request.url.params.get("conferenceDataVersion") == "1"
        assert request.headers["Authorization"] == "Bearer at-1"
        body = request.content.decode()
        assert "hangoutsMeet" in body
        return httpx.Response(
            200,
            json={
                "id": "evt-123",
                "conferenceData": {
                    "conferenceId": "abc-defg-hij",
                    "status": {"statusCode": "success"},
                    "entryPoints": [
                        {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"},
                        {"entryPointType": "phone", "uri": "tel:+1-111"},
                    ],
                },
            },
        )

    client = _client(handler)
    result = await client.create_event(
        "at-1",
        summary="Sync",
        description="desc",
        start=datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc),
        end=datetime(2026, 6, 12, 11, 0, tzinfo=timezone.utc),
        attendees=["a@acme.com", "b@acme.com"],
    )
    assert isinstance(result, CreatedEvent)
    assert result.event_id == "evt-123"
    assert result.meet_uri == "https://meet.google.com/abc-defg-hij"
    assert result.meeting_code == "abc-defg-hij"


async def test_create_event_handles_missing_conference():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "evt-x"})

    result = await _client(handler).create_event(
        "at-1",
        summary="S",
        description=None,
        start=datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc),
        end=datetime(2026, 6, 12, 11, 0, tzinfo=timezone.utc),
        attendees=[],
    )
    assert result.event_id == "evt-x"
    assert result.meet_uri is None
    assert result.meeting_code is None


async def test_delete_event_calls_delete():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(204)

    await _client(handler).delete_event("at-1", "evt-123")
    assert seen["method"] == "DELETE"
    assert seen["path"].endswith("/calendars/primary/events/evt-123")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_calendar_client.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write implementation**

```python
# app/google/calendar_client.py
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import httpx

CALENDAR_EVENTS_URI = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


@dataclass
class CreatedEvent:
    event_id: str
    meet_uri: str | None
    meeting_code: str | None
    conference_status: str


class CalendarClient(Protocol):
    async def create_event(
        self,
        access_token: str,
        *,
        summary: str,
        description: str | None,
        start: datetime,
        end: datetime,
        attendees: list[str],
    ) -> CreatedEvent: ...

    async def delete_event(self, access_token: str, event_id: str) -> None: ...


class GoogleCalendarClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def _http(self, access_token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=30.0,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def create_event(
        self,
        access_token: str,
        *,
        summary: str,
        description: str | None,
        start: datetime,
        end: datetime,
        attendees: list[str],
    ) -> CreatedEvent:
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "attendees": [{"email": e} for e in attendees],
            "conferenceData": {
                "createRequest": {
                    "requestId": str(uuid.uuid4()),
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
        }
        async with self._http(access_token) as http:
            resp = await http.post(
                CALENDAR_EVENTS_URI, params={"conferenceDataVersion": "1"}, json=body
            )
            resp.raise_for_status()
            data = resp.json()
        return _parse_event(data)

    async def delete_event(self, access_token: str, event_id: str) -> None:
        async with self._http(access_token) as http:
            resp = await http.delete(f"{CALENDAR_EVENTS_URI}/{event_id}")
            if resp.status_code not in (200, 204, 404, 410):
                resp.raise_for_status()


def _parse_event(data: dict) -> CreatedEvent:
    conf = data.get("conferenceData") or {}
    status = (conf.get("status") or {}).get("statusCode", "none")
    meet_uri = None
    for ep in conf.get("entryPoints", []):
        if ep.get("entryPointType") == "video":
            meet_uri = ep.get("uri")
            break
    meeting_code = meet_uri.rstrip("/").split("/")[-1] if meet_uri else None
    return CreatedEvent(
        event_id=data["id"],
        meet_uri=meet_uri,
        meeting_code=meeting_code,
        conference_status=status,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_calendar_client.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/google/calendar_client.py tests/test_calendar_client.py
git commit -m "feat: google calendar client (create/delete event with meet link)"
```

---

## Task 3: Meet client (`app/google/meet_client.py`)

**Files:**
- Create: `app/google/meet_client.py`
- Test: `tests/test_meet_client.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_meet_client.py
import httpx

from app.google.meet_client import GoogleMeetClient


def _client(handler) -> GoogleMeetClient:
    return GoogleMeetClient(transport=httpx.MockTransport(handler))


async def test_get_space_name_resolves_canonical_name():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/spaces/abc-defg-hij"
        assert request.headers["Authorization"] == "Bearer at-1"
        return httpx.Response(200, json={"name": "spaces/SERVERID", "meetingCode": "abc-defg-hij"})

    name = await _client(handler).get_space_name("at-1", "abc-defg-hij")
    assert name == "spaces/SERVERID"


async def test_enable_auto_transcript_patches_artifact_config():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["mask"] = request.url.params.get("updateMask")
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"name": "spaces/SERVERID"})

    await _client(handler).enable_auto_transcript("at-1", "spaces/SERVERID")
    assert captured["method"] == "PATCH"
    assert captured["path"] == "/v2/spaces/SERVERID"
    assert captured["mask"] == "config.artifactConfig"
    assert "autoTranscriptionGeneration" in captured["body"]
    assert "ON" in captured["body"]


async def test_enable_auto_transcript_raises_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "not supported"}})

    import pytest

    with pytest.raises(httpx.HTTPStatusError):
        await _client(handler).enable_auto_transcript("at-1", "spaces/SERVERID")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_meet_client.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write implementation**

```python
# app/google/meet_client.py
from __future__ import annotations

from typing import Protocol

import httpx

MEET_BASE = "https://meet.googleapis.com/v2"


class MeetClient(Protocol):
    async def get_space_name(self, access_token: str, meeting_code: str) -> str: ...
    async def enable_auto_transcript(self, access_token: str, space_name: str) -> None: ...


class GoogleMeetClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def _http(self, access_token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=30.0,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def get_space_name(self, access_token: str, meeting_code: str) -> str:
        async with self._http(access_token) as http:
            resp = await http.get(f"{MEET_BASE}/spaces/{meeting_code}")
            resp.raise_for_status()
            return resp.json()["name"]

    async def enable_auto_transcript(self, access_token: str, space_name: str) -> None:
        body = {
            "config": {
                "artifactConfig": {
                    "transcriptionConfig": {"autoTranscriptionGeneration": "ON"}
                }
            }
        }
        async with self._http(access_token) as http:
            resp = await http.patch(
                f"{MEET_BASE}/{space_name}",
                params={"updateMask": "config.artifactConfig"},
                json=body,
            )
            resp.raise_for_status()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_meet_client.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/google/meet_client.py tests/test_meet_client.py
git commit -m "feat: google meet client (resolve space, enable auto-transcript)"
```

---

## Task 4: Meeting service (`app/services/meeting_service.py`)

**Files:**
- Create: `app/services/meeting_service.py`
- Test: `tests/test_meeting_service.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_meeting_service.py
from datetime import datetime, timezone

import httpx
import pytest
from cryptography.fernet import Fernet

from app.google.calendar_client import CreatedEvent
from app.google.oauth_client import TokenBundle
from app.models import User
from app.schemas.meeting import MeetingCreate
from app.services import connection_service, meeting_service


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeOAuthClient:
    async def refresh(self, refresh_token: str) -> TokenBundle:
        return TokenBundle(access_token="at", expires_in=3599, scope="openid")


class FakeCalendarClient:
    def __init__(self):
        self.deleted = []

    async def create_event(self, access_token, *, summary, description, start, end, attendees):
        return CreatedEvent(
            event_id="evt-1",
            meet_uri="https://meet.google.com/abc-defg-hij",
            meeting_code="abc-defg-hij",
            conference_status="success",
        )

    async def delete_event(self, access_token, event_id):
        self.deleted.append(event_id)


class FakeMeetClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.enabled = []

    async def get_space_name(self, access_token, meeting_code):
        return "spaces/SERVERID"

    async def enable_auto_transcript(self, access_token, space_name):
        if self.fail:
            raise httpx.HTTPStatusError(
                "no", request=httpx.Request("PATCH", "https://x"),
                response=httpx.Response(403, request=httpx.Request("PATCH", "https://x")),
            )
        self.enabled.append(space_name)


async def _user_with_connection(db_session) -> User:
    user = User(email="u@acme.com", name="U", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="u@acme.com"
    )
    return user


def _payload(notes_enabled=False) -> MeetingCreate:
    return MeetingCreate(
        title="Roadmap",
        description="d",
        start_time=datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 6, 12, 11, 0, tzinfo=timezone.utc),
        attendees=["a@acme.com"],
        notes_enabled=notes_enabled,
    )


async def test_create_meeting_without_notes(db_session):
    user = await _user_with_connection(db_session)
    meet = FakeMeetClient()
    meeting, warning = await meeting_service.create_meeting(
        db_session, user=user, payload=_payload(notes_enabled=False),
        oauth_client=FakeOAuthClient(), calendar_client=FakeCalendarClient(), meet_client=meet,
    )
    assert meeting.calendar_event_id == "evt-1"
    assert meeting.meet_join_uri == "https://meet.google.com/abc-defg-hij"
    assert meeting.notes_enabled is False
    assert meet.enabled == []
    assert warning is None


async def test_create_meeting_with_notes_enables_transcript(db_session):
    user = await _user_with_connection(db_session)
    meet = FakeMeetClient()
    meeting, warning = await meeting_service.create_meeting(
        db_session, user=user, payload=_payload(notes_enabled=True),
        oauth_client=FakeOAuthClient(), calendar_client=FakeCalendarClient(), meet_client=meet,
    )
    assert meeting.notes_enabled is True
    assert meet.enabled == ["spaces/SERVERID"]
    assert warning is None


async def test_create_meeting_notes_capability_failure_downgrades(db_session):
    user = await _user_with_connection(db_session)
    meet = FakeMeetClient(fail=True)
    meeting, warning = await meeting_service.create_meeting(
        db_session, user=user, payload=_payload(notes_enabled=True),
        oauth_client=FakeOAuthClient(), calendar_client=FakeCalendarClient(), meet_client=meet,
    )
    assert meeting.notes_enabled is False
    assert warning is not None
    assert meeting.calendar_event_id == "evt-1"  # meeting still created


async def test_create_meeting_without_connection_raises(db_session):
    user = User(email="solo@acme.com", name="S", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    with pytest.raises(meeting_service.NotConnectedError):
        await meeting_service.create_meeting(
            db_session, user=user, payload=_payload(),
            oauth_client=FakeOAuthClient(), calendar_client=FakeCalendarClient(),
            meet_client=FakeMeetClient(),
        )


async def test_list_and_get_meetings(db_session):
    user = await _user_with_connection(db_session)
    await meeting_service.create_meeting(
        db_session, user=user, payload=_payload(),
        oauth_client=FakeOAuthClient(), calendar_client=FakeCalendarClient(), meet_client=FakeMeetClient(),
    )
    meetings = await meeting_service.list_meetings(db_session, user)
    assert len(meetings) == 1
    fetched = await meeting_service.get_meeting(db_session, user, meetings[0].id)
    assert fetched is not None
    assert fetched.id == meetings[0].id


async def test_delete_meeting_removes_and_deletes_event(db_session):
    user = await _user_with_connection(db_session)
    cal = FakeCalendarClient()
    meeting, _ = await meeting_service.create_meeting(
        db_session, user=user, payload=_payload(),
        oauth_client=FakeOAuthClient(), calendar_client=cal, meet_client=FakeMeetClient(),
    )
    ok = await meeting_service.delete_meeting(
        db_session, user=user, meeting_id=meeting.id,
        oauth_client=FakeOAuthClient(), calendar_client=cal,
    )
    assert ok is True
    assert "evt-1" in cal.deleted
    assert await meeting_service.get_meeting(db_session, user, meeting.id) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m pytest tests/test_meeting_service.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write implementation**

```python
# app/services/meeting_service.py
import logging
import uuid

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
            except Exception:
                logger.warning(
                    "could not enable auto-transcript for meeting code %s (user %s)",
                    created.meeting_code,
                    user.id,
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
            logger.warning("failed to delete calendar event for meeting %s", meeting_id)
    await session.delete(meeting)
    await session.commit()
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv\Scripts\python.exe -m pytest tests/test_meeting_service.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/meeting_service.py tests/test_meeting_service.py
git commit -m "feat: meeting service (create/list/get/delete orchestration)"
```

---

## Task 5: API routes (`app/api/routes/meetings.py`)

**Files:**
- Modify: `app/api/deps.py` (add `get_calendar_client`, `get_meet_client`)
- Create: `app/api/routes/meetings.py`
- Modify: `app/main.py`
- Test: `tests/test_meetings_api.py`

- [ ] **Step 1: Add client dependencies to `app/api/deps.py`**

Append to `app/api/deps.py`:

```python
from app.google.calendar_client import CalendarClient, GoogleCalendarClient
from app.google.meet_client import GoogleMeetClient, MeetClient


def get_calendar_client() -> CalendarClient:
    return GoogleCalendarClient()


def get_meet_client() -> MeetClient:
    return GoogleMeetClient()
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_meetings_api.py
import pytest
from cryptography.fernet import Fernet

from app.api.deps import get_calendar_client, get_meet_client, get_oauth_client
from app.google.calendar_client import CreatedEvent
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
    def build_authorization_url(self, state): return f"https://auth?state={state}"
    async def exchange_code(self, code): return TokenBundle("at", 3599, "openid email", "rt")
    async def refresh(self, refresh_token): return TokenBundle("at2", 3599, "openid email")
    async def fetch_userinfo(self, access_token): return "person@acme.com"
    async def revoke(self, token): return None


class FakeCalendarClient:
    def __init__(self): self.deleted = []
    async def create_event(self, access_token, *, summary, description, start, end, attendees):
        return CreatedEvent("evt-1", "https://meet.google.com/abc-defg-hij", "abc-defg-hij", "success")
    async def delete_event(self, access_token, event_id): self.deleted.append(event_id)


class FakeMeetClient:
    def __init__(self, fail=False): self.fail = fail
    async def get_space_name(self, access_token, meeting_code): return "spaces/SID"
    async def enable_auto_transcript(self, access_token, space_name):
        if self.fail:
            import httpx
            raise httpx.HTTPStatusError("x", request=httpx.Request("PATCH", "https://x"),
                                        response=httpx.Response(403, request=httpx.Request("PATCH", "https://x")))


@pytest.fixture
def fakes():
    cal, meet, oauth = FakeCalendarClient(), FakeMeetClient(), FakeOAuthClient()
    app.dependency_overrides[get_calendar_client] = lambda: cal
    app.dependency_overrides[get_meet_client] = lambda: meet
    app.dependency_overrides[get_oauth_client] = lambda: oauth
    yield {"cal": cal, "meet": meet, "oauth": oauth}
    for dep in (get_calendar_client, get_meet_client, get_oauth_client):
        app.dependency_overrides.pop(dep, None)


async def _register_and_connect(client) -> str:
    reg = await client.post(
        "/v1/auth/register",
        json={"email": "person@acme.com", "name": "P", "password": "password123"},
    )
    token = reg.json()["access_token"]
    me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    state = create_oauth_state(me.json()["id"])
    await client.get(f"/v1/connections/google/callback?code=abc&state={state}")
    return token


def _body(notes=False):
    return {
        "title": "Q3 Roadmap",
        "description": "plan",
        "start_time": "2026-06-12T10:00:00+00:00",
        "end_time": "2026-06-12T11:00:00+00:00",
        "attendees": ["a@acme.com"],
        "notes_enabled": notes,
    }


async def test_create_meeting_requires_connection(client, fakes):
    reg = await client.post(
        "/v1/auth/register",
        json={"email": "noconn@acme.com", "name": "N", "password": "password123"},
    )
    token = reg.json()["access_token"]
    resp = await client.post("/v1/meetings", json=_body(), headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 409


async def test_create_meeting_success(client, fakes):
    token = await _register_and_connect(client)
    resp = await client.post(
        "/v1/meetings", json=_body(notes=True), headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["meet_join_uri"] == "https://meet.google.com/abc-defg-hij"
    assert body["notes_enabled"] is True
    assert body["warning"] is None


async def test_create_meeting_notes_capability_downgrade(client, fakes):
    fakes["meet"].fail = True
    token = await _register_and_connect(client)
    resp = await client.post(
        "/v1/meetings", json=_body(notes=True), headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["notes_enabled"] is False
    assert body["warning"]


async def test_create_meeting_validates_times(client, fakes):
    token = await _register_and_connect(client)
    bad = _body()
    bad["end_time"] = "2026-06-12T09:00:00+00:00"  # before start
    resp = await client.post("/v1/meetings", json=bad, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 422


async def test_list_get_delete_meeting(client, fakes):
    token = await _register_and_connect(client)
    created = await client.post("/v1/meetings", json=_body(), headers={"Authorization": f"Bearer {token}"})
    mid = created.json()["id"]

    listed = await client.get("/v1/meetings", headers={"Authorization": f"Bearer {token}"})
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    got = await client.get(f"/v1/meetings/{mid}", headers={"Authorization": f"Bearer {token}"})
    assert got.status_code == 200

    deleted = await client.delete(f"/v1/meetings/{mid}", headers={"Authorization": f"Bearer {token}"})
    assert deleted.status_code == 204

    missing = await client.get(f"/v1/meetings/{mid}", headers={"Authorization": f"Bearer {token}"})
    assert missing.status_code == 404
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_meetings_api.py -v`
Expected: FAIL (404 — routes not mounted)

- [ ] **Step 4: Write the router**

```python
# app/api/routes/meetings.py
import uuid

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
    except TokenRefreshError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Google connection needs to be reconnected.",
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
```

- [ ] **Step 5: Wire the router in `app/main.py`**

```python
# app/main.py
from fastapi import FastAPI

from app.api.routes import auth, connections, health, meetings


def create_app() -> FastAPI:
    application = FastAPI(title="Google Meet Notes")
    application.include_router(health.router)
    application.include_router(auth.router)
    application.include_router(connections.router)
    application.include_router(meetings.router)
    return application


app = create_app()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_meetings_api.py -v`
Expected: PASS (5 tests)

- [ ] **Step 7: Commit**

```bash
git add app/api/deps.py app/api/routes/meetings.py app/main.py tests/test_meetings_api.py
git commit -m "feat: meeting endpoints (create/list/get/delete)"
```

---

## Task 6: Full suite green + verification

- [ ] **Step 1: Run the whole suite**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: ALL pass (Phase 1 + 2 + 3; ~64 total).

- [ ] **Step 2: Confirm migration at head**

Run: `.venv\Scripts\python.exe -m alembic upgrade head`
Expected: no errors; `meetings` table present.

- [ ] **Step 3: Final commit (only if anything uncommitted)**

```bash
git add -A
git commit -m "chore: phase 3 meetings complete"
```

---

## Self-review (completed during planning)

- **Spec coverage (Phase 3 slice):** `meetings` table ✓ (Task 1); `POST /v1/meetings` creating Calendar event + Meet link + carrying the `notes_enabled` flag ✓ (Tasks 2,4,5); enabling auto-transcript when notes requested ✓ (Task 3,4); capability handling ✓ (Task 4 — refined to downgrade-with-warning rather than 422, documented above); list/get/delete ✓ (Tasks 4,5); 409 when not connected / needs reconnect ✓ (Task 5). Deferred by design: `PATCH /v1/meetings/{id}` (edit/toggle) — not needed for a complete create→manage slice, noted for a later enhancement; recurring-meeting occurrences, transcript fetch, and notes generation are Phases 4-6.
- **Placeholder scan:** none — all steps have complete code and exact commands.
- **Type consistency:** `CreatedEvent` fields (`event_id`, `meet_uri`, `meeting_code`, `conference_status`) are produced by `GoogleCalendarClient` and the `FakeCalendarClient`s, and consumed by `meeting_service`; `CalendarClient`/`MeetClient`/`OAuthClient` Protocol method signatures match the real clients and all fakes; `meeting_service.create_meeting` returns `tuple[Meeting, str | None]` and the route unpacks `(meeting, warning)`; `MeetingResponse.warning` is set on create only; dependency names `get_calendar_client`/`get_meet_client`/`get_oauth_client` are overridden by name in the API tests; `NotConnectedError` and `TokenRefreshError` are mapped to 409.
- **Known caveats for the implementer:** (1) Calendar conference creation can return `status=pending` rather than `success` with no entry points yet; the parser tolerates a missing conference (returns `meet_uri=None`), and the service downgrades notes with a warning in that case — for the synchronous happy path the fakes return `success`. A real deployment may later poll for the link (note for hardening). (2) `meeting_service.create_meeting` catches a broad `Exception` around transcript enabling on purpose (capability/permission/network all downgrade gracefully) — this is intentional, not a smell. (3) FastAPI empty-path routes (`@router.post("")`) under the `/v1/meetings` prefix resolve at `/v1/meetings` with the non-redirect-following test client, consistent with the Phase 2 connections routes.
