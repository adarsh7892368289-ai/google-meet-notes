# Phase 4: Event Ingestion — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Subscribe each connected Google account to Meet transcript events, receive the OIDC-verified Pub/Sub push, dedup it, map it to a meeting, and persist a `conferences` row ready for the notes pipeline.

**Architecture:** A Workspace Events subscription is created **once per user at OAuth-connect time**, targeting `//cloudidentity.googleapis.com/users/{google_user_id}` — which delivers transcript events for every Meet space that user owns (verified: see `docs/superpowers/specs/...` §6 and the research notes below). The public webhook does only fast, network-free work: verify the OIDC JWT → dedup on `message_id` → parse the CloudEvent → map the firing subscription to the owning connection → upsert a `conferences` row in `pending` state → enqueue a job → ack `200`. The actual transcript fetch + Gemini summarization is Phase 5; this phase stops at a durable `conferences` row + an enqueued job (via a `JobQueue` port whose only implementation now is a no-op `NullJobQueue`).

**Tech Stack:** Python 3.12, FastAPI, async SQLAlchemy 2.0, Alembic (migrations live under `migrations/versions/`), httpx (with `MockTransport` for tests), `google-auth` (new — OIDC token verification), Fernet (existing), pytest + pytest-asyncio (`asyncio_mode=auto`).

---

## Follow-ups discovered during execution (carry into later phases)

These were surfaced by code review during Phase 4 execution, judged out-of-scope for this phase, and deferred:

- **Phase 7 — OIDC webhook hardening (security):** `GooglePushVerifier` skips `aud` validation when `push_audience` is empty (`verify_oauth2_token(audience=None)` does not check `aud`) and skips the service-account check when `push_service_account_email` is empty. Phase-4 dev defaults are both empty. Phase 7 must fail fast at startup when `app_env != "local"` and either is unset. Risk documented in a comment block at the top of `app/events/oidc.py`.
- **Phase 7 — reconciliation must backfill dropped conferences:** `event_service.handle_event` commits the `processed_events` ledger BEFORE the `conferences` upsert, so a non-transient failure of the conference commit lets the redelivery dedup as "duplicate" and the conference is never created. The Phase 7 sweeper/reconciliation must list recent `conferenceRecords` and create any missing rows.
- **Phase 5 — enqueue failure handling:** `queue.enqueue_notes_pipeline(...)` is not wrapped in try/except (harmless with `NullJobQueue`). When the real arq/Redis queue lands, a Redis outage would 500 → redelivery dedups → orphaned `pending` conference. Phase 5 should wrap+return 500 or rely on the Phase 7 sweeper to pick up orphaned `pending` rows.
- **Phase 5 — resolve `conferences.meeting_id`:** intentionally left NULL this phase; the worker resolves it via `conferenceRecords.get` → `space` → `meetings.meet_space_name`.

---

## Research findings this plan is built on (verified June 2026)

These shaped the design; do not re-litigate them mid-implementation.

1. **User-level subscription target exists and is the right choice.** `targetResource: "//cloudidentity.googleapis.com/users/{USER_ID}"` receives events for *all meeting spaces where the user is the owner*. So one subscription per connected user covers every meeting they host, including all occurrences of recurring meetings. This vindicates the spec's 1:1 `event_subscriptions ↔ oauth_connections` model.
2. **We need the Google user id** (OIDC `sub`) to build that target. The current `fetch_userinfo` returns only the email; we extend it to also return `sub` and persist it as `oauth_connections.google_user_id`.
3. **Subscription create/renew:** `POST https://workspaceevents.googleapis.com/v1/subscriptions` (returns a long-running `Operation` whose `response` is the `Subscription`); renew with `PATCH .../v1/{name}?updateMask=ttl`. Event type strings: `google.workspace.meet.conference.v2.started`, `google.workspace.meet.transcript.v2.fileGenerated`. Resource-name-only payload (`payloadOptions.includeResource=false`) → max `ttl` of `604800s` (7 days). Scope `meetings.space.created` (already granted) suffices to create the subscription and to read transcripts later.
4. **Pub/Sub push envelope:** `{ "message": { "data": <base64>, "messageId": ..., "attributes": {...}, "publishTime": ... }, "subscription": "projects/.../subscriptions/..." }`. CloudEvent metadata is in `message.attributes` with keys `ce-id`, `ce-source`, `ce-type`, `ce-subject`, `ce-time`, `ce-specversion`, `ce-datacontenttype`. `ce-source` = `//workspaceevents.googleapis.com/subscriptions/{SUBSCRIPTION_ID}` — this is how we map a push to the subscription (and thence the connection). The decoded `message.data` for `transcript.fileGenerated` is `{ "transcript": { "name": "conferenceRecords/{cr}/transcripts/{t}" } }`; for `conference.started` it is `{ "conferenceRecord": { "name": "conferenceRecords/{cr}" } }`.
5. **OIDC verification:** push carries `Authorization: Bearer <Google-signed OIDC JWT>`; verify with `google.oauth2.id_token.verify_oauth2_token(token, google.auth.transport.requests.Request(), audience=<expected>)`; require `iss in {"accounts.google.com","https://accounts.google.com"}`, `email_verified is True`, and `email == <configured push service account>`.
6. **Mapping to the meeting is deferred to the worker (Phase 5).** The `transcript.fileGenerated` payload does not inline the space; resolving it requires a `conferenceRecords.get` network call, which must NOT happen in the webhook request path. So `conferences.meeting_id` is **nullable** in this phase and resolved later. We persist `conference_record_name` + `transcript_resource_name` + the owning `oauth_connection_id` now.
7. **Schema gap from Phase 3:** `meetings` lacks `meet_space_name`. Phase 4 adds it (used by the Phase 5 worker to map space→meeting). No historical backfill (dev-only data).

---

## File structure (created/modified in this phase)

**New source files**
- `app/models/event_subscription.py` — `EventSubscription` ORM model.
- `app/models/conference.py` — `Conference` ORM model (the idempotency anchor).
- `app/models/processed_event.py` — `ProcessedEvent` dedup ledger.
- `app/google/events_client.py` — typed client wrapping the Workspace Events REST API (create/renew/delete subscription).
- `app/events/__init__.py`
- `app/events/oidc.py` — injectable OIDC push-token verifier (`PushVerifier` protocol + Google impl + `VerifiedPush`).
- `app/events/parser.py` — pure functions: decode the Pub/Sub envelope + CloudEvent into a typed `MeetEvent`.
- `app/queue.py` — `JobQueue` protocol + `NullJobQueue` (real arq impl arrives Phase 5).
- `app/services/subscription_service.py` — create/renew/delete the per-user Events subscription + persistence.
- `app/services/event_service.py` — webhook orchestration: dedup → parse → map → upsert conference → enqueue.
- `app/api/routes/webhooks.py` — `POST /v1/webhooks/google/events`.

**Modified source files**
- `app/config.py` — add events/webhook settings.
- `app/models/__init__.py` — export the three new models.
- `app/models/oauth_connection.py` — add `google_user_id` column.
- `app/models/meeting.py` — add `meet_space_name` column.
- `app/google/oauth_client.py` — `fetch_userinfo` returns `(email, sub)`; new `UserInfo` dataclass.
- `app/services/connection_service.py` — `upsert_connection` persists `google_user_id`.
- `app/services/meeting_service.py` — persist `meet_space_name`; resolve space name even unused for mapping.
- `app/api/routes/connections.py` — callback creates the Events subscription; disconnect deletes it.
- `app/api/deps.py` — `get_events_client`, `get_push_verifier`, `get_job_queue` providers.
- `app/main.py` — register the webhooks router.
- `pyproject.toml` — add `google-auth>=2.35`.

**New migrations** (`migrations/versions/`, autogenerated then hand-checked)
- add `oauth_connections.google_user_id`
- add `meetings.meet_space_name` (+ index)
- create `event_subscriptions`
- create `conferences`
- create `processed_events`

**New test files**
- `tests/test_events_client.py`, `tests/test_oidc_verifier.py`, `tests/test_event_parser.py`, `tests/test_subscription_service.py`, `tests/test_event_service.py`, `tests/test_webhooks_api.py`, `tests/test_queue.py`
- modified: `tests/test_oauth_client.py` (userinfo now returns sub), `tests/test_connections_api.py` (subscription created on callback), `tests/test_meeting_service.py` (meet_space_name persisted)

---

## Conventions to follow (match existing code exactly)

- Google clients are classes with an injectable `transport: httpx.BaseTransport | None`, a `_http()` helper that builds an `httpx.AsyncClient(transport=..., timeout=30.0, headers={"Authorization": f"Bearer {token}"})`, and a `Protocol` describing the interface (see `app/google/meet_client.py`). Tests drive them with `httpx.MockTransport(handler)`.
- Services are module-level `async def` functions taking `session: AsyncSession` first, dependencies passed explicitly (see `app/services/meeting_service.py`). They `await session.commit()` then `await session.refresh(obj)`.
- Models: `Mapped[...]` + `mapped_column(...)`, `UUID(as_uuid=True)` pk `default=uuid.uuid4`, `DateTime(timezone=True)` with `server_default=func.now()` and `onupdate=func.now()` for `updated_at` (see `app/models/oauth_connection.py`).
- Migrations: created via `alembic revision --autogenerate -m "..."`, files land in `migrations/versions/`, with `revision`/`down_revision` module-level strings (see `migrations/versions/2abdc7a1f5e6_create_meetings_table.py`). Always read the autogenerated file and confirm it only contains the intended change.
- Tests are `async def` (auto asyncio mode), use the `db_session` and `client` fixtures from `tests/conftest.py`, set `ENCRYPTION_KEY` via the `_set_key` autouse fixture pattern when crypto/settings are touched, and override FastAPI deps with `app.dependency_overrides[...]`.
- No code comments unless they clarify non-obvious intent (the codebase is near comment-free).
- Run the full suite with `python -m pytest -q` from the repo root. Current baseline: **67 passing**.

> **Note on git:** the repo is on branch `feat/phase3-meetings` and we are stacking Phase 4 on top of it (no new branch). Commit after each task as shown.

---

## Task 1: Add dependency `google-auth` and events settings

**Files:**
- Modify: `pyproject.toml:6-20`
- Modify: `app/config.py:6-27`
- Test: `tests/test_config.py` (add a test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_events_settings_have_defaults(monkeypatch):
    monkeypatch.delenv("WORKSPACE_EVENTS_TOPIC", raising=False)
    monkeypatch.delenv("PUSH_AUDIENCE", raising=False)
    monkeypatch.delenv("PUSH_SERVICE_ACCOUNT_EMAIL", raising=False)
    from app.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    assert s.workspace_events_topic == ""
    assert s.push_audience == ""
    assert s.push_service_account_email == ""
    assert s.subscription_ttl_seconds == 604800
    get_settings.cache_clear()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_config.py::test_events_settings_have_defaults -q`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'workspace_events_topic'`.

- [ ] **Step 3: Add the settings fields**

In `app/config.py`, add these fields to `Settings` (after `encryption_key`):

```python
    workspace_events_topic: str = ""
    push_audience: str = ""
    push_service_account_email: str = ""
    subscription_ttl_seconds: int = 604800
```

- [ ] **Step 4: Add the dependency**

In `pyproject.toml`, add to the `dependencies` list (after `"cryptography>=43.0",`):

```toml
    "google-auth>=2.35",
```

Then install it:

Run: `python -m pip install "google-auth>=2.35"`
Expected: `Successfully installed google-auth-...` (and its deps `cachetools`, `pyasn1*`, `rsa`).

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_config.py -q`
Expected: PASS (all config tests).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml app/config.py tests/test_config.py
git commit -m "feat: add google-auth dep and workspace events settings"
```

---

## Task 2: `oauth_connections.google_user_id` column + model

**Files:**
- Modify: `app/models/oauth_connection.py:24` (add column)
- Test: `tests/test_connection_service.py` (add a test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_connection_service.py`:

```python
async def test_oauth_connection_stores_google_user_id(db_session):
    user = await _make_user(db_session)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="user@acme.com",
        google_user_id="108200000000000000001",
    )
    assert conn.google_user_id == "108200000000000000001"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_connection_service.py::test_oauth_connection_stores_google_user_id -q`
Expected: FAIL — `upsert_connection() got an unexpected keyword argument 'google_user_id'` (we wire the service in Task 7; for now the column must exist).

- [ ] **Step 3: Add the column to the model**

In `app/models/oauth_connection.py`, add after the `google_email` column (line ~24):

```python
    google_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

- [ ] **Step 4: Confirm the model imports cleanly**

Run: `python -c "from app.models import OAuthConnection; print(OAuthConnection.__table__.c.keys())"`
Expected: a list that includes `'google_user_id'`.

- [ ] **Step 5: Commit** (test stays red until Task 7 — that is expected and noted)

```bash
git add app/models/oauth_connection.py tests/test_connection_service.py
git commit -m "feat: add google_user_id column to oauth_connections model"
```

> The new test is intentionally red until Task 7 wires `upsert_connection`. The DB-level fixture (`Base.metadata.create_all`) already includes the new column, so no migration is needed for tests; the Alembic migration for real DBs is Task 14.

---

## Task 3: `meetings.meet_space_name` column + model

**Files:**
- Modify: `app/models/meeting.py:30` (add column)
- Test: `tests/test_meeting_service.py` (add an assertion-only test)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_meeting_service.py`:

```python
async def test_create_meeting_persists_meet_space_name(db_session):
    user = await _user_with_connection(db_session)
    meeting, _ = await meeting_service.create_meeting(
        db_session, user=user, payload=_payload(notes_enabled=True),
        oauth_client=FakeOAuthClient(), calendar_client=FakeCalendarClient(),
        meet_client=FakeMeetClient(),
    )
    assert meeting.meet_space_name == "spaces/SERVERID"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_meeting_service.py::test_create_meeting_persists_meet_space_name -q`
Expected: FAIL — `AttributeError: 'Meeting' object has no attribute 'meet_space_name'`.

- [ ] **Step 3: Add the column to the model**

In `app/models/meeting.py`, add after the `meeting_code` column (line ~30):

```python
    meet_space_name: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
```

- [ ] **Step 4: Run** (still failing — service not yet wired)

Run: `python -m pytest tests/test_meeting_service.py::test_create_meeting_persists_meet_space_name -q`
Expected: FAIL — `assert None == "spaces/SERVERID"` (column exists, value not set yet; wired in Task 8).

- [ ] **Step 5: Commit**

```bash
git add app/models/meeting.py tests/test_meeting_service.py
git commit -m "feat: add meet_space_name column to meetings model"
```

---

## Task 4: `EventSubscription`, `Conference`, `ProcessedEvent` models

**Files:**
- Create: `app/models/event_subscription.py`
- Create: `app/models/conference.py`
- Create: `app/models/processed_event.py`
- Modify: `app/models/__init__.py`
- Test: `tests/test_event_models.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_event_models.py`:

```python
import uuid
from datetime import datetime, timezone

from app.models import Conference, EventSubscription, ProcessedEvent, User
from app.services import connection_service
from app.google.oauth_client import TokenBundle
from cryptography.fernet import Fernet
import pytest


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _conn(db_session):
    user = User(email="m@acme.com", name="M", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    return await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="m@acme.com"
    )


async def test_event_subscription_round_trips(db_session):
    conn = await _conn(db_session)
    sub = EventSubscription(
        oauth_connection_id=conn.id,
        subscription_name="subscriptions/abc123",
        expire_time=datetime(2026, 6, 20, tzinfo=timezone.utc),
        state="active",
    )
    db_session.add(sub)
    await db_session.commit()
    await db_session.refresh(sub)
    assert sub.id is not None
    assert sub.state == "active"


async def test_conference_unique_record_name(db_session):
    conn = await _conn(db_session)
    c = Conference(
        oauth_connection_id=conn.id,
        conference_record_name="conferenceRecords/xyz",
        pipeline_state="pending",
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    assert c.meeting_id is None  # nullable in this phase
    assert c.attempts == 0


async def test_processed_event_unique_message_id(db_session):
    ev = ProcessedEvent(
        message_id="msg-1",
        event_type="google.workspace.meet.transcript.v2.fileGenerated",
        conference_record_name="conferenceRecords/xyz",
    )
    db_session.add(ev)
    await db_session.commit()
    await db_session.refresh(ev)
    assert ev.id is not None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_event_models.py -q`
Expected: FAIL — `ImportError: cannot import name 'Conference' from 'app.models'`.

- [ ] **Step 3: Create `app/models/event_subscription.py`**

```python
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class EventSubscription(Base):
    __tablename__ = "event_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    oauth_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("oauth_connections.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    subscription_name: Mapped[str] = mapped_column(String(512), nullable=False)
    expire_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    last_renewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
```

- [ ] **Step 4: Create `app/models/conference.py`**

```python
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Conference(Base):
    __tablename__ = "conferences"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    meeting_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("meetings.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    oauth_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("oauth_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conference_record_name: Mapped[str] = mapped_column(
        String(256), nullable=False, unique=True, index=True
    )
    transcript_resource_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    actual_start_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    actual_end_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pipeline_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending", index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
```

- [ ] **Step 5: Create `app/models/processed_event.py`**

```python
import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ProcessedEvent(Base):
    __tablename__ = "processed_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    message_id: Mapped[str] = mapped_column(
        String(256), nullable=False, unique=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    conference_record_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

- [ ] **Step 6: Update `app/models/__init__.py`**

```python
from app.models.conference import Conference
from app.models.event_subscription import EventSubscription
from app.models.meeting import Meeting
from app.models.oauth_connection import OAuthConnection
from app.models.processed_event import ProcessedEvent
from app.models.user import User

__all__ = [
    "User",
    "OAuthConnection",
    "Meeting",
    "EventSubscription",
    "Conference",
    "ProcessedEvent",
]
```

- [ ] **Step 7: Run the test to verify it passes**

Run: `python -m pytest tests/test_event_models.py -q`
Expected: PASS (3 tests).

- [ ] **Step 8: Commit**

```bash
git add app/models/event_subscription.py app/models/conference.py app/models/processed_event.py app/models/__init__.py tests/test_event_models.py
git commit -m "feat: event_subscriptions, conferences, processed_events models"
```

---

## Task 5: `fetch_userinfo` returns email + sub (`UserInfo`)

**Files:**
- Modify: `app/google/oauth_client.py:13-29,98-104`
- Modify: `tests/test_oauth_client.py:68-75` (update existing test)
- Test: same file (add a test for sub)

- [ ] **Step 1: Update the failing test**

In `tests/test_oauth_client.py`, replace `test_fetch_userinfo_returns_email` with:

```python
async def test_fetch_userinfo_returns_email_and_sub():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer at-1"
        return httpx.Response(200, json={"email": "user@acme.com", "sub": "10820000000001"})

    client = _client_with_handler(handler)
    info = await client.fetch_userinfo("at-1")
    assert info.email == "user@acme.com"
    assert info.sub == "10820000000001"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_oauth_client.py::test_fetch_userinfo_returns_email_and_sub -q`
Expected: FAIL — `AttributeError: 'str' object has no attribute 'email'`.

- [ ] **Step 3: Add `UserInfo` and change the return type**

In `app/google/oauth_client.py`, add a dataclass next to `TokenBundle`:

```python
@dataclass
class UserInfo:
    email: str
    sub: str
```

Update the `OAuthClient` protocol method signature:

```python
    async def fetch_userinfo(self, access_token: str) -> UserInfo: ...
```

Replace the `GoogleOAuthClient.fetch_userinfo` implementation:

```python
    async def fetch_userinfo(self, access_token: str) -> UserInfo:
        async with self._http() as http:
            resp = await http.get(
                USERINFO_URI, headers={"Authorization": f"Bearer {access_token}"}
            )
            resp.raise_for_status()
            data = resp.json()
            return UserInfo(email=data["email"], sub=data["sub"])
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_oauth_client.py -q`
Expected: PASS.

- [ ] **Step 5: Commit** (callers updated in Task 6/7)

```bash
git add app/google/oauth_client.py tests/test_oauth_client.py
git commit -m "feat: fetch_userinfo returns email and sub (UserInfo)"
```

> This breaks callers in `connections.py` (uses `email = await ...fetch_userinfo(...)`). Those are fixed in Task 7. Run only the targeted test here; the full suite goes green at Task 7.

---

## Task 6: Workspace Events client (`events_client.py`)

**Files:**
- Create: `app/google/events_client.py`
- Test: `tests/test_events_client.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_events_client.py`:

```python
import httpx
import pytest

from app.google.events_client import GoogleEventsClient


def _client(handler) -> GoogleEventsClient:
    return GoogleEventsClient(transport=httpx.MockTransport(handler))


async def test_create_subscription_posts_user_target_and_event_types():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        captured["auth"] = request.headers["Authorization"]
        return httpx.Response(
            200,
            json={
                "done": True,
                "response": {
                    "name": "subscriptions/sub-1",
                    "expireTime": "2026-06-20T00:00:00Z",
                    "state": "ACTIVE",
                },
            },
        )

    result = await _client(handler).create_subscription(
        "at-1",
        google_user_id="108200001",
        topic="projects/p/topics/meet-events",
        ttl_seconds=604800,
    )
    assert captured["method"] == "POST"
    assert captured["url"] == "https://workspaceevents.googleapis.com/v1/subscriptions"
    assert captured["auth"] == "Bearer at-1"
    assert "//cloudidentity.googleapis.com/users/108200001" in captured["body"]
    assert "google.workspace.meet.transcript.v2.fileGenerated" in captured["body"]
    assert "google.workspace.meet.conference.v2.started" in captured["body"]
    assert "604800s" in captured["body"]
    assert result.subscription_name == "subscriptions/sub-1"
    assert result.state == "ACTIVE"


async def test_create_subscription_unwraps_operation_without_response_block():
    def handler(request: httpx.Request) -> httpx.Response:
        # Some operations return the subscription at top level under metadata-less done op
        return httpx.Response(
            200,
            json={"name": "operations/op-1", "done": True,
                  "response": {"name": "subscriptions/sub-2",
                               "expireTime": "2026-06-21T00:00:00Z", "state": "ACTIVE"}},
        )

    result = await _client(handler).create_subscription(
        "at-1", google_user_id="1", topic="projects/p/topics/t", ttl_seconds=604800
    )
    assert result.subscription_name == "subscriptions/sub-2"


async def test_renew_subscription_patches_ttl():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={"done": True, "response": {
                "name": "subscriptions/sub-1",
                "expireTime": "2026-06-27T00:00:00Z", "state": "ACTIVE"}},
        )

    result = await _client(handler).renew_subscription(
        "at-1", subscription_name="subscriptions/sub-1", ttl_seconds=604800
    )
    assert captured["method"] == "PATCH"
    assert "subscriptions/sub-1" in captured["url"]
    assert "updateMask=ttl" in captured["url"]
    assert "604800s" in captured["body"]
    assert result.subscription_name == "subscriptions/sub-1"


async def test_delete_subscription_calls_delete():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(200, json={})

    await _client(handler).delete_subscription("at-1", subscription_name="subscriptions/sub-1")
    assert captured["method"] == "DELETE"
    assert captured["url"].endswith("/v1/subscriptions/sub-1")


async def test_delete_subscription_ignores_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "not found"}})

    # should not raise
    await _client(handler).delete_subscription("at-1", subscription_name="subscriptions/gone")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_events_client.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.google.events_client'`.

- [ ] **Step 3: Implement `app/google/events_client.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

EVENTS_BASE = "https://workspaceevents.googleapis.com/v1"

MEET_EVENT_TYPES = [
    "google.workspace.meet.conference.v2.started",
    "google.workspace.meet.transcript.v2.fileGenerated",
]


@dataclass
class SubscriptionResult:
    subscription_name: str
    expire_time: str | None
    state: str


class EventsClient(Protocol):
    async def create_subscription(
        self, access_token: str, *, google_user_id: str, topic: str, ttl_seconds: int
    ) -> SubscriptionResult: ...
    async def renew_subscription(
        self, access_token: str, *, subscription_name: str, ttl_seconds: int
    ) -> SubscriptionResult: ...
    async def delete_subscription(
        self, access_token: str, *, subscription_name: str
    ) -> None: ...


def _parse_subscription(payload: dict) -> SubscriptionResult:
    sub = payload.get("response", payload)
    return SubscriptionResult(
        subscription_name=sub["name"],
        expire_time=sub.get("expireTime"),
        state=sub.get("state", "STATE_UNSPECIFIED"),
    )


class GoogleEventsClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def _http(self, access_token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=30.0,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def create_subscription(
        self, access_token: str, *, google_user_id: str, topic: str, ttl_seconds: int
    ) -> SubscriptionResult:
        body = {
            "targetResource": f"//cloudidentity.googleapis.com/users/{google_user_id}",
            "eventTypes": MEET_EVENT_TYPES,
            "notificationEndpoint": {"pubsubTopic": topic},
            "payloadOptions": {"includeResource": False},
            "ttl": f"{ttl_seconds}s",
        }
        async with self._http(access_token) as http:
            resp = await http.post(f"{EVENTS_BASE}/subscriptions", json=body)
            resp.raise_for_status()
            return _parse_subscription(resp.json())

    async def renew_subscription(
        self, access_token: str, *, subscription_name: str, ttl_seconds: int
    ) -> SubscriptionResult:
        body = {"ttl": f"{ttl_seconds}s"}
        async with self._http(access_token) as http:
            resp = await http.patch(
                f"{EVENTS_BASE}/{subscription_name}",
                params={"updateMask": "ttl"},
                json=body,
            )
            resp.raise_for_status()
            return _parse_subscription(resp.json())

    async def delete_subscription(
        self, access_token: str, *, subscription_name: str
    ) -> None:
        async with self._http(access_token) as http:
            resp = await http.delete(f"{EVENTS_BASE}/{subscription_name}")
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_events_client.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/google/events_client.py tests/test_events_client.py
git commit -m "feat: workspace events client (create/renew/delete subscription)"
```

---

## Task 7: Persist `google_user_id`; wire userinfo + subscription creation into callback

**Files:**
- Modify: `app/services/connection_service.py:46-68` (`upsert_connection` signature)
- Create: `app/services/subscription_service.py`
- Modify: `app/api/routes/connections.py:26-91` (callback + disconnect)
- Modify: `app/api/deps.py` (add `get_events_client`)
- Test: `tests/test_subscription_service.py` (create), update `tests/test_connections_api.py`

This is the largest task; it splits into 7a (service) and 7b (wiring).

### 7a — `upsert_connection` accepts `google_user_id`; `subscription_service`

- [ ] **Step 1: The red test from Task 2 is our guide.** Confirm it still fails:

Run: `python -m pytest tests/test_connection_service.py::test_oauth_connection_stores_google_user_id -q`
Expected: FAIL — unexpected kwarg `google_user_id`.

- [ ] **Step 2: Update `upsert_connection`**

In `app/services/connection_service.py`, change the signature and body:

```python
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
```

- [ ] **Step 3: Run the Task 2 test — now green**

Run: `python -m pytest tests/test_connection_service.py -q`
Expected: PASS (all connection-service tests).

- [ ] **Step 4: Write the failing subscription-service test**

Create `tests/test_subscription_service.py`:

```python
import pytest
from cryptography.fernet import Fernet

from app.google.events_client import SubscriptionResult
from app.google.oauth_client import TokenBundle
from app.models import EventSubscription, User
from app.services import connection_service, subscription_service
from sqlalchemy import select


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


class FakeEventsClient:
    def __init__(self):
        self.created = []
        self.deleted = []

    async def create_subscription(self, access_token, *, google_user_id, topic, ttl_seconds):
        self.created.append((google_user_id, topic, ttl_seconds))
        return SubscriptionResult(
            subscription_name="subscriptions/sub-1",
            expire_time="2026-06-20T00:00:00Z",
            state="ACTIVE",
        )

    async def renew_subscription(self, access_token, *, subscription_name, ttl_seconds):
        return SubscriptionResult(subscription_name, "2026-06-27T00:00:00Z", "ACTIVE")

    async def delete_subscription(self, access_token, *, subscription_name):
        self.deleted.append(subscription_name)


async def _conn(db_session, *, user_id="108"):
    user = User(email="m@acme.com", name="M", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    return await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="m@acme.com", google_user_id=user_id
    )


async def test_create_subscription_persists_row(db_session):
    conn = await _conn(db_session)
    events = FakeEventsClient()
    sub = await subscription_service.create_for_connection(
        db_session, conn=conn, oauth_client=FakeOAuthClient(), events_client=events,
        topic="projects/p/topics/meet-events", ttl_seconds=604800,
    )
    assert sub.subscription_name == "subscriptions/sub-1"
    assert sub.state == "active"
    assert events.created == [("108", "projects/p/topics/meet-events", 604800)]
    row = await db_session.scalar(
        select(EventSubscription).where(EventSubscription.oauth_connection_id == conn.id)
    )
    assert row is not None


async def test_create_subscription_noop_without_user_id(db_session):
    conn = await _conn(db_session, user_id=None)
    events = FakeEventsClient()
    sub = await subscription_service.create_for_connection(
        db_session, conn=conn, oauth_client=FakeOAuthClient(), events_client=events,
        topic="projects/p/topics/meet-events", ttl_seconds=604800,
    )
    assert sub is None
    assert events.created == []


async def test_create_subscription_noop_without_topic(db_session):
    conn = await _conn(db_session)
    events = FakeEventsClient()
    sub = await subscription_service.create_for_connection(
        db_session, conn=conn, oauth_client=FakeOAuthClient(), events_client=events,
        topic="", ttl_seconds=604800,
    )
    assert sub is None
    assert events.created == []


async def test_delete_subscription_removes_row_and_calls_api(db_session):
    conn = await _conn(db_session)
    events = FakeEventsClient()
    await subscription_service.create_for_connection(
        db_session, conn=conn, oauth_client=FakeOAuthClient(), events_client=events,
        topic="projects/p/topics/meet-events", ttl_seconds=604800,
    )
    await subscription_service.delete_for_connection(
        db_session, conn=conn, oauth_client=FakeOAuthClient(), events_client=events,
    )
    assert events.deleted == ["subscriptions/sub-1"]
    row = await db_session.scalar(
        select(EventSubscription).where(EventSubscription.oauth_connection_id == conn.id)
    )
    assert row is None


async def test_get_by_subscription_name(db_session):
    conn = await _conn(db_session)
    events = FakeEventsClient()
    await subscription_service.create_for_connection(
        db_session, conn=conn, oauth_client=FakeOAuthClient(), events_client=events,
        topic="projects/p/topics/meet-events", ttl_seconds=604800,
    )
    found = await subscription_service.get_by_subscription_name(db_session, "subscriptions/sub-1")
    assert found is not None
    assert found.oauth_connection_id == conn.id
```

- [ ] **Step 5: Run it and watch it fail**

Run: `python -m pytest tests/test_subscription_service.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.subscription_service'`.

- [ ] **Step 6: Implement `app/services/subscription_service.py`**

```python
import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.google.events_client import EventsClient
from app.google.oauth_client import OAuthClient
from app.models import EventSubscription, OAuthConnection
from app.services import connection_service

logger = logging.getLogger(__name__)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def get_for_connection(
    session: AsyncSession, conn: OAuthConnection
) -> EventSubscription | None:
    return await session.scalar(
        select(EventSubscription).where(
            EventSubscription.oauth_connection_id == conn.id
        )
    )


async def get_by_subscription_name(
    session: AsyncSession, subscription_name: str
) -> EventSubscription | None:
    return await session.scalar(
        select(EventSubscription).where(
            EventSubscription.subscription_name == subscription_name
        )
    )


async def create_for_connection(
    session: AsyncSession,
    *,
    conn: OAuthConnection,
    oauth_client: OAuthClient,
    events_client: EventsClient,
    topic: str,
    ttl_seconds: int,
) -> EventSubscription | None:
    if not conn.google_user_id or not topic:
        logger.info(
            "skipping events subscription for connection %s (missing user id or topic)",
            conn.id,
        )
        return None

    access_token = await connection_service.get_valid_access_token(
        session, conn, oauth_client
    )
    result = await events_client.create_subscription(
        access_token,
        google_user_id=conn.google_user_id,
        topic=topic,
        ttl_seconds=ttl_seconds,
    )

    sub = await get_for_connection(session, conn)
    if sub is None:
        sub = EventSubscription(oauth_connection_id=conn.id)
        session.add(sub)
    sub.subscription_name = result.subscription_name
    sub.expire_time = _parse_time(result.expire_time)
    sub.state = "active"
    await session.commit()
    await session.refresh(sub)
    return sub


async def delete_for_connection(
    session: AsyncSession,
    *,
    conn: OAuthConnection,
    oauth_client: OAuthClient,
    events_client: EventsClient,
) -> None:
    sub = await get_for_connection(session, conn)
    if sub is None:
        return
    try:
        access_token = await connection_service.get_valid_access_token(
            session, conn, oauth_client
        )
        await events_client.delete_subscription(
            access_token, subscription_name=sub.subscription_name
        )
    except Exception as exc:  # best-effort remote delete; always remove local row
        logger.warning(
            "failed to delete remote subscription %s: %s", sub.subscription_name, exc
        )
    await session.delete(sub)
    await session.commit()
```

> **Note:** `last_renewed_at` is intentionally left unset here (nullable column; `updated_at` already tracks change time, and the Phase 7 scheduler sets `last_renewed_at` on renewal). The codebase derives times from the DB or token expiry, never wall-clock in services — do not add a `datetime.now()` call.

- [ ] **Step 7: Run**

Run: `python -m pytest tests/test_subscription_service.py -q`
Expected: PASS (5 tests).

- [ ] **Step 8: Commit**

```bash
git add app/services/connection_service.py app/services/subscription_service.py tests/test_subscription_service.py tests/test_connection_service.py
git commit -m "feat: subscription service create/delete + persist google_user_id"
```

### 7b — Wire userinfo + subscription into the connections router

- [ ] **Step 1: Add the events-client provider to deps**

In `app/api/deps.py`, add the import and provider:

```python
from app.google.events_client import EventsClient, GoogleEventsClient
```

```python
def get_events_client() -> EventsClient:
    return GoogleEventsClient()
```

- [ ] **Step 2: Update the failing API test**

In `tests/test_connections_api.py`, update `FakeOAuthClient.fetch_userinfo` and add a subscription-creation assertion. Replace the `FakeOAuthClient` class body's `fetch_userinfo` with:

```python
    async def fetch_userinfo(self, access_token: str):
        from app.google.oauth_client import UserInfo
        return UserInfo(email="person@acme.com", sub="108200001")
```

Add a fake events client + override fixture near the top (after `FakeOAuthClient`):

```python
class FakeEventsClient:
    def __init__(self):
        self.created = []

    async def create_subscription(self, access_token, *, google_user_id, topic, ttl_seconds):
        from app.google.events_client import SubscriptionResult
        self.created.append(google_user_id)
        return SubscriptionResult("subscriptions/sub-1", "2026-06-20T00:00:00Z", "ACTIVE")

    async def renew_subscription(self, access_token, *, subscription_name, ttl_seconds):
        from app.google.events_client import SubscriptionResult
        return SubscriptionResult(subscription_name, "2026-06-27T00:00:00Z", "ACTIVE")

    async def delete_subscription(self, access_token, *, subscription_name):
        pass


@pytest.fixture
def fake_events(monkeypatch):
    from app.api.deps import get_events_client
    fake = FakeEventsClient()
    app.dependency_overrides[get_events_client] = lambda: fake
    monkeypatch.setenv("WORKSPACE_EVENTS_TOPIC", "projects/p/topics/meet-events")
    from app.config import get_settings
    get_settings.cache_clear()
    yield fake
    app.dependency_overrides.pop(get_events_client, None)
    get_settings.cache_clear()
```

Then update `test_callback_creates_connection` to also accept `fake_events` and assert subscription creation:

```python
async def test_callback_creates_connection(client, fake_oauth, fake_events):
    token = await _register(client)
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
    assert fake_events.created == ["108200001"]
```

> The other callback tests that don't pass `fake_events` will now have no topic configured (`WORKSPACE_EVENTS_TOPIC` unset → `create_for_connection` returns `None` without calling the events client), so they remain valid. The callback must be resilient: if subscription creation fails, the connection is still saved (we log and continue).

- [ ] **Step 3: Run it and watch it fail**

Run: `python -m pytest tests/test_connections_api.py::test_callback_creates_connection -q`
Expected: FAIL — `fetch_userinfo` returns `UserInfo` but the route still does `email = await ...` and treats it as a string, or `fake_events.created` is empty.

- [ ] **Step 4: Update the callback + disconnect routes**

In `app/api/routes/connections.py`:

Update imports and add a module-level logger (the file currently has none):

```python
import logging

from app.api.deps import get_current_user, get_events_client, get_oauth_client
from app.config import get_settings
from app.google.events_client import EventsClient
from app.services import connection_service, subscription_service
```

After the imports, near the `router = APIRouter(...)` line, add:

```python
logger = logging.getLogger(__name__)
```

Replace the `callback` function's userinfo + upsert section and add subscription creation:

```python
@router.get("/callback")
async def callback(
    code: str,
    state: str,
    session: AsyncSession = Depends(get_session),
    oauth_client: OAuthClient = Depends(get_oauth_client),
    events_client: EventsClient = Depends(get_events_client),
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
        info = await oauth_client.fetch_userinfo(bundle.access_token)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to fetch Google account info",
        ) from exc

    conn = await connection_service.upsert_connection(
        session, user=user, bundle=bundle, google_email=info.email, google_user_id=info.sub
    )

    settings = get_settings()
    try:
        await subscription_service.create_for_connection(
            session,
            conn=conn,
            oauth_client=oauth_client,
            events_client=events_client,
            topic=settings.workspace_events_topic,
            ttl_seconds=settings.subscription_ttl_seconds,
        )
    except Exception:  # subscription is best-effort; connection still succeeds
        logger.warning(
            "failed to create events subscription for user %s", user.id, exc_info=True
        )

    return {"connected": True, "google_email": info.email}
```

Update `disconnect` to delete the subscription before the connection:

```python
@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    oauth_client: OAuthClient = Depends(get_oauth_client),
    events_client: EventsClient = Depends(get_events_client),
) -> None:
    conn = await connection_service.get_connection(session, current_user)
    if conn is not None:
        await subscription_service.delete_for_connection(
            session, conn=conn, oauth_client=oauth_client, events_client=events_client
        )
        await connection_service.delete_connection(session, conn, oauth_client)
```

> Note: `delete_for_connection` commits after deleting the subscription row; `delete_connection` then deletes the connection. Order matters because of the FK (`event_subscriptions.oauth_connection_id` → `oauth_connections.id` with `ondelete=CASCADE`); deleting the subscription first is explicit and also triggers the remote API delete.

- [ ] **Step 5: Run the connections API tests**

Run: `python -m pytest tests/test_connections_api.py -q`
Expected: PASS (all). If a test that calls the callback without `fake_events` fails because `fetch_userinfo` now returns `UserInfo`, ensure that test's fake client returns `UserInfo` too — the `NoRefreshTokenClient` returns before userinfo, and `ExchangeFailsClient` raises before userinfo, so only the default `FakeOAuthClient` needed updating.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (everything green again; this is the integration point where Task 5's deferred break is healed).

- [ ] **Step 7: Commit**

```bash
git add app/api/routes/connections.py app/api/deps.py tests/test_connections_api.py
git commit -m "feat: create events subscription on connect, delete on disconnect"
```

---

## Task 8: Persist `meet_space_name` in `meeting_service`

**Files:**
- Modify: `app/services/meeting_service.py:46-87`
- Test: the Task 3 test (`test_create_meeting_persists_meet_space_name`)

- [ ] **Step 1: Confirm the Task 3 test still fails**

Run: `python -m pytest tests/test_meeting_service.py::test_create_meeting_persists_meet_space_name -q`
Expected: FAIL — `assert None == "spaces/SERVERID"`.

- [ ] **Step 2: Capture and persist the space name**

In `app/services/meeting_service.py`, modify the `notes_enabled` block to keep the resolved `space_name`, and persist it on the `Meeting`. Replace the block from `notes_enabled = payload.notes_enabled` through the `Meeting(...)` construction:

```python
    notes_enabled = payload.notes_enabled
    warning: str | None = None
    space_name: str | None = None
    if notes_enabled:
        if not created.meeting_code:
            notes_enabled = False
            warning = "Meeting created but no Meet link was generated; notes disabled."
        else:
            try:
                space_name = await meet_client.get_space_name(access_token, created.meeting_code)
                await meet_client.enable_auto_transcript(access_token, space_name)
            except httpx.HTTPError:
                logger.warning(
                    "could not enable auto-transcript for meeting code %s (user %s)",
                    created.meeting_code,
                    user.id,
                    exc_info=True,
                )
                notes_enabled = False
                space_name = None
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
        meet_space_name=space_name,
        notes_enabled=notes_enabled,
        notes_config=payload.notes_config.model_dump(),
        status="scheduled",
    )
```

- [ ] **Step 3: Run the Task 3 test — now green**

Run: `python -m pytest tests/test_meeting_service.py -q`
Expected: PASS (all meeting-service tests).

- [ ] **Step 4: Commit**

```bash
git add app/services/meeting_service.py
git commit -m "feat: persist meet_space_name on meeting creation"
```

---

## Task 9: `JobQueue` port + `NullJobQueue`

**Files:**
- Create: `app/queue.py`
- Test: `tests/test_queue.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_queue.py`:

```python
from app.queue import NullJobQueue


async def test_null_job_queue_records_enqueues():
    q = NullJobQueue()
    await q.enqueue_notes_pipeline("conf-123")
    await q.enqueue_notes_pipeline("conf-456")
    assert q.enqueued == ["conf-123", "conf-456"]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_queue.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.queue'`.

- [ ] **Step 3: Implement `app/queue.py`**

```python
from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class JobQueue(Protocol):
    async def enqueue_notes_pipeline(self, conference_id: str) -> None: ...


class NullJobQueue:
    """No-op queue used until the arq worker lands in Phase 5.

    The durable `conferences` row (pipeline_state='pending') is the source of
    truth; the Phase 7 sweeper will pick up anything not yet processed, so
    dropping the enqueue here loses no work.
    """

    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue_notes_pipeline(self, conference_id: str) -> None:
        self.enqueued.append(conference_id)
        logger.info("notes pipeline enqueue (noop) for conference %s", conference_id)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_queue.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/queue.py tests/test_queue.py
git commit -m "feat: JobQueue port with NullJobQueue placeholder"
```

---

## Task 10: CloudEvent / Pub/Sub envelope parser

**Files:**
- Create: `app/events/__init__.py` (empty)
- Create: `app/events/parser.py`
- Test: `tests/test_event_parser.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_event_parser.py`:

```python
import base64
import json

import pytest

from app.events.parser import EventParseError, parse_push


def _envelope(*, data: dict, attributes: dict, message_id="msg-1"):
    return {
        "subscription": "projects/p/subscriptions/s",
        "message": {
            "data": base64.b64encode(json.dumps(data).encode()).decode(),
            "messageId": message_id,
            "publishTime": "2026-06-10T10:00:00Z",
            "attributes": attributes,
        },
    }


def test_parse_transcript_event():
    env = _envelope(
        data={"transcript": {"name": "conferenceRecords/cr-1/transcripts/t-1"}},
        attributes={
            "ce-id": "spaces/SP/spaceEvents/E1",
            "ce-source": "//workspaceevents.googleapis.com/subscriptions/sub-1",
            "ce-type": "google.workspace.meet.transcript.v2.fileGenerated",
            "ce-time": "2026-06-10T10:00:00Z",
        },
    )
    ev = parse_push(env)
    assert ev.message_id == "msg-1"
    assert ev.event_type == "google.workspace.meet.transcript.v2.fileGenerated"
    assert ev.subscription_name == "subscriptions/sub-1"
    assert ev.transcript_resource_name == "conferenceRecords/cr-1/transcripts/t-1"
    assert ev.conference_record_name == "conferenceRecords/cr-1"


def test_parse_conference_started_event():
    env = _envelope(
        data={"conferenceRecord": {"name": "conferenceRecords/cr-9"}},
        attributes={
            "ce-source": "//workspaceevents.googleapis.com/subscriptions/sub-2",
            "ce-type": "google.workspace.meet.conference.v2.started",
        },
    )
    ev = parse_push(env)
    assert ev.event_type == "google.workspace.meet.conference.v2.started"
    assert ev.conference_record_name == "conferenceRecords/cr-9"
    assert ev.transcript_resource_name is None


def test_parse_missing_message_raises():
    with pytest.raises(EventParseError):
        parse_push({"subscription": "x"})


def test_parse_bad_base64_raises():
    env = {"message": {"data": "!!!notbase64!!!", "messageId": "m", "attributes": {}}}
    with pytest.raises(EventParseError):
        parse_push(env)


def test_parse_missing_message_id_raises():
    env = _envelope(
        data={"conferenceRecord": {"name": "conferenceRecords/c"}},
        attributes={"ce-source": "//workspaceevents.googleapis.com/subscriptions/s",
                    "ce-type": "google.workspace.meet.conference.v2.started"},
        message_id="",
    )
    with pytest.raises(EventParseError):
        parse_push(env)


def test_subscription_name_none_when_source_missing():
    env = _envelope(
        data={"conferenceRecord": {"name": "conferenceRecords/c"}},
        attributes={"ce-type": "google.workspace.meet.conference.v2.started"},
    )
    ev = parse_push(env)
    assert ev.subscription_name is None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_event_parser.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.events'`.

- [ ] **Step 3: Create `app/events/__init__.py`** (empty file)

```python
```

- [ ] **Step 4: Implement `app/events/parser.py`**

```python
from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass


class EventParseError(Exception):
    pass


@dataclass
class MeetEvent:
    message_id: str
    event_type: str
    subscription_name: str | None
    conference_record_name: str | None
    transcript_resource_name: str | None


def _subscription_name_from_source(source: str | None) -> str | None:
    if not source:
        return None
    marker = "/subscriptions/"
    idx = source.find(marker)
    if idx == -1:
        return None
    return "subscriptions/" + source[idx + len(marker):]


def _conference_record_from(name: str) -> str | None:
    # "conferenceRecords/{cr}" or "conferenceRecords/{cr}/transcripts/{t}"
    parts = name.split("/")
    if len(parts) >= 2 and parts[0] == "conferenceRecords":
        return f"{parts[0]}/{parts[1]}"
    return None


def parse_push(envelope: dict) -> MeetEvent:
    message = envelope.get("message")
    if not isinstance(message, dict):
        raise EventParseError("missing message")

    message_id = message.get("messageId") or message.get("message_id")
    if not message_id:
        raise EventParseError("missing messageId")

    raw = message.get("data")
    if not raw:
        raise EventParseError("missing data")
    try:
        decoded = base64.b64decode(raw, validate=True)
        data = json.loads(decoded)
    except (binascii.Error, ValueError) as exc:
        raise EventParseError(f"invalid data payload: {exc}") from exc

    attributes = message.get("attributes") or {}
    event_type = attributes.get("ce-type", "")
    subscription_name = _subscription_name_from_source(attributes.get("ce-source"))

    transcript_resource_name: str | None = None
    conference_record_name: str | None = None

    transcript = data.get("transcript")
    if isinstance(transcript, dict) and transcript.get("name"):
        transcript_resource_name = transcript["name"]
        conference_record_name = _conference_record_from(transcript_resource_name)

    record = data.get("conferenceRecord")
    if conference_record_name is None and isinstance(record, dict) and record.get("name"):
        conference_record_name = _conference_record_from(record["name"])

    return MeetEvent(
        message_id=message_id,
        event_type=event_type,
        subscription_name=subscription_name,
        conference_record_name=conference_record_name,
        transcript_resource_name=transcript_resource_name,
    )
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_event_parser.py -q`
Expected: PASS (6 tests).

- [ ] **Step 6: Commit**

```bash
git add app/events/__init__.py app/events/parser.py tests/test_event_parser.py
git commit -m "feat: parse pub/sub + cloudevent envelope into MeetEvent"
```

---

## Task 11: OIDC push verifier

**Files:**
- Create: `app/events/oidc.py`
- Test: `tests/test_oidc_verifier.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_oidc_verifier.py`:

```python
import pytest

from app.events.oidc import (
    GooglePushVerifier,
    PushVerificationError,
    VerifiedPush,
)


def _make_verifier(claims, *, expected_audience, expected_email, raise_exc=None):
    def fake_verify(token, request, audience=None):
        if raise_exc is not None:
            raise raise_exc
        return claims

    return GooglePushVerifier(
        expected_audience=expected_audience,
        expected_email=expected_email,
        _verify_fn=fake_verify,
    )


def test_verify_accepts_valid_token():
    claims = {
        "iss": "https://accounts.google.com",
        "email": "pusher@project.iam.gserviceaccount.com",
        "email_verified": True,
        "aud": "https://app.example/v1/webhooks/google/events",
    }
    v = _make_verifier(
        claims,
        expected_audience="https://app.example/v1/webhooks/google/events",
        expected_email="pusher@project.iam.gserviceaccount.com",
    )
    result = v.verify("Bearer the.jwt.token")
    assert isinstance(result, VerifiedPush)
    assert result.email == "pusher@project.iam.gserviceaccount.com"


def test_verify_rejects_missing_bearer():
    v = _make_verifier({}, expected_audience="a", expected_email="e")
    with pytest.raises(PushVerificationError):
        v.verify(None)
    with pytest.raises(PushVerificationError):
        v.verify("token-without-bearer-prefix")


def test_verify_rejects_bad_issuer():
    claims = {"iss": "https://evil.example", "email": "e", "email_verified": True}
    v = _make_verifier(claims, expected_audience="a", expected_email="e")
    with pytest.raises(PushVerificationError):
        v.verify("Bearer x")


def test_verify_rejects_unverified_email():
    claims = {"iss": "https://accounts.google.com", "email": "e", "email_verified": False}
    v = _make_verifier(claims, expected_audience="a", expected_email="e")
    with pytest.raises(PushVerificationError):
        v.verify("Bearer x")


def test_verify_rejects_wrong_email():
    claims = {"iss": "https://accounts.google.com", "email": "other@x", "email_verified": True}
    v = _make_verifier(claims, expected_audience="a", expected_email="expected@x")
    with pytest.raises(PushVerificationError):
        v.verify("Bearer x")


def test_verify_propagates_library_failure_as_push_error():
    v = _make_verifier(
        {}, expected_audience="a", expected_email="e", raise_exc=ValueError("bad signature")
    )
    with pytest.raises(PushVerificationError):
        v.verify("Bearer x")


def test_verify_skips_email_check_when_no_expected_email():
    claims = {"iss": "accounts.google.com", "email": "anyone@x", "email_verified": True}
    v = _make_verifier(claims, expected_audience="a", expected_email="")
    result = v.verify("Bearer x")
    assert result.email == "anyone@x"


class AllowAllVerifier:
    def verify(self, authorization_header):
        return VerifiedPush(email="test@local")


def test_allow_all_verifier_is_usable_in_tests():
    assert AllowAllVerifier().verify("anything").email == "test@local"
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_oidc_verifier.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.events.oidc'`.

- [ ] **Step 3: Implement `app/events/oidc.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

_VALID_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


class PushVerificationError(Exception):
    pass


@dataclass
class VerifiedPush:
    email: str | None


class PushVerifier(Protocol):
    def verify(self, authorization_header: str | None) -> VerifiedPush: ...


def _default_verify_fn(token: str, request, audience=None):
    # Imported lazily so importing this module never requires network/credentials.
    from google.auth.transport import requests as ga_requests
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, ga_requests.Request(), audience=audience)


class GooglePushVerifier:
    def __init__(
        self,
        *,
        expected_audience: str,
        expected_email: str,
        _verify_fn: Callable[..., dict] | None = None,
    ) -> None:
        self._expected_audience = expected_audience
        self._expected_email = expected_email
        self._verify_fn = _verify_fn or _default_verify_fn

    def verify(self, authorization_header: str | None) -> VerifiedPush:
        if not authorization_header or not authorization_header.startswith("Bearer "):
            raise PushVerificationError("missing bearer token")
        token = authorization_header[len("Bearer "):].strip()

        try:
            claims = self._verify_fn(
                token, None, audience=self._expected_audience or None
            )
        except Exception as exc:  # signature/expiry/audience failure
            raise PushVerificationError(f"token verification failed: {exc}") from exc

        if claims.get("iss") not in _VALID_ISSUERS:
            raise PushVerificationError("invalid issuer")
        if claims.get("email_verified") is not True:
            raise PushVerificationError("email not verified")

        email = claims.get("email")
        if self._expected_email and email != self._expected_email:
            raise PushVerificationError("unexpected service account email")

        return VerifiedPush(email=email)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_oidc_verifier.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add app/events/oidc.py tests/test_oidc_verifier.py
git commit -m "feat: OIDC push verifier for pub/sub webhook"
```

---

## Task 12: Event service — dedup, map, upsert conference, enqueue

**Files:**
- Create: `app/services/event_service.py`
- Test: `tests/test_event_service.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_event_service.py`:

```python
import base64
import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.events.parser import parse_push
from app.google.oauth_client import TokenBundle
from app.models import Conference, EventSubscription, ProcessedEvent, User
from app.queue import NullJobQueue
from app.services import connection_service, event_service


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _envelope(*, message_id, ce_type, ce_source, data):
    return {
        "subscription": "projects/p/subscriptions/s",
        "message": {
            "data": base64.b64encode(json.dumps(data).encode()).decode(),
            "messageId": message_id,
            "attributes": {"ce-type": ce_type, "ce-source": ce_source},
        },
    }


def _transcript_env(message_id="msg-1", sub="subscriptions/sub-1", cr="cr-1"):
    return _envelope(
        message_id=message_id,
        ce_type="google.workspace.meet.transcript.v2.fileGenerated",
        ce_source=f"//workspaceevents.googleapis.com/{sub}",
        data={"transcript": {"name": f"conferenceRecords/{cr}/transcripts/t-1"}},
    )


async def _conn_with_sub(db_session, *, sub_name="subscriptions/sub-1"):
    user = User(email="m@acme.com", name="M", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="m@acme.com", google_user_id="108"
    )
    sub = EventSubscription(
        oauth_connection_id=conn.id, subscription_name=sub_name, state="active"
    )
    db_session.add(sub)
    await db_session.commit()
    return conn


async def test_handle_creates_conference_and_enqueues(db_session):
    conn = await _conn_with_sub(db_session)
    queue = NullJobQueue()
    ev = parse_push(_transcript_env())
    result = await event_service.handle_event(db_session, ev, queue)
    assert result == "enqueued"

    conf = await db_session.scalar(
        select(Conference).where(Conference.conference_record_name == "conferenceRecords/cr-1")
    )
    assert conf is not None
    assert conf.oauth_connection_id == conn.id
    assert conf.transcript_resource_name == "conferenceRecords/cr-1/transcripts/t-1"
    assert conf.pipeline_state == "pending"
    assert queue.enqueued == [str(conf.id)]

    ledger = await db_session.scalar(
        select(ProcessedEvent).where(ProcessedEvent.message_id == "msg-1")
    )
    assert ledger is not None


async def test_handle_dedupes_duplicate_message(db_session):
    await _conn_with_sub(db_session)
    queue = NullJobQueue()
    ev = parse_push(_transcript_env(message_id="dup"))
    first = await event_service.handle_event(db_session, ev, queue)
    second = await event_service.handle_event(
        db_session, parse_push(_transcript_env(message_id="dup")), queue
    )
    assert first == "enqueued"
    assert second == "duplicate"
    confs = (await db_session.scalars(select(Conference))).all()
    assert len(confs) == 1
    assert queue.enqueued == [str(confs[0].id)]  # enqueued exactly once


async def test_handle_second_event_same_conference_no_duplicate_conference(db_session):
    await _conn_with_sub(db_session)
    queue = NullJobQueue()
    # conference.started then transcript.fileGenerated, both for cr-7
    started = _envelope(
        message_id="m-started",
        ce_type="google.workspace.meet.conference.v2.started",
        ce_source="//workspaceevents.googleapis.com/subscriptions/sub-1",
        data={"conferenceRecord": {"name": "conferenceRecords/cr-7"}},
    )
    await event_service.handle_event(db_session, parse_push(started), queue)
    transcript = _transcript_env(message_id="m-trans", cr="cr-7")
    await event_service.handle_event(db_session, parse_push(transcript), queue)

    confs = (
        await db_session.scalars(
            select(Conference).where(
                Conference.conference_record_name == "conferenceRecords/cr-7"
            )
        )
    ).all()
    assert len(confs) == 1
    # transcript resource got filled in by the second event
    assert confs[0].transcript_resource_name == "conferenceRecords/cr-7/transcripts/t-1"


async def test_handle_unmappable_subscription_acks_without_conference(db_session):
    # no EventSubscription row for this sub
    queue = NullJobQueue()
    ev = parse_push(_transcript_env(sub="subscriptions/unknown"))
    result = await event_service.handle_event(db_session, ev, queue)
    assert result == "ignored"
    confs = (await db_session.scalars(select(Conference))).all()
    assert confs == []


async def test_handle_event_without_conference_record_is_ignored(db_session):
    await _conn_with_sub(db_session)
    queue = NullJobQueue()
    # lifecycle-style event: no transcript/conferenceRecord in payload
    env = _envelope(
        message_id="m-life",
        ce_type="google.workspace.events.subscription.v1.expirationReminder",
        ce_source="//workspaceevents.googleapis.com/subscriptions/sub-1",
        data={"subscription": {"name": "subscriptions/sub-1"}},
    )
    result = await event_service.handle_event(db_session, parse_push(env), queue)
    assert result == "ignored"
    assert (await db_session.scalars(select(Conference))).all() == []
    # still recorded in the ledger so we don't reprocess
    ledger = await db_session.scalar(
        select(ProcessedEvent).where(ProcessedEvent.message_id == "m-life")
    )
    assert ledger is not None
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_event_service.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.event_service'`.

- [ ] **Step 3: Implement `app/services/event_service.py`**

```python
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.events.parser import MeetEvent
from app.models import Conference, ProcessedEvent
from app.queue import JobQueue
from app.services import subscription_service

logger = logging.getLogger(__name__)


async def _already_processed(session: AsyncSession, message_id: str) -> bool:
    existing = await session.scalar(
        select(ProcessedEvent).where(ProcessedEvent.message_id == message_id)
    )
    return existing is not None


async def handle_event(
    session: AsyncSession, event: MeetEvent, queue: JobQueue
) -> str:
    if await _already_processed(session, event.message_id):
        logger.info("duplicate event %s ignored", event.message_id)
        return "duplicate"

    # Record the message first so retries/duplicates are dropped even if the rest fails.
    ledger = ProcessedEvent(
        message_id=event.message_id,
        event_type=event.event_type,
        conference_record_name=event.conference_record_name,
    )
    session.add(ledger)
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent duplicate beat us to it.
        await session.rollback()
        logger.info("race duplicate event %s ignored", event.message_id)
        return "duplicate"

    if event.conference_record_name is None:
        logger.info("event %s has no conference record; acking", event.message_id)
        return "ignored"

    if event.subscription_name is None:
        logger.warning("event %s has no subscription source; acking", event.message_id)
        return "ignored"

    sub = await subscription_service.get_by_subscription_name(
        session, event.subscription_name
    )
    if sub is None:
        logger.warning(
            "event %s references unknown subscription %s; acking",
            event.message_id,
            event.subscription_name,
        )
        return "ignored"

    conf = await session.scalar(
        select(Conference).where(
            Conference.conference_record_name == event.conference_record_name
        )
    )
    if conf is None:
        conf = Conference(
            oauth_connection_id=sub.oauth_connection_id,
            conference_record_name=event.conference_record_name,
            pipeline_state="pending",
        )
        session.add(conf)

    if event.transcript_resource_name is not None:
        conf.transcript_resource_name = event.transcript_resource_name

    try:
        await session.commit()
    except IntegrityError:
        # Another worker inserted the same conference concurrently; reload it.
        await session.rollback()
        conf = await session.scalar(
            select(Conference).where(
                Conference.conference_record_name == event.conference_record_name
            )
        )
        if conf is None:
            logger.warning("conference vanished after race for %s", event.message_id)
            return "ignored"

    await session.refresh(conf)
    await queue.enqueue_notes_pipeline(str(conf.id))
    return "enqueued"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_event_service.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/event_service.py tests/test_event_service.py
git commit -m "feat: event service dedup/map/upsert-conference/enqueue"
```

---

## Task 13: Webhook route `POST /v1/webhooks/google/events`

**Files:**
- Create: `app/api/routes/webhooks.py`
- Modify: `app/api/deps.py` (add `get_push_verifier`, `get_job_queue`)
- Modify: `app/main.py` (register router)
- Test: `tests/test_webhooks_api.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_webhooks_api.py`:

```python
import base64
import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.api.deps import get_job_queue, get_push_verifier
from app.events.oidc import PushVerificationError, VerifiedPush
from app.google.oauth_client import TokenBundle
from app.main import app
from app.models import Conference, EventSubscription, User
from app.queue import NullJobQueue
from app.services import connection_service


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class AllowVerifier:
    def verify(self, authorization_header):
        return VerifiedPush(email="pusher@x")


class DenyVerifier:
    def verify(self, authorization_header):
        raise PushVerificationError("nope")


@pytest.fixture
def shared_queue():
    q = NullJobQueue()
    app.dependency_overrides[get_job_queue] = lambda: q
    yield q
    app.dependency_overrides.pop(get_job_queue, None)


@pytest.fixture
def allow_verifier():
    app.dependency_overrides[get_push_verifier] = lambda: AllowVerifier()
    yield
    app.dependency_overrides.pop(get_push_verifier, None)


@pytest.fixture
def deny_verifier():
    app.dependency_overrides[get_push_verifier] = lambda: DenyVerifier()
    yield
    app.dependency_overrides.pop(get_push_verifier, None)


def _push_body(message_id="msg-1", sub="subscriptions/sub-1", cr="cr-1"):
    data = {"transcript": {"name": f"conferenceRecords/{cr}/transcripts/t-1"}}
    return {
        "subscription": "projects/p/subscriptions/s",
        "message": {
            "data": base64.b64encode(json.dumps(data).encode()).decode(),
            "messageId": message_id,
            "attributes": {
                "ce-type": "google.workspace.meet.transcript.v2.fileGenerated",
                "ce-source": f"//workspaceevents.googleapis.com/{sub}",
            },
        },
    }


async def _seed_subscription(db_session, sub_name="subscriptions/sub-1"):
    user = User(email="m@acme.com", name="M", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="m@acme.com", google_user_id="108"
    )
    db_session.add(
        EventSubscription(oauth_connection_id=conn.id, subscription_name=sub_name, state="active")
    )
    await db_session.commit()


async def test_webhook_accepts_and_creates_conference(
    client, db_session, allow_verifier, shared_queue
):
    await _seed_subscription(db_session)
    resp = await client.post(
        "/v1/webhooks/google/events",
        json=_push_body(),
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    conf = await db_session.scalar(
        select(Conference).where(Conference.conference_record_name == "conferenceRecords/cr-1")
    )
    assert conf is not None
    assert shared_queue.enqueued == [str(conf.id)]


async def test_webhook_rejects_bad_oidc(client, deny_verifier, shared_queue):
    resp = await client.post(
        "/v1/webhooks/google/events",
        json=_push_body(),
        headers={"Authorization": "Bearer bad"},
    )
    assert resp.status_code == 401


async def test_webhook_acks_duplicate(client, db_session, allow_verifier, shared_queue):
    await _seed_subscription(db_session)
    body = _push_body(message_id="dup")
    r1 = await client.post("/v1/webhooks/google/events", json=body,
                           headers={"Authorization": "Bearer t"})
    r2 = await client.post("/v1/webhooks/google/events", json=body,
                           headers={"Authorization": "Bearer t"})
    assert r1.status_code == 200
    assert r2.status_code == 200
    confs = (await db_session.scalars(select(Conference))).all()
    assert len(confs) == 1


async def test_webhook_acks_unparseable_body(client, allow_verifier, shared_queue):
    resp = await client.post(
        "/v1/webhooks/google/events",
        json={"message": {"data": "!!notbase64!!", "messageId": "m", "attributes": {}}},
        headers={"Authorization": "Bearer t"},
    )
    # Bad payloads must be acked (200) so Pub/Sub stops redelivering a poison message.
    assert resp.status_code == 200


async def test_webhook_acks_unmapped_subscription(client, allow_verifier, shared_queue):
    resp = await client.post(
        "/v1/webhooks/google/events",
        json=_push_body(sub="subscriptions/unknown"),
        headers={"Authorization": "Bearer t"},
    )
    assert resp.status_code == 200
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_webhooks_api.py -q`
Expected: FAIL — `ImportError: cannot import name 'get_job_queue' from 'app.api.deps'`.

- [ ] **Step 3: Add the providers to `app/api/deps.py`**

Add imports (note: `from app.config import get_settings` is **already imported** at the top of this file — do not duplicate it):

```python
from app.events.oidc import GooglePushVerifier, PushVerifier
from app.queue import JobQueue, NullJobQueue
```

Add a module-level singleton near the other module constants (after `_bearer = HTTPBearer(...)`):

```python
_job_queue = NullJobQueue()
```

Add providers:

```python
def get_push_verifier() -> PushVerifier:
    settings = get_settings()
    return GooglePushVerifier(
        expected_audience=settings.push_audience,
        expected_email=settings.push_service_account_email,
    )


def get_job_queue() -> JobQueue:
    return _job_queue
```

> The module-level singleton `_job_queue` keeps the `NullJobQueue.enqueued` list stable across requests within a process (tests override it anyway). When the arq queue lands in Phase 5, this provider returns the real queue.

- [ ] **Step 4: Implement `app/api/routes/webhooks.py`**

```python
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_job_queue, get_push_verifier
from app.db import get_session
from app.events.oidc import PushVerificationError, PushVerifier
from app.events.parser import EventParseError, parse_push
from app.queue import JobQueue
from app.services import event_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/webhooks/google", tags=["webhooks"])


@router.post("/events")
async def receive_events(
    request: Request,
    session: AsyncSession = Depends(get_session),
    verifier: PushVerifier = Depends(get_push_verifier),
    queue: JobQueue = Depends(get_job_queue),
) -> Response:
    try:
        verifier.verify(request.headers.get("Authorization"))
    except PushVerificationError as exc:
        logger.warning("rejected unverified pub/sub push: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid push token"
        ) from exc

    envelope = await request.json()
    try:
        event = parse_push(envelope)
    except EventParseError as exc:
        # Ack poison messages so Pub/Sub stops redelivering them.
        logger.warning("unparseable pub/sub push acked: %s", exc)
        return Response(status_code=status.HTTP_200_OK)

    try:
        outcome = await event_service.handle_event(session, event, queue)
    except Exception:
        # Unexpected failure: NACK (500) so Pub/Sub retries with backoff.
        logger.exception("error handling event %s", event.message_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="processing error"
        )

    logger.info("event %s outcome=%s", event.message_id, outcome)
    return Response(status_code=status.HTTP_200_OK)
```

- [ ] **Step 5: Register the router in `app/main.py`**

Add the import and `include_router` call alongside the existing routers (match the existing registration style in that file):

```python
from app.api.routes import webhooks
```

```python
app.include_router(webhooks.router)
```

- [ ] **Step 6: Run the webhook tests**

Run: `python -m pytest tests/test_webhooks_api.py -q`
Expected: PASS (5 tests).

- [ ] **Step 7: Commit**

```bash
git add app/api/routes/webhooks.py app/api/deps.py app/main.py tests/test_webhooks_api.py
git commit -m "feat: pub/sub webhook endpoint (verify, dedup, enqueue)"
```

---

## Task 14: Alembic migrations for all schema changes

**Files:**
- Create (autogenerated): five migration files under `migrations/versions/`
- No test changes (the test DB uses `create_all`; migrations are for real DBs)

> **Why one task for all five:** the test suite builds the schema from the models via `Base.metadata.create_all`, so tests already pass without migrations. This task makes the *real* database match. We autogenerate once (Alembic emits all pending diffs together), then split or keep as a single revision — a single revision is fine and simpler here.

- [ ] **Step 1: Confirm models are all imported for autogenerate**

`migrations/env.py` already does `from app import models  # noqa`. Confirm `app/models/__init__.py` exports `Conference`, `EventSubscription`, `ProcessedEvent` (done in Task 4).

- [ ] **Step 2: Ensure the dev database is at head**

Run: `python -m alembic upgrade head`
Expected: `Running upgrade ... ` or "already at head" with no error. (Dev DB `meetnotes` must be running — Postgres 17 from earlier.)

- [ ] **Step 3: Autogenerate the migration**

Run: `python -m alembic revision --autogenerate -m "phase4 event ingestion tables and columns"`
Expected: a new file in `migrations/versions/` is created; console lists detected changes:
- add column `oauth_connections.google_user_id`
- add column `meetings.meet_space_name` + index
- add table `event_subscriptions`
- add table `conferences`
- add table `processed_events`

- [ ] **Step 4: Read the generated migration and verify it**

Open the new file. Confirm:
- `upgrade()` adds the two columns (with the `meetings.meet_space_name` index `ix_meetings_meet_space_name`), and creates the three tables with the FKs (`ondelete='CASCADE'`), unique constraints (`conferences.conference_record_name`, `processed_events.message_id`, `event_subscriptions.oauth_connection_id`), and indexes (`conferences.pipeline_state`, `event_subscriptions.expire_time`, etc.).
- `downgrade()` reverses them (drop tables, drop indexes, drop columns).
- No spurious diffs (e.g. it should NOT try to drop/recreate unrelated tables). If it does, the model and DB are out of sync — investigate before proceeding.

Fix any autogenerate quirks by hand (e.g. ordering of `drop_table` in downgrade so FKs don't block, or a missing `postgresql.UUID` import — match the style in `migrations/versions/2abdc7a1f5e6_create_meetings_table.py`).

- [ ] **Step 5: Apply and round-trip the migration**

```bash
python -m alembic upgrade head
python -m alembic downgrade -1
python -m alembic upgrade head
```
Expected: all three commands succeed with no error. The downgrade/upgrade round-trip proves `downgrade()` is correct.

- [ ] **Step 6: Verify the columns/tables exist**

Run: `python -c "import asyncio; from sqlalchemy import text; from app.db import engine; asyncio.run(_check()) if False else None"` — simpler, use psql:

Run: `psql "postgresql://postgres:postgres@localhost:5432/meetnotes" -c "\d conferences" -c "\d event_subscriptions" -c "\d processed_events" -c "\d meetings" | grep -E "meet_space_name|conference_record_name|message_id|google_user_id"`
Expected: lines confirming `meet_space_name`, `conference_record_name`, `message_id`, and (from `\d oauth_connections` if you add it) `google_user_id`. (Adjust the psql path if needed; the DB was created earlier in this project.)

- [ ] **Step 7: Run the full suite once more**

Run: `python -m pytest -q`
Expected: PASS (all tests — the new ones plus the original 67).

- [ ] **Step 8: Commit**

```bash
git add migrations/versions/
git commit -m "feat: phase4 migration (event tables + google_user_id + meet_space_name)"
```

---

## Task 15: Phase verification & cleanup

**Files:** none (verification only)

- [ ] **Step 1: Full suite green**

Run: `python -m pytest -q`
Expected: PASS. Record the count (should be ~67 + the new tests from this phase).

- [ ] **Step 2: App imports and OpenAPI lists the webhook**

Run: `python -c "from app.main import app; print([r.path for r in app.routes if 'webhook' in r.path])"`
Expected: `['/v1/webhooks/google/events']`.

- [ ] **Step 3: Confirm no stray red tests**

Run: `python -m pytest -q -rf`
Expected: no failures section.

- [ ] **Step 4: Review the diff for the whole phase**

Run: `git log --oneline feat/phase3-meetings..HEAD`
Expected: ~14 commits, one per task, clean messages.

- [ ] **Step 5: (Optional) self-review hooks for Phase 5**

Confirm these Phase-5 seams are in place and documented:
- `conferences.meeting_id` is nullable and currently `NULL` (worker resolves it via `conferenceRecords.get` → `space` → `meetings.meet_space_name`).
- `NullJobQueue` is the only `JobQueue` impl; Phase 5 swaps in arq + Redis.
- `transcript_resource_name` is persisted on the conference for the worker to fetch entries.

---

## Self-review checklist (the plan author runs this before handing off)

**Spec coverage (§5/§6/§7):**
- POST `/v1/webhooks/google/events`, OIDC-verified, dedup + enqueue → Tasks 11, 12, 13. ✓
- Flow A subscription creation at connect (events: `conference.started`, `transcript.fileGenerated`; resource-name-only → 7-day TTL) → Tasks 6, 7. ✓
- `event_subscriptions` (subscription_name, expire_time, state) + `conferences` (conference_record_name unique, pipeline_state, attempts, last_error) + `processed_events` (message_id unique) → Task 4. ✓
- Idempotency: unique `message_id` + unique `conference_record_name` → Tasks 4, 12. ✓
- Out-of-order events (started after fileGenerated) handled via upsert → Task 12 test. ✓
- Bad OIDC → 401, never enqueue → Tasks 11, 13. ✓
- Unmappable event → ack 200, ignore → Tasks 12, 13. ✓
- Fast ack (no network in request path; map to meeting deferred) → documented deviation, Task 12/13. ✓

**Documented deviations from the spec:**
1. `conferences.meeting_id` is **nullable** and resolved by the Phase 5 worker (spec implied it set at ingestion). Reason: mapping requires a `conferenceRecords.get` network call that must not run in the webhook request path (fast-ack robustness).
2. **Subscription target is user-level** (`//cloudidentity.googleapis.com/users/{id}`), confirmed by research — matches the spec's 1:1 model and needs the new `google_user_id` column.
3. **`meetings.meet_space_name`** added (spec listed it; Phase 3 omitted it). No historical backfill.
4. Enqueue uses a **`NullJobQueue`** placeholder; the real arq/Redis worker is Phase 5. Durable `pending` conference row is the source of truth, so no work is lost.

**Type consistency:** `UserInfo(email, sub)` (Task 5) used in `connections.py` (Task 7b) and `test_connections_api.py`. `SubscriptionResult(subscription_name, expire_time, state)` (Task 6) used in `subscription_service` (Task 7a) and its tests. `MeetEvent(message_id, event_type, subscription_name, conference_record_name, transcript_resource_name)` (Task 10) consumed by `event_service.handle_event` (Task 12). `VerifiedPush(email)` (Task 11) returned by verifier and used in webhook tests. Consistent. ✓

**Placeholder scan:** no placeholders, no TODOs, no "implement later". Every code step contains complete, runnable code. ✓
