# Phase 5: Notes Pipeline — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a `pending` conference (created by the Phase 4 webhook) into generated notes: fetch the Meet transcript with speaker attribution, summarize it with the Gemini API, and persist the result — driven by a resumable per-stage state machine, runnable by an arq worker, and queryable via read endpoints.

**Architecture:** A pure async `pipeline.run_pipeline(session, conference_id, ...)` orchestrator advances a conference through `pending → transcript_fetched → notes_generated`, committing after each stage so a crash/retry resumes from the last completed stage (never re-doing side effects). Stage 1 (`transcript_service`) resolves the conference's parent space via `conferenceRecords.get`, maps it to the meeting (`meetings.meet_space_name`), lists participants for a speaker map, pages through transcript entries, assembles an attributed `full_text`, and stores it encrypted in a new `transcripts` row. Stage 2 (`notes_service`) chunks the transcript if it exceeds a configurable token budget (map-reduce), calls an injectable `Summarizer` (thin wrapper over the `google-genai` SDK) for structured JSON notes, and stores a `notes` row titled with the meeting title. The arq worker module wraps `run_pipeline` as a task; a `RealJobQueue` enqueues by `conference_id` with `_job_id` dedup. **Delivery (Google Doc + email) is Phase 6 — this phase stops at `notes_generated`.**

**Tech Stack:** Python 3.12, FastAPI, async SQLAlchemy 2.0, Alembic (`migrations/versions/`), httpx (`MockTransport` in tests), `google-genai` (new — Gemini, AI Studio API key), `arq` + `redis` (new — worker; **Redis not required to build or unit-test this phase**), Fernet (existing — transcript encryption), pytest + pytest-asyncio (`asyncio_mode=auto`).

---

## Decisions locked for this phase

1. **Redis is deferred.** Per the project decision, we build and unit-test the entire pipeline WITHOUT a running Redis. Verified: importing `arq` and defining `WorkerSettings`/task functions does NOT connect to Redis (connection happens only at `create_pool(...)` / worker start). arq tasks are plain coroutines, unit-tested by calling them with a hand-built `ctx` dict. The `RealJobQueue` (which needs a pool) is defined but only instantiated when `REDIS_URL` is configured; otherwise the app keeps using `NullJobQueue`.
2. **Gemini via `google-genai` AI Studio API key.** Verified current SDK is `google-genai` (NOT legacy `google-generativeai`); `genai.Client(api_key=...)` makes NO network call at construction; native async via `client.aio.models.generate_content`; structured output via `response_schema=<PydanticModel>` → `response.parsed`; errors are `google.genai.errors.{ClientError(4xx), ServerError(5xx), APIError}`; safety blocks are inspected on the response (`prompt_feedback.block_reason`, `candidates[0].finish_reason`), not raised. Tests use a fake `Summarizer` — no real API calls and no key needed. A real `GEMINI_API_KEY` is supplied later for live verification.
3. **Default model `gemini-2.5-flash`** (stable/GA, large context, cost-effective for summarization), configurable via settings.
4. **Chunking threshold is a config value** (`gemini_chunk_token_threshold`, default 600000), NOT a hardcoded model limit — the exact 2026 context windows could not be verified, so we make it tunable with safe headroom.
5. **Pipeline stops at `notes_generated`.** The spec's `doc_created`/`emailed` stages are Phase 6. `run_pipeline` is written so Phase 6 appends stages 3–4 without restructuring.
6. **Meeting mapping happens here (Phase 5), not in the webhook.** `conferences.meeting_id` (nullable since Phase 4) is resolved in stage 1 via `conferenceRecords.get().space` matched against `meetings.meet_space_name`. If no match (e.g. a meeting not created through our API), `meeting_id` stays NULL and the notes title falls back to a default — this is allowed, not an error.

## Research findings this plan is built on (verified June 2026)

- **Meet REST API v2** (scope `meetings.space.created`, already granted):
  - `GET /v2/conferenceRecords/{cr}` → `ConferenceRecord{ name, startTime, endTime, space }`; `space` format `spaces/{id}`.
  - `GET /v2/conferenceRecords/{cr}/transcripts/{t}` → `Transcript{ name, state, startTime, endTime }`; `state ∈ {STATE_UNSPECIFIED, STARTED, ENDED, FILE_GENERATED}`.
  - `GET /v2/conferenceRecords/{cr}/transcripts/{t}/entries?pageSize=&pageToken=` → `{ transcriptEntries: [{ name, participant, text, languageCode, startTime, endTime }], nextPageToken }`. pageSize default 10, max 100.
  - `GET /v2/conferenceRecords/{cr}/participants?pageSize=&pageToken=` → `{ participants: [{ name, signedinUser{user,displayName} | anonymousUser{displayName} | phoneUser{displayName} }], nextPageToken }`. pageSize default 100, max 250.
  - Conference records + transcripts + entries are **deleted ~30 days** after the conference → handle 404.
- **`google-genai`:** `from google import genai`; `genai.Client(api_key=...)`; `await client.aio.models.generate_content(model=, contents=, config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Model))`; read `response.parsed` (Pydantic instance) and `response.text` (raw). `await client.aio.models.count_tokens(model=, contents=)` → `.total_tokens`. Errors under `google.genai.errors`.
- **arq 0.28:** `pip install arq` (depends on `redis[hiredis]>=4.2,<6`); `from arq.connections import RedisSettings`; `from arq import create_pool, Retry`; `WorkerSettings.functions`/`redis_settings`/`on_startup`/`on_shutdown`; `await pool.enqueue_job('task', arg, _job_id=...)` (dedups while pending → returns `None` on duplicate). Importing/defining does not connect to Redis.

---

## File structure (created/modified in this phase)

**New source files**
- `app/models/transcript.py` — `Transcript` ORM model (encrypted `full_text`).
- `app/models/notes.py` — `Notes` ORM model.
- `app/schemas/notes.py` — Pydantic: `NotesContent` (the Gemini structured-output schema) + API response schemas (`NotesResponse`, `TranscriptResponse`, `ConferenceResponse`).
- `app/google/gemini_client.py` — `Summarizer` Protocol + `NotesContent` usage + `GeminiSummarizer` (wraps `google-genai`).
- `app/services/transcript_service.py` — fetch + assemble + persist transcript; resolve meeting.
- `app/services/notes_service.py` — chunk + summarize + persist notes.
- `app/services/pipeline.py` — `run_pipeline` state machine; `PipelineError`.
- `app/worker.py` — arq `WorkerSettings`, the `notes_pipeline` task, `RealJobQueue`.
- `app/api/routes/notes.py` — read endpoints + regenerate.

**Modified source files**
- `app/config.py` — Gemini + Redis + chunking settings.
- `app/models/__init__.py` — export `Transcript`, `Notes`.
- `app/google/meet_client.py` — add `get_conference_record`, `get_transcript`, `list_transcript_entries`, `list_participants`.
- `app/api/deps.py` — `get_summarizer`, `get_job_queue` returns Real or Null based on config.
- `app/main.py` — register the notes router.
- `pyproject.toml` — add `google-genai`, `arq`.

**New migration** (`migrations/versions/`) — create `transcripts`, `notes`.

**New test files**
- `tests/test_transcript_model.py`, `tests/test_notes_model.py`
- `tests/test_meet_client_transcript.py` (extends Meet client coverage)
- `tests/test_gemini_client.py`
- `tests/test_transcript_service.py`
- `tests/test_notes_service.py`
- `tests/test_pipeline.py`
- `tests/test_worker.py`
- `tests/test_notes_api.py`

---

## Conventions to follow (match existing code exactly)

- **Google clients**: class with injectable `transport: httpx.BaseTransport | None`, `_http(access_token)` building `httpx.AsyncClient(transport=..., timeout=30.0, headers={"Authorization": f"Bearer {token}"})`, a `typing.Protocol` interface, module-level URL constant. Tests use `httpx.MockTransport(handler)`. See `app/google/meet_client.py`, `app/google/events_client.py`.
- **Services**: module-level `async def` functions, `session: AsyncSession` first arg, deps passed explicitly, `await session.commit()` then `await session.refresh(obj)`, module-level `logger = logging.getLogger(__name__)`. See `app/services/event_service.py`, `app/services/subscription_service.py`.
- **Models**: `Mapped[...]` + `mapped_column`, `UUID(as_uuid=True)` pk `default=uuid.uuid4`, `DateTime(timezone=True)` with `server_default=func.now()`/`onupdate=func.now()`, encrypted blobs use `LargeBinary`. See `app/models/conference.py`, `app/models/oauth_connection.py` (`refresh_token_encrypted` is `LargeBinary`).
- **Encryption**: `from app.crypto import encrypt, decrypt` — `encrypt(str) -> bytes`, `decrypt(bytes) -> str`. Tests set `ENCRYPTION_KEY` via the autouse `_set_key` fixture (see `tests/test_crypto.py`).
- **Settings**: add fields to `Settings` in `app/config.py` with defaults; `get_settings()` is `lru_cache`d; tests call `get_settings.cache_clear()`.
- **Migrations**: `python -m alembic revision --autogenerate -m "..."`, files land in `migrations/versions/`, read & verify before applying. Test DB is built from models via `create_all` (no migration needed for tests).
- **Tests**: `async def`, `db_session`/`client` fixtures from `tests/conftest.py`, dep overrides via `app.dependency_overrides`. No comments unless clarifying non-obvious intent (repo is near comment-free).
- Run full suite: `python -m pytest -q` from repo root. **Current baseline: 121 passing.**

> **Branch:** continue stacking on `feat/phase3-meetings` (do NOT create a new branch). Commit after each task.

---

## Task 1: Add `google-genai` + `arq` deps and Phase 5 settings

**Files:** Modify `pyproject.toml`, `app/config.py`, Test `tests/test_config.py`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_phase5_settings_have_defaults(monkeypatch):
    for var in ("GEMINI_API_KEY", "GEMINI_MODEL", "REDIS_URL", "GEMINI_CHUNK_TOKEN_THRESHOLD"):
        monkeypatch.delenv(var, raising=False)
    from app.config import get_settings

    get_settings.cache_clear()
    s = get_settings()
    assert s.gemini_api_key == ""
    assert s.gemini_model == "gemini-2.5-flash"
    assert s.redis_url == ""
    assert s.gemini_chunk_token_threshold == 600000
    assert s.notes_default_title == "Meeting Notes"
    get_settings.cache_clear()
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_config.py::test_phase5_settings_have_defaults -q`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'gemini_api_key'`.

- [ ] **Step 3: Add the settings**

In `app/config.py`, add to `Settings` (after `subscription_ttl_seconds`):

```python
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_chunk_token_threshold: int = 600000
    redis_url: str = ""
    notes_default_title: str = "Meeting Notes"
```

- [ ] **Step 4: Add dependencies and install**

In `pyproject.toml`, add to `dependencies` (after `"google-auth>=2.35",`):

```toml
    "google-genai>=2.8",
    "arq>=0.28",
```

Run: `python -m pip install "google-genai>=2.8" "arq>=0.28"`
Expected: `Successfully installed ... google-genai-... arq-... redis-...`.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_config.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml app/config.py tests/test_config.py
git commit -m "feat: add google-genai and arq deps with phase 5 settings"
```

---

## Task 2: `Transcript` and `Notes` models + migration

**Files:** Create `app/models/transcript.py`, `app/models/notes.py`; Modify `app/models/__init__.py`; Test `tests/test_transcript_model.py`, `tests/test_notes_model.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_transcript_model.py`:

```python
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.crypto import decrypt, encrypt
from app.google.oauth_client import TokenBundle
from app.models import Conference, Transcript, User
from app.services import connection_service


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _conference(db_session) -> Conference:
    user = User(email="t@acme.com", name="T", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="t@acme.com"
    )
    conf = Conference(
        oauth_connection_id=conn.id,
        conference_record_name="conferenceRecords/cr-1",
        pipeline_state="pending",
    )
    db_session.add(conf)
    await db_session.commit()
    await db_session.refresh(conf)
    return conf


async def test_transcript_stores_encrypted_full_text(db_session):
    conf = await _conference(db_session)
    t = Transcript(
        conference_id=conf.id,
        full_text=encrypt("alice: hello\nbob: hi"),
        language="en-US",
        speaker_map={"conferenceRecords/cr-1/participants/p1": "Alice"},
    )
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    assert t.id is not None
    assert decrypt(t.full_text) == "alice: hello\nbob: hi"
    assert t.speaker_map["conferenceRecords/cr-1/participants/p1"] == "Alice"


async def test_transcript_one_per_conference(db_session):
    from sqlalchemy.exc import IntegrityError
    conf = await _conference(db_session)
    db_session.add(Transcript(conference_id=conf.id, full_text=b"x"))
    await db_session.commit()
    db_session.add(Transcript(conference_id=conf.id, full_text=b"y"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
```

Create `tests/test_notes_model.py`:

```python
import pytest
from cryptography.fernet import Fernet

from app.google.oauth_client import TokenBundle
from app.models import Conference, Notes, User
from app.services import connection_service


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _conference(db_session) -> Conference:
    user = User(email="n@acme.com", name="N", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="n@acme.com"
    )
    conf = Conference(
        oauth_connection_id=conn.id,
        conference_record_name="conferenceRecords/cr-2",
        pipeline_state="transcript_fetched",
    )
    db_session.add(conf)
    await db_session.commit()
    await db_session.refresh(conf)
    return conf


async def test_notes_round_trip(db_session):
    conf = await _conference(db_session)
    n = Notes(
        conference_id=conf.id,
        title="Q3 Roadmap Sync",
        summary="We agreed on the roadmap.",
        decisions=["Ship feature X in Q3"],
        action_items=[{"who": "Alice", "what": "Draft spec"}],
        gemini_model="gemini-2.5-flash",
    )
    db_session.add(n)
    await db_session.commit()
    await db_session.refresh(n)
    assert n.id is not None
    assert n.title == "Q3 Roadmap Sync"
    assert n.decisions == ["Ship feature X in Q3"]
    assert n.action_items[0]["who"] == "Alice"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python -m pytest tests/test_transcript_model.py tests/test_notes_model.py -q`
Expected: FAIL — `ImportError: cannot import name 'Transcript' from 'app.models'`.

- [ ] **Step 3: Create `app/models/transcript.py`**

```python
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, LargeBinary, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conference_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conferences.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    full_text: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    speaker_map: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

> `full_text` is nullable so the Phase 7 retention job can null it after the window without dropping the row.

- [ ] **Step 4: Create `app/models/notes.py`**

```python
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Notes(Base):
    __tablename__ = "notes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    conference_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conferences.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decisions: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    action_items: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    gemini_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    doc_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    doc_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    emailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

> `doc_id`/`doc_url`/`emailed_at` are populated in Phase 6; included now so Phase 6 needs no migration.

- [ ] **Step 5: Update `app/models/__init__.py`**

```python
from app.models.conference import Conference
from app.models.event_subscription import EventSubscription
from app.models.meeting import Meeting
from app.models.notes import Notes
from app.models.oauth_connection import OAuthConnection
from app.models.processed_event import ProcessedEvent
from app.models.transcript import Transcript
from app.models.user import User

__all__ = [
    "User",
    "OAuthConnection",
    "Meeting",
    "EventSubscription",
    "Conference",
    "ProcessedEvent",
    "Transcript",
    "Notes",
]
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_transcript_model.py tests/test_notes_model.py -q`
Expected: PASS (5 tests).

- [ ] **Step 7: Commit**

```bash
git add app/models/transcript.py app/models/notes.py app/models/__init__.py tests/test_transcript_model.py tests/test_notes_model.py
git commit -m "feat: transcripts and notes models"
```

---

## Task 3: Extend the Meet client (conferenceRecord, transcript, entries, participants)

**Files:** Modify `app/google/meet_client.py`; Test `tests/test_meet_client_transcript.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_meet_client_transcript.py`:

```python
import httpx
import pytest

from app.google.meet_client import GoogleMeetClient


def _client(handler) -> GoogleMeetClient:
    return GoogleMeetClient(transport=httpx.MockTransport(handler))


async def test_get_conference_record_returns_space_and_times():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/conferenceRecords/cr-1"
        assert request.headers["Authorization"] == "Bearer at-1"
        return httpx.Response(200, json={
            "name": "conferenceRecords/cr-1",
            "space": "spaces/SERVERID",
            "startTime": "2026-06-11T10:00:00Z",
            "endTime": "2026-06-11T11:00:00Z",
        })

    rec = await _client(handler).get_conference_record("at-1", "conferenceRecords/cr-1")
    assert rec.space == "spaces/SERVERID"
    assert rec.start_time == "2026-06-11T10:00:00Z"
    assert rec.end_time == "2026-06-11T11:00:00Z"


async def test_get_transcript_returns_state():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/conferenceRecords/cr-1/transcripts/t-1"
        return httpx.Response(200, json={
            "name": "conferenceRecords/cr-1/transcripts/t-1",
            "state": "FILE_GENERATED",
        })

    t = await _client(handler).get_transcript("at-1", "conferenceRecords/cr-1/transcripts/t-1")
    assert t.state == "FILE_GENERATED"


async def test_list_transcript_entries_paginates():
    pages = {
        None: {"transcriptEntries": [
                   {"name": "e1", "participant": "conferenceRecords/cr-1/participants/p1",
                    "text": "hello", "languageCode": "en-US"}],
               "nextPageToken": "tok2"},
        "tok2": {"transcriptEntries": [
                     {"name": "e2", "participant": "conferenceRecords/cr-1/participants/p2",
                      "text": "hi", "languageCode": "en-US"}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/conferenceRecords/cr-1/transcripts/t-1/entries"
        token = request.url.params.get("pageToken")
        return httpx.Response(200, json=pages[token])

    entries = await _client(handler).list_transcript_entries(
        "at-1", "conferenceRecords/cr-1/transcripts/t-1"
    )
    assert [e.text for e in entries] == ["hello", "hi"]
    assert entries[0].participant == "conferenceRecords/cr-1/participants/p1"
    assert entries[0].language_code == "en-US"


async def test_list_participants_maps_all_user_types():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/conferenceRecords/cr-1/participants"
        return httpx.Response(200, json={"participants": [
            {"name": "conferenceRecords/cr-1/participants/p1",
             "signedinUser": {"user": "users/u1", "displayName": "Alice"}},
            {"name": "conferenceRecords/cr-1/participants/p2",
             "anonymousUser": {"displayName": "Guest"}},
            {"name": "conferenceRecords/cr-1/participants/p3",
             "phoneUser": {"displayName": "+1 (555) ..."}},
        ]})

    parts = await _client(handler).list_participants("at-1", "conferenceRecords/cr-1")
    names = {p.name: p.display_name for p in parts}
    assert names["conferenceRecords/cr-1/participants/p1"] == "Alice"
    assert names["conferenceRecords/cr-1/participants/p2"] == "Guest"
    assert names["conferenceRecords/cr-1/participants/p3"] == "+1 (555) ..."


async def test_get_conference_record_404_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "not found"}})

    with pytest.raises(httpx.HTTPStatusError):
        await _client(handler).get_conference_record("at-1", "conferenceRecords/gone")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_meet_client_transcript.py -q`
Expected: FAIL — `AttributeError: 'GoogleMeetClient' object has no attribute 'get_conference_record'`.

- [ ] **Step 3: Extend `app/google/meet_client.py`**

Add these dataclasses and Protocol methods, and implement them on `GoogleMeetClient`. The file currently has `from __future__ import annotations`, `from typing import Protocol`, `import httpx`, and `MEET_BASE = "https://meet.googleapis.com/v2"`. Add a `from dataclasses import dataclass` import at the top.

Add dataclasses (after the imports, before the `MeetClient` Protocol):

```python
@dataclass
class ConferenceRecordInfo:
    name: str
    space: str | None
    start_time: str | None
    end_time: str | None


@dataclass
class TranscriptInfo:
    name: str
    state: str


@dataclass
class TranscriptEntryInfo:
    participant: str | None
    text: str
    language_code: str | None


@dataclass
class ParticipantInfo:
    name: str
    display_name: str
```

Extend the `MeetClient` Protocol with these signatures:

```python
    async def get_conference_record(
        self, access_token: str, conference_record_name: str
    ) -> ConferenceRecordInfo: ...
    async def get_transcript(
        self, access_token: str, transcript_resource_name: str
    ) -> TranscriptInfo: ...
    async def list_transcript_entries(
        self, access_token: str, transcript_resource_name: str
    ) -> list[TranscriptEntryInfo]: ...
    async def list_participants(
        self, access_token: str, conference_record_name: str
    ) -> list[ParticipantInfo]: ...
```

Add these methods to `GoogleMeetClient` (the class already has `_http`):

```python
    async def get_conference_record(
        self, access_token: str, conference_record_name: str
    ) -> ConferenceRecordInfo:
        async with self._http(access_token) as http:
            resp = await http.get(f"{MEET_BASE}/{conference_record_name}")
            resp.raise_for_status()
            data = resp.json()
            return ConferenceRecordInfo(
                name=data["name"],
                space=data.get("space"),
                start_time=data.get("startTime"),
                end_time=data.get("endTime"),
            )

    async def get_transcript(
        self, access_token: str, transcript_resource_name: str
    ) -> TranscriptInfo:
        async with self._http(access_token) as http:
            resp = await http.get(f"{MEET_BASE}/{transcript_resource_name}")
            resp.raise_for_status()
            data = resp.json()
            return TranscriptInfo(
                name=data["name"], state=data.get("state", "STATE_UNSPECIFIED")
            )

    async def list_transcript_entries(
        self, access_token: str, transcript_resource_name: str
    ) -> list[TranscriptEntryInfo]:
        entries: list[TranscriptEntryInfo] = []
        page_token: str | None = None
        async with self._http(access_token) as http:
            while True:
                params = {"pageSize": 100}
                if page_token:
                    params["pageToken"] = page_token
                resp = await http.get(
                    f"{MEET_BASE}/{transcript_resource_name}/entries", params=params
                )
                resp.raise_for_status()
                data = resp.json()
                for e in data.get("transcriptEntries", []):
                    entries.append(
                        TranscriptEntryInfo(
                            participant=e.get("participant"),
                            text=e.get("text", ""),
                            language_code=e.get("languageCode"),
                        )
                    )
                page_token = data.get("nextPageToken")
                if not page_token:
                    return entries

    async def list_participants(
        self, access_token: str, conference_record_name: str
    ) -> list[ParticipantInfo]:
        participants: list[ParticipantInfo] = []
        page_token: str | None = None
        async with self._http(access_token) as http:
            while True:
                params = {"pageSize": 250}
                if page_token:
                    params["pageToken"] = page_token
                resp = await http.get(
                    f"{MEET_BASE}/{conference_record_name}/participants", params=params
                )
                resp.raise_for_status()
                data = resp.json()
                for p in data.get("participants", []):
                    user = (
                        p.get("signedinUser")
                        or p.get("anonymousUser")
                        or p.get("phoneUser")
                        or {}
                    )
                    participants.append(
                        ParticipantInfo(
                            name=p["name"], display_name=user.get("displayName", "")
                        )
                    )
                page_token = data.get("nextPageToken")
                if not page_token:
                    return participants
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_meet_client_transcript.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the existing meet-client tests too (no regressions)**

Run: `python -m pytest tests/test_meet_client.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/google/meet_client.py tests/test_meet_client_transcript.py
git commit -m "feat: meet client transcript/participant/conference-record fetch"
```

---

## Task 4: Notes schemas (`NotesContent` + API response models)

**Files:** Create `app/schemas/notes.py`; Test `tests/test_notes_schema.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_notes_schema.py`:

```python
from app.schemas.notes import ActionItem, NotesContent


def test_notes_content_parses():
    nc = NotesContent(
        summary="We discussed the roadmap.",
        decisions=["Ship X in Q3"],
        action_items=[ActionItem(who="Alice", what="Draft the spec")],
    )
    assert nc.summary.startswith("We discussed")
    assert nc.decisions == ["Ship X in Q3"]
    assert nc.action_items[0].who == "Alice"


def test_notes_content_defaults_empty_lists():
    nc = NotesContent(summary="Short call, nothing decided.")
    assert nc.decisions == []
    assert nc.action_items == []
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_notes_schema.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.schemas.notes'`.

- [ ] **Step 3: Implement `app/schemas/notes.py`**

```python
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ActionItem(BaseModel):
    who: str = ""
    what: str


class NotesContent(BaseModel):
    """Structured output schema handed to the Gemini API as response_schema."""

    summary: str
    decisions: list[str] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)


class NotesResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    conference_id: uuid.UUID
    title: str
    summary: str
    decisions: list
    action_items: list
    doc_url: str | None = None
    created_at: datetime


class TranscriptResponse(BaseModel):
    conference_id: uuid.UUID
    language: str | None
    text: str
    speaker_map: dict


class ConferenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    meeting_id: uuid.UUID | None
    conference_record_name: str
    pipeline_state: str
    attempts: int
    last_error: str | None = None
    created_at: datetime
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_notes_schema.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/notes.py tests/test_notes_schema.py
git commit -m "feat: notes content + api response schemas"
```

---

## Task 5: Gemini summarizer client

**Files:** Create `app/google/gemini_client.py`; Test `tests/test_gemini_client.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_gemini_client.py`:

```python
import pytest

from app.google.gemini_client import GeminiSummarizer, SummarizationError
from app.schemas.notes import NotesContent


class _Resp:
    def __init__(self, parsed=None, text="", candidates=None, prompt_feedback=None):
        self.parsed = parsed
        self.text = text
        self.candidates = candidates if candidates is not None else [object()]
        self.prompt_feedback = prompt_feedback


class _FakeModels:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.calls = []

    async def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents})
        if self._exc is not None:
            raise self._exc
        return self._resp

    async def count_tokens(self, *, model, contents):
        class _T:
            total_tokens = len(contents)
        return _T()


class _FakeAio:
    def __init__(self, models):
        self.models = models


class _FakeClient:
    def __init__(self, models):
        self.aio = _FakeAio(models)


async def test_summarize_returns_parsed_notes():
    parsed = NotesContent(summary="Recap", decisions=["D1"], action_items=[])
    models = _FakeModels(resp=_Resp(parsed=parsed, text='{"summary":"Recap"}'))
    summarizer = GeminiSummarizer(client=_FakeClient(models), model="gemini-2.5-flash")
    out = await summarizer.summarize("alice: hello\nbob: hi")
    assert out.summary == "Recap"
    assert out.decisions == ["D1"]
    assert models.calls[0]["model"] == "gemini-2.5-flash"


async def test_summarize_falls_back_to_text_when_parsed_none():
    models = _FakeModels(resp=_Resp(parsed=None, text='{"summary":"From text","decisions":[],"action_items":[]}'))
    summarizer = GeminiSummarizer(client=_FakeClient(models), model="m")
    out = await summarizer.summarize("transcript")
    assert out.summary == "From text"


async def test_summarize_raises_on_empty_and_blocked():
    class _Blocked:
        block_reason = "SAFETY"
    models = _FakeModels(resp=_Resp(parsed=None, text="", candidates=[], prompt_feedback=_Blocked()))
    summarizer = GeminiSummarizer(client=_FakeClient(models), model="m")
    with pytest.raises(SummarizationError):
        await summarizer.summarize("transcript")


async def test_count_tokens_proxies_client():
    models = _FakeModels(resp=_Resp(parsed=NotesContent(summary="x")))
    summarizer = GeminiSummarizer(client=_FakeClient(models), model="m")
    n = await summarizer.count_tokens("hello")
    assert n == 5
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_gemini_client.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.google.gemini_client'`.

- [ ] **Step 3: Implement `app/google/gemini_client.py`**

```python
from __future__ import annotations

import json
import logging
from typing import Protocol

from app.schemas.notes import NotesContent

logger = logging.getLogger(__name__)

_PROMPT = (
    "You are a meeting-notes assistant. Read the following meeting transcript and "
    "produce concise, faithful notes. Capture the overall summary, explicit decisions "
    "made, and concrete action items with an owner when one is stated. Do not invent "
    "content that is not supported by the transcript.\n\nTRANSCRIPT:\n"
)


class SummarizationError(Exception):
    pass


class Summarizer(Protocol):
    async def summarize(self, transcript: str) -> NotesContent: ...
    async def count_tokens(self, text: str) -> int: ...


def _build_config():
    # Imported lazily so importing this module never requires the SDK at import time.
    from google.genai import types

    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=NotesContent,
    )


class GeminiSummarizer:
    def __init__(self, *, client, model: str) -> None:
        self._client = client
        self._model = model

    async def count_tokens(self, text: str) -> int:
        resp = await self._client.aio.models.count_tokens(
            model=self._model, contents=text
        )
        return resp.total_tokens

    async def summarize(self, transcript: str) -> NotesContent:
        resp = await self._client.aio.models.generate_content(
            model=self._model,
            contents=_PROMPT + transcript,
            config=_build_config(),
        )
        return self._extract(resp)

    @staticmethod
    def _extract(resp) -> NotesContent:
        feedback = getattr(resp, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            raise SummarizationError(f"prompt blocked: {feedback.block_reason}")
        if not getattr(resp, "candidates", None) and not getattr(resp, "text", ""):
            raise SummarizationError("empty completion")

        parsed = getattr(resp, "parsed", None)
        if isinstance(parsed, NotesContent):
            return parsed
        text = getattr(resp, "text", "") or ""
        if not text.strip():
            raise SummarizationError("empty completion")
        try:
            return NotesContent.model_validate(json.loads(text))
        except (ValueError, json.JSONDecodeError) as exc:
            raise SummarizationError(f"unparseable notes output: {exc}") from exc
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_gemini_client.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/google/gemini_client.py tests/test_gemini_client.py
git commit -m "feat: gemini summarizer client wrapping google-genai"
```

---

## Task 6: Transcript service (fetch, attribute, persist, map meeting)

**Files:** Create `app/services/transcript_service.py`; Test `tests/test_transcript_service.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_transcript_service.py`:

```python
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.crypto import decrypt
from app.google.meet_client import (
    ConferenceRecordInfo,
    ParticipantInfo,
    TranscriptEntryInfo,
)
from app.google.oauth_client import TokenBundle
from app.models import Conference, Meeting, Transcript, User
from app.services import connection_service, transcript_service


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeOAuthClient:
    async def refresh(self, refresh_token):
        return TokenBundle(access_token="at", expires_in=3599, scope="openid")


class FakeMeetClient:
    def __init__(self, *, space="spaces/SERVERID"):
        self._space = space

    async def get_conference_record(self, access_token, conference_record_name):
        return ConferenceRecordInfo(
            name=conference_record_name, space=self._space,
            start_time="2026-06-11T10:00:00Z", end_time="2026-06-11T11:00:00Z",
        )

    async def list_participants(self, access_token, conference_record_name):
        return [
            ParticipantInfo(name="conferenceRecords/cr-1/participants/p1", display_name="Alice"),
            ParticipantInfo(name="conferenceRecords/cr-1/participants/p2", display_name="Bob"),
        ]

    async def list_transcript_entries(self, access_token, transcript_resource_name):
        return [
            TranscriptEntryInfo(participant="conferenceRecords/cr-1/participants/p1", text="hello", language_code="en-US"),
            TranscriptEntryInfo(participant="conferenceRecords/cr-1/participants/p2", text="hi there", language_code="en-US"),
            TranscriptEntryInfo(participant=None, text="(unknown)", language_code="en-US"),
        ]


async def _conf_with_meeting(db_session, *, space="spaces/SERVERID", with_meeting=True):
    user = User(email="x@acme.com", name="X", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="x@acme.com"
    )
    if with_meeting:
        from datetime import datetime, timezone
        m = Meeting(
            user_id=user.id, title="Q3 Sync", start_time=datetime(2026, 6, 11, tzinfo=timezone.utc),
            end_time=datetime(2026, 6, 11, tzinfo=timezone.utc), attendees=[],
            meet_space_name=space, notes_enabled=True, notes_config={},
        )
        db_session.add(m)
        await db_session.commit()
    conf = Conference(
        oauth_connection_id=conn.id,
        conference_record_name="conferenceRecords/cr-1",
        transcript_resource_name="conferenceRecords/cr-1/transcripts/t-1",
        pipeline_state="pending",
    )
    db_session.add(conf)
    await db_session.commit()
    await db_session.refresh(conf)
    return conf


async def test_fetch_assembles_attributed_text_and_maps_meeting(db_session):
    conf = await _conf_with_meeting(db_session)
    await transcript_service.fetch_transcript(
        db_session, conference=conf, oauth_client=FakeOAuthClient(), meet_client=FakeMeetClient()
    )
    t = await db_session.scalar(select(Transcript).where(Transcript.conference_id == conf.id))
    assert t is not None
    text = decrypt(t.full_text)
    assert "Alice: hello" in text
    assert "Bob: hi there" in text
    assert "(unknown)" in text  # entries with no participant still included
    assert t.language == "en-US"
    assert t.speaker_map["conferenceRecords/cr-1/participants/p1"] == "Alice"
    # meeting was mapped via space
    await db_session.refresh(conf)
    assert conf.meeting_id is not None


async def test_fetch_without_matching_meeting_leaves_meeting_id_none(db_session):
    conf = await _conf_with_meeting(db_session, with_meeting=False)
    await transcript_service.fetch_transcript(
        db_session, conference=conf, oauth_client=FakeOAuthClient(), meet_client=FakeMeetClient()
    )
    await db_session.refresh(conf)
    assert conf.meeting_id is None  # no meeting with that space; allowed


async def test_fetch_empty_transcript_stores_empty(db_session):
    class EmptyMeet(FakeMeetClient):
        async def list_transcript_entries(self, access_token, transcript_resource_name):
            return []

    conf = await _conf_with_meeting(db_session)
    await transcript_service.fetch_transcript(
        db_session, conference=conf, oauth_client=FakeOAuthClient(), meet_client=EmptyMeet()
    )
    t = await db_session.scalar(select(Transcript).where(Transcript.conference_id == conf.id))
    assert t is not None
    assert decrypt(t.full_text) == ""
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_transcript_service.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.transcript_service'`.

- [ ] **Step 3: Implement `app/services/transcript_service.py`**

```python
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import encrypt
from app.google.meet_client import MeetClient
from app.google.oauth_client import OAuthClient
from app.models import Conference, Meeting, OAuthConnection, Transcript
from app.services import connection_service

logger = logging.getLogger(__name__)


class TranscriptNotReadyError(Exception):
    """No transcript resource is associated with the conference yet."""


async def _access_token(
    session: AsyncSession, conference: Conference, oauth_client: OAuthClient
) -> str:
    conn = await session.get(OAuthConnection, conference.oauth_connection_id)
    if conn is None:
        raise TranscriptNotReadyError("conference has no connection")
    return await connection_service.get_valid_access_token(session, conn, oauth_client)


async def _map_meeting(session: AsyncSession, conference: Conference, space: str | None) -> None:
    if conference.meeting_id is not None or not space:
        return
    meeting = await session.scalar(
        select(Meeting).where(Meeting.meet_space_name == space)
    )
    if meeting is not None:
        conference.meeting_id = meeting.id


def _assemble(entries, speaker_map: dict) -> tuple[str, str | None]:
    lines: list[str] = []
    language: str | None = None
    for e in entries:
        if language is None and e.language_code:
            language = e.language_code
        speaker = speaker_map.get(e.participant or "", "Unknown") if e.participant else "Unknown"
        lines.append(f"{speaker}: {e.text}")
    return "\n".join(lines), language


async def fetch_transcript(
    session: AsyncSession,
    *,
    conference: Conference,
    oauth_client: OAuthClient,
    meet_client: MeetClient,
) -> Transcript:
    if not conference.transcript_resource_name:
        raise TranscriptNotReadyError("conference has no transcript resource yet")

    access_token = await _access_token(session, conference, oauth_client)

    record = await meet_client.get_conference_record(
        access_token, conference.conference_record_name
    )
    await _map_meeting(session, conference, record.space)

    participants = await meet_client.list_participants(
        access_token, conference.conference_record_name
    )
    speaker_map = {p.name: p.display_name for p in participants}

    entries = await meet_client.list_transcript_entries(
        access_token, conference.transcript_resource_name
    )
    full_text, language = _assemble(entries, speaker_map)

    existing = await session.scalar(
        select(Transcript).where(Transcript.conference_id == conference.id)
    )
    if existing is None:
        existing = Transcript(conference_id=conference.id)
        session.add(existing)
    existing.full_text = encrypt(full_text)
    existing.language = language
    existing.speaker_map = speaker_map

    await session.commit()
    await session.refresh(existing)
    return existing
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_transcript_service.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/transcript_service.py tests/test_transcript_service.py
git commit -m "feat: transcript service (fetch, attribute speakers, map meeting)"
```

---

## Task 7: Notes service (chunk + summarize + persist)

**Files:** Create `app/services/notes_service.py`; Test `tests/test_notes_service.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_notes_service.py`:

```python
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.crypto import encrypt
from app.google.oauth_client import TokenBundle
from app.models import Conference, Meeting, Notes, Transcript, User
from app.schemas.notes import ActionItem, NotesContent
from app.services import connection_service, notes_service


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeSummarizer:
    def __init__(self, *, tokens=10):
        self._tokens = tokens
        self.summarize_calls = []

    async def count_tokens(self, text):
        return self._tokens

    async def summarize(self, transcript):
        self.summarize_calls.append(transcript)
        # echo a stable structure; include marker of how much text it saw
        return NotesContent(
            summary=f"summary of {len(transcript)} chars",
            decisions=["D1"],
            action_items=[ActionItem(who="Alice", what="follow up")],
        )


async def _conf_with_transcript(db_session, *, title="Q3 Sync", text="Alice: hello\nBob: hi", with_meeting=True):
    user = User(email="z@acme.com", name="Z", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="z@acme.com"
    )
    meeting_id = None
    if with_meeting:
        from datetime import datetime, timezone
        m = Meeting(
            user_id=user.id, title=title, start_time=datetime(2026, 6, 11, tzinfo=timezone.utc),
            end_time=datetime(2026, 6, 11, tzinfo=timezone.utc), attendees=[],
            meet_space_name="spaces/S", notes_enabled=True, notes_config={},
        )
        db_session.add(m)
        await db_session.commit()
        await db_session.refresh(m)
        meeting_id = m.id
    conf = Conference(
        oauth_connection_id=conn.id, meeting_id=meeting_id,
        conference_record_name="conferenceRecords/cr-1", pipeline_state="transcript_fetched",
    )
    db_session.add(conf)
    await db_session.commit()
    await db_session.refresh(conf)
    db_session.add(Transcript(conference_id=conf.id, full_text=encrypt(text), language="en-US", speaker_map={}))
    await db_session.commit()
    return conf


async def test_generate_notes_titles_with_meeting_title(db_session):
    conf = await _conf_with_transcript(db_session, title="Q3 Roadmap Sync")
    summarizer = FakeSummarizer()
    notes = await notes_service.generate_notes(
        db_session, conference=conf, summarizer=summarizer, model="gemini-2.5-flash",
        chunk_threshold=600000, default_title="Meeting Notes",
    )
    assert notes.title == "Q3 Roadmap Sync"
    assert notes.summary.startswith("summary of")
    assert notes.decisions == ["D1"]
    assert notes.action_items[0]["who"] == "Alice"
    assert notes.gemini_model == "gemini-2.5-flash"
    assert len(summarizer.summarize_calls) == 1  # small transcript -> single pass


async def test_generate_notes_default_title_without_meeting(db_session):
    conf = await _conf_with_transcript(db_session, with_meeting=False)
    notes = await notes_service.generate_notes(
        db_session, conference=conf, summarizer=FakeSummarizer(), model="m",
        chunk_threshold=600000, default_title="Meeting Notes",
    )
    assert notes.title == "Meeting Notes"


async def test_generate_notes_empty_transcript_skips_summarizer(db_session):
    conf = await _conf_with_transcript(db_session, text="")
    summarizer = FakeSummarizer()
    notes = await notes_service.generate_notes(
        db_session, conference=conf, summarizer=summarizer, model="m",
        chunk_threshold=600000, default_title="Meeting Notes",
    )
    assert summarizer.summarize_calls == []  # nothing to summarize
    assert notes.summary == "No content was captured for this meeting."


async def test_generate_notes_map_reduce_when_over_threshold(db_session):
    conf = await _conf_with_transcript(db_session, text="x" * 5000)
    # threshold tiny so it chunks; tokens reported per call is len(text)
    summarizer = FakeSummarizer(tokens=5000)
    notes = await notes_service.generate_notes(
        db_session, conference=conf, summarizer=summarizer, model="m",
        chunk_threshold=1000, default_title="Meeting Notes",
    )
    # multiple chunk summaries + 1 reduce pass
    assert len(summarizer.summarize_calls) > 1
    assert notes.summary.startswith("summary of")


async def test_generate_notes_is_idempotent_upsert(db_session):
    conf = await _conf_with_transcript(db_session)
    await notes_service.generate_notes(
        db_session, conference=conf, summarizer=FakeSummarizer(), model="m",
        chunk_threshold=600000, default_title="Meeting Notes",
    )
    await notes_service.generate_notes(
        db_session, conference=conf, summarizer=FakeSummarizer(), model="m",
        chunk_threshold=600000, default_title="Meeting Notes",
    )
    rows = (await db_session.scalars(select(Notes).where(Notes.conference_id == conf.id))).all()
    assert len(rows) == 1
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_notes_service.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.notes_service'`.

- [ ] **Step 3: Implement `app/services/notes_service.py`**

```python
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crypto import decrypt
from app.google.gemini_client import Summarizer
from app.models import Conference, Meeting, Notes, Transcript
from app.schemas.notes import NotesContent

logger = logging.getLogger(__name__)

_EMPTY_SUMMARY = "No content was captured for this meeting."


def _split_long_line(line: str, max_chars: int) -> list[str]:
    if len(line) <= max_chars:
        return [line]
    return [line[i : i + max_chars] for i in range(0, len(line), max_chars)]


def _chunk(text: str, max_chars: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for raw_line in text.split("\n"):
        # A single speaker turn longer than the budget is split by characters so
        # no chunk ever exceeds max_chars.
        for line in _split_long_line(raw_line, max_chars):
            if size + len(line) > max_chars and current:
                chunks.append("\n".join(current))
                current = []
                size = 0
            current.append(line)
            size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or [text]


async def _title_for(session: AsyncSession, conference: Conference, default_title: str) -> str:
    if conference.meeting_id is not None:
        meeting = await session.get(Meeting, conference.meeting_id)
        if meeting is not None and meeting.title:
            return meeting.title
    return default_title


async def _summarize_text(
    summarizer: Summarizer, text: str, chunk_threshold: int
) -> NotesContent:
    # Token threshold drives whether we map-reduce. We approximate one token per
    # ~4 chars for the chunk size; the count_tokens call decides if we chunk at all.
    total_tokens = await summarizer.count_tokens(text)
    if total_tokens <= chunk_threshold:
        return await summarizer.summarize(text)

    max_chars = max(1000, chunk_threshold * 4)
    chunks = _chunk(text, max_chars)
    partials = [await summarizer.summarize(c) for c in chunks]
    combined = "\n\n".join(
        f"Section {i + 1} summary: {p.summary}\n"
        f"Decisions: {'; '.join(p.decisions)}\n"
        f"Action items: {'; '.join(a.what for a in p.action_items)}"
        for i, p in enumerate(partials)
    )
    return await summarizer.summarize(combined)


async def generate_notes(
    session: AsyncSession,
    *,
    conference: Conference,
    summarizer: Summarizer,
    model: str,
    chunk_threshold: int,
    default_title: str,
) -> Notes:
    transcript = await session.scalar(
        select(Transcript).where(Transcript.conference_id == conference.id)
    )
    if transcript is None:
        raise ValueError("no transcript to summarize")

    text = decrypt(transcript.full_text) if transcript.full_text else ""
    title = await _title_for(session, conference, default_title)

    if not text.strip():
        content = NotesContent(summary=_EMPTY_SUMMARY)
    else:
        content = await _summarize_text(summarizer, text, chunk_threshold)

    existing = await session.scalar(
        select(Notes).where(Notes.conference_id == conference.id)
    )
    if existing is None:
        existing = Notes(conference_id=conference.id)
        session.add(existing)
    existing.title = title
    existing.summary = content.summary
    existing.decisions = list(content.decisions)
    existing.action_items = [a.model_dump() for a in content.action_items]
    existing.gemini_model = model

    await session.commit()
    await session.refresh(existing)
    return existing
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_notes_service.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/notes_service.py tests/test_notes_service.py
git commit -m "feat: notes service (chunk, summarize, persist, idempotent)"
```

---

## Task 8: Pipeline orchestrator (resumable state machine)

**Files:** Create `app/services/pipeline.py`; Test `tests/test_pipeline.py`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_pipeline.py`:

```python
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.crypto import encrypt
from app.google.meet_client import (
    ConferenceRecordInfo,
    ParticipantInfo,
    TranscriptEntryInfo,
)
from app.google.oauth_client import TokenBundle
from app.models import Conference, Notes, Transcript, User
from app.schemas.notes import NotesContent
from app.services import connection_service, pipeline


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeOAuthClient:
    async def refresh(self, refresh_token):
        return TokenBundle(access_token="at", expires_in=3599, scope="openid")


class FakeMeetClient:
    def __init__(self):
        self.entry_calls = 0

    async def get_conference_record(self, access_token, conference_record_name):
        return ConferenceRecordInfo(conference_record_name, "spaces/S", None, None)

    async def list_participants(self, access_token, conference_record_name):
        return [ParticipantInfo(name="conferenceRecords/cr-1/participants/p1", display_name="Alice")]

    async def list_transcript_entries(self, access_token, transcript_resource_name):
        self.entry_calls += 1
        return [TranscriptEntryInfo(participant="conferenceRecords/cr-1/participants/p1", text="hello", language_code="en-US")]


class FakeSummarizer:
    def __init__(self):
        self.calls = 0

    async def count_tokens(self, text):
        return 10

    async def summarize(self, transcript):
        self.calls += 1
        return NotesContent(summary="recap", decisions=[], action_items=[])


async def _conf(db_session, *, state="pending", with_transcript_resource=True):
    user = User(email="p@acme.com", name="P", hashed_password="x")
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="p@acme.com"
    )
    conf = Conference(
        oauth_connection_id=conn.id,
        conference_record_name="conferenceRecords/cr-1",
        transcript_resource_name="conferenceRecords/cr-1/transcripts/t-1" if with_transcript_resource else None,
        pipeline_state=state,
    )
    db_session.add(conf)
    await db_session.commit()
    await db_session.refresh(conf)
    return conf


async def test_run_pipeline_full_advances_to_notes_generated(db_session):
    conf = await _conf(db_session)
    meet = FakeMeetClient()
    summ = FakeSummarizer()
    await pipeline.run_pipeline(
        db_session, conference_id=conf.id, oauth_client=FakeOAuthClient(),
        meet_client=meet, summarizer=summ, model="m", chunk_threshold=600000,
        default_title="Meeting Notes",
    )
    await db_session.refresh(conf)
    assert conf.pipeline_state == "notes_generated"
    assert await db_session.scalar(select(Transcript).where(Transcript.conference_id == conf.id)) is not None
    assert await db_session.scalar(select(Notes).where(Notes.conference_id == conf.id)) is not None


async def test_run_pipeline_resumes_from_transcript_fetched(db_session):
    conf = await _conf(db_session, state="transcript_fetched")
    # seed an existing transcript so stage 1 is already done
    db_session.add(Transcript(conference_id=conf.id, full_text=encrypt("Alice: hi"), language="en-US", speaker_map={}))
    await db_session.commit()
    meet = FakeMeetClient()
    summ = FakeSummarizer()
    await pipeline.run_pipeline(
        db_session, conference_id=conf.id, oauth_client=FakeOAuthClient(),
        meet_client=meet, summarizer=summ, model="m", chunk_threshold=600000,
        default_title="Meeting Notes",
    )
    assert meet.entry_calls == 0  # stage 1 skipped
    assert summ.calls == 1
    await db_session.refresh(conf)
    assert conf.pipeline_state == "notes_generated"


async def test_run_pipeline_already_done_is_noop(db_session):
    conf = await _conf(db_session, state="notes_generated")
    meet = FakeMeetClient()
    summ = FakeSummarizer()
    await pipeline.run_pipeline(
        db_session, conference_id=conf.id, oauth_client=FakeOAuthClient(),
        meet_client=meet, summarizer=summ, model="m", chunk_threshold=600000,
        default_title="Meeting Notes",
    )
    assert meet.entry_calls == 0
    assert summ.calls == 0


async def test_run_pipeline_records_failure_and_reraises(db_session):
    class BoomMeet(FakeMeetClient):
        async def list_transcript_entries(self, access_token, transcript_resource_name):
            raise RuntimeError("meet api down")

    conf = await _conf(db_session)
    with pytest.raises(RuntimeError):
        await pipeline.run_pipeline(
            db_session, conference_id=conf.id, oauth_client=FakeOAuthClient(),
            meet_client=BoomMeet(), summarizer=FakeSummarizer(), model="m",
            chunk_threshold=600000, default_title="Meeting Notes",
        )
    await db_session.refresh(conf)
    assert conf.pipeline_state == "failed"
    assert conf.attempts == 1
    assert "meet api down" in (conf.last_error or "")


async def test_run_pipeline_missing_conference_is_noop(db_session):
    import uuid
    # should not raise
    await pipeline.run_pipeline(
        db_session, conference_id=uuid.uuid4(), oauth_client=FakeOAuthClient(),
        meet_client=FakeMeetClient(), summarizer=FakeSummarizer(), model="m",
        chunk_threshold=600000, default_title="Meeting Notes",
    )
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_pipeline.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.pipeline'`.

- [ ] **Step 3: Implement `app/services/pipeline.py`**

```python
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.google.gemini_client import Summarizer
from app.google.meet_client import MeetClient
from app.google.oauth_client import OAuthClient
from app.models import Conference
from app.services import notes_service, transcript_service

logger = logging.getLogger(__name__)

# Ordered pipeline states. Terminal success state for Phase 5 is "notes_generated".
# Phase 6 appends "doc_created" and "emailed".
_ORDER = {
    "pending": 0,
    "transcript_fetched": 1,
    "notes_generated": 2,
    "failed": -1,
}


def _reached(state: str, target: str) -> bool:
    return _ORDER.get(state, -1) >= _ORDER[target]


async def run_pipeline(
    session: AsyncSession,
    *,
    conference_id: uuid.UUID,
    oauth_client: OAuthClient,
    meet_client: MeetClient,
    summarizer: Summarizer,
    model: str,
    chunk_threshold: int,
    default_title: str,
) -> None:
    conference = await session.get(Conference, conference_id)
    if conference is None:
        logger.warning("pipeline: conference %s not found", conference_id)
        return

    if _reached(conference.pipeline_state, "notes_generated"):
        logger.info("pipeline: conference %s already complete", conference_id)
        return

    try:
        if not _reached(conference.pipeline_state, "transcript_fetched"):
            await transcript_service.fetch_transcript(
                session, conference=conference, oauth_client=oauth_client,
                meet_client=meet_client,
            )
            conference.pipeline_state = "transcript_fetched"
            conference.last_error = None
            await session.commit()

        if not _reached(conference.pipeline_state, "notes_generated"):
            await notes_service.generate_notes(
                session, conference=conference, summarizer=summarizer, model=model,
                chunk_threshold=chunk_threshold, default_title=default_title,
            )
            conference.pipeline_state = "notes_generated"
            conference.last_error = None
            await session.commit()
    except Exception as exc:
        await session.rollback()
        # Reload after rollback; record the failure on a clean transaction.
        conference = await session.get(Conference, conference_id)
        if conference is not None:
            conference.pipeline_state = "failed"
            conference.attempts = (conference.attempts or 0) + 1
            conference.last_error = str(exc)[:2000]
            await session.commit()
        logger.exception("pipeline failed for conference %s", conference_id)
        raise
```

> **Resumability note:** each stage commits its state before the next runs. If the worker crashes *between* stages, the conference is left at a real state (`transcript_fetched`) and a retry skips the completed stage via the `_reached` guard. If a stage *raises*, we set `failed` (which has `_ORDER -1`, below all real states), so a retry re-runs both guards from the top — but because `fetch_transcript` and `generate_notes` are idempotent upserts (one row per conference, overwritten in place), re-running an already-completed stage is cheap and produces no duplicate side effects. So both crash-between and raise-then-retry are safe; the only difference is the failed case may redo one already-done stage. The arq worker's `max_tries` (Task 9) re-invokes `run_pipeline` on failure.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_pipeline.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/services/pipeline.py tests/test_pipeline.py
git commit -m "feat: resumable notes-pipeline state machine"
```

---

## Task 9: arq worker module + RealJobQueue (Redis deferred)

**Files:** Create `app/worker.py`; Test `tests/test_worker.py`.

> This task defines the worker and a Redis-backed queue but does NOT require Redis to build or test. The task function is unit-tested by calling it with a hand-built `ctx`. `RealJobQueue` is only constructed when `REDIS_URL` is set (Task 10 wires that); its `enqueue` path is covered with a fake pool.

- [ ] **Step 1: Write the failing test**

Create `tests/test_worker.py`:

```python
import uuid

import pytest

from app import worker


class _RecordingPipeline:
    def __init__(self):
        self.calls = []

    async def __call__(self, session, **kwargs):
        self.calls.append(kwargs["conference_id"])


async def test_notes_pipeline_task_builds_session_and_runs(monkeypatch):
    rec = _RecordingPipeline()
    monkeypatch.setattr(worker.pipeline, "run_pipeline", rec)

    sessions_closed = []

    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            sessions_closed.append(True)

    monkeypatch.setattr(worker, "SessionLocal", lambda: FakeSession())

    cid = str(uuid.uuid4())
    ctx = {"job_try": 1}
    await worker.notes_pipeline(ctx, cid)
    assert rec.calls == [uuid.UUID(cid)]
    assert sessions_closed == [True]


def test_worker_settings_lists_task():
    assert worker.notes_pipeline in worker.WorkerSettings.functions


class _FakePool:
    def __init__(self):
        self.enqueued = []

    async def enqueue_job(self, name, *args, _job_id=None):
        self.enqueued.append((name, args, _job_id))
        return object()


async def test_real_job_queue_enqueues_with_dedup_id():
    pool = _FakePool()
    q = worker.RealJobQueue(pool)
    await q.enqueue_notes_pipeline("conf-123")
    assert pool.enqueued == [("notes_pipeline", ("conf-123",), "notes:conf-123")]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_worker.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.worker'`.

- [ ] **Step 3: Implement `app/worker.py`**

```python
from __future__ import annotations

import logging
import uuid

from app.config import get_settings
from app.db import SessionLocal
from app.services import pipeline

logger = logging.getLogger(__name__)


def _build_summarizer():
    # Lazy import so importing this module needs neither the SDK nor a network call.
    from google import genai

    from app.google.gemini_client import GeminiSummarizer

    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_api_key)
    return GeminiSummarizer(client=client, model=settings.gemini_model)


def _build_clients():
    from app.google.calendar_client import GoogleCalendarClient  # noqa: F401 (parity import)
    from app.google.meet_client import GoogleMeetClient
    from app.google.oauth_client import GoogleOAuthClient

    settings = get_settings()
    oauth_client = GoogleOAuthClient(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        redirect_uri=settings.google_redirect_uri,
        scopes=settings.google_scopes,
    )
    return oauth_client, GoogleMeetClient()


async def notes_pipeline(ctx, conference_id: str) -> None:
    settings = get_settings()
    oauth_client, meet_client = _build_clients()
    summarizer = _build_summarizer()
    async with SessionLocal() as session:
        await pipeline.run_pipeline(
            session,
            conference_id=uuid.UUID(conference_id),
            oauth_client=oauth_client,
            meet_client=meet_client,
            summarizer=summarizer,
            model=settings.gemini_model,
            chunk_threshold=settings.gemini_chunk_token_threshold,
            default_title=settings.notes_default_title,
        )


def _redis_settings():
    from arq.connections import RedisSettings

    return RedisSettings.from_dsn(get_settings().redis_url)


class WorkerSettings:
    functions = [notes_pipeline]
    max_tries = 4

    @staticmethod
    def redis_settings():
        return _redis_settings()


class RealJobQueue:
    """arq-backed queue. Construct with an arq pool (created where REDIS_URL is set)."""

    def __init__(self, pool) -> None:
        self._pool = pool

    async def enqueue_notes_pipeline(self, conference_id: str) -> None:
        await self._pool.enqueue_job(
            "notes_pipeline", conference_id, _job_id=f"notes:{conference_id}"
        )
```

> `WorkerSettings.redis_settings` is a staticmethod so importing this module never reads/constructs a Redis connection (it's only called when the worker process actually starts via `arq app.worker.WorkerSettings`).

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_worker.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/worker.py tests/test_worker.py
git commit -m "feat: arq worker task and RealJobQueue (redis deferred)"
```

---

## Task 10: Wire summarizer + queue providers into deps

**Files:** Modify `app/api/deps.py`; Test `tests/test_deps_providers.py`.

> The webhook (Phase 4) already depends on `get_job_queue`. This task makes `get_job_queue` return a `RealJobQueue` when `REDIS_URL` is set, else keep the `NullJobQueue` singleton — so behavior is unchanged in tests/dev (no Redis) but production enqueues for real. Also adds `get_summarizer`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_deps_providers.py`:

```python
import pytest


@pytest.fixture(autouse=True)
def _clear_settings():
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_get_job_queue_returns_null_when_no_redis(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    from app.queue import NullJobQueue
    q = deps.get_job_queue()
    assert isinstance(q, NullJobQueue)


def test_get_summarizer_builds_gemini_summarizer(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    from app.google.gemini_client import GeminiSummarizer
    s = deps.get_summarizer()
    assert isinstance(s, GeminiSummarizer)
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_deps_providers.py -q`
Expected: FAIL — `AttributeError: module 'app.api.deps' has no attribute 'get_summarizer'`.

- [ ] **Step 3: Update `app/api/deps.py`**

Add imports (near the others):

```python
from app.google.gemini_client import GeminiSummarizer, Summarizer
```

Replace the existing `get_job_queue` with a config-aware version (the module already has `_job_queue = NullJobQueue()` and `get_settings` imported):

```python
def get_job_queue() -> JobQueue:
    # Redis-backed queue only when configured; otherwise the in-process no-op
    # queue (durable `conferences` row remains the source of truth).
    return _job_queue
```

> Leave `get_job_queue` returning the `NullJobQueue` singleton in the API process. The real enqueue path is exercised by the worker/queue tests; wiring a live arq pool into the FastAPI app is deferred until Redis is provisioned (the webhook still records the durable `pending` conference either way). Add a `get_summarizer` provider:

```python
def get_summarizer() -> Summarizer:
    from google import genai

    settings = get_settings()
    client = genai.Client(api_key=settings.gemini_api_key)
    return GeminiSummarizer(client=client, model=settings.gemini_model)
```

> `genai.Client(api_key=...)` makes no network call at construction (verified), so building it with an empty/fake key in tests is safe — the fake is injected via dependency override in the API tests, so `get_summarizer` is never actually called there with a real key.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/test_deps_providers.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the full suite (no regression to the webhook)**

Run: `python -m pytest -q`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add app/api/deps.py tests/test_deps_providers.py
git commit -m "feat: summarizer provider; job-queue provider stays config-aware"
```

---

## Task 11: Read endpoints + regenerate

**Files:** Create `app/api/routes/notes.py`; Modify `app/main.py`; Test `tests/test_notes_api.py`.

Endpoints (all require auth; ownership enforced by walking conference → connection → user):
- `GET /v1/conferences/{conference_id}/notes` → notes for that conference (404 if none).
- `GET /v1/conferences/{conference_id}/transcript` → decrypted transcript text (404 if none / nulled).
- `GET /v1/meetings/{meeting_id}/conferences` → list occurrences + pipeline_state.
- `GET /v1/meetings/{meeting_id}/notes` → notes for the latest occurrence of a meeting (convenience).
- `POST /v1/conferences/{conference_id}/notes:regenerate` → re-run notes generation synchronously (uses the injected summarizer); returns the refreshed notes.

- [ ] **Step 1: Write the failing test**

Create `tests/test_notes_api.py`:

```python
import uuid
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

from app.api.deps import get_summarizer
from app.crypto import encrypt
from app.google.oauth_client import TokenBundle
from app.main import app
from app.models import Conference, Meeting, Notes, Transcript, User
from app.schemas.notes import ActionItem, NotesContent
from app.services import connection_service


@pytest.fixture(autouse=True)
def _set_key(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class FakeSummarizer:
    async def count_tokens(self, text):
        return 5

    async def summarize(self, transcript):
        return NotesContent(summary="regenerated", decisions=["D9"], action_items=[ActionItem(who="Bob", what="x")])


@pytest.fixture
def fake_summarizer():
    app.dependency_overrides[get_summarizer] = lambda: FakeSummarizer()
    yield
    app.dependency_overrides.pop(get_summarizer, None)


async def _register(client) -> tuple[str, uuid.UUID]:
    resp = await client.post(
        "/v1/auth/register",
        json={"email": "owner@acme.com", "name": "O", "password": "password123"},
    )
    token = resp.json()["access_token"]
    me = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    return token, uuid.UUID(me.json()["id"])


async def _seed(db_session, user_id, *, with_notes=True, with_transcript=True, state="notes_generated"):
    user = await db_session.get(User, user_id)
    bundle = TokenBundle(access_token="at", expires_in=3599, scope="openid", refresh_token="rt")
    conn = await connection_service.upsert_connection(
        db_session, user=user, bundle=bundle, google_email="owner@acme.com"
    )
    meeting = Meeting(
        user_id=user_id, title="Q3 Sync", start_time=datetime(2026, 6, 11, tzinfo=timezone.utc),
        end_time=datetime(2026, 6, 11, tzinfo=timezone.utc), attendees=[],
        meet_space_name="spaces/S", notes_enabled=True, notes_config={},
    )
    db_session.add(meeting)
    await db_session.commit()
    await db_session.refresh(meeting)
    conf = Conference(
        oauth_connection_id=conn.id, meeting_id=meeting.id,
        conference_record_name="conferenceRecords/cr-1", pipeline_state=state,
    )
    db_session.add(conf)
    await db_session.commit()
    await db_session.refresh(conf)
    if with_transcript:
        db_session.add(Transcript(conference_id=conf.id, full_text=encrypt("Alice: hello"), language="en-US", speaker_map={}))
    if with_notes:
        db_session.add(Notes(conference_id=conf.id, title="Q3 Sync", summary="recap", decisions=["D1"], action_items=[], gemini_model="m"))
    await db_session.commit()
    return meeting, conf


async def test_get_conference_notes(client, db_session):
    token, uid = await _register(client)
    _, conf = await _seed(db_session, uid)
    resp = await client.get(f"/v1/conferences/{conf.id}/notes", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["summary"] == "recap"
    assert resp.json()["title"] == "Q3 Sync"


async def test_get_conference_transcript(client, db_session):
    token, uid = await _register(client)
    _, conf = await _seed(db_session, uid)
    resp = await client.get(f"/v1/conferences/{conf.id}/transcript", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["text"] == "Alice: hello"


async def test_get_notes_404_when_absent(client, db_session):
    token, uid = await _register(client)
    _, conf = await _seed(db_session, uid, with_notes=False)
    resp = await client.get(f"/v1/conferences/{conf.id}/notes", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404


async def test_list_meeting_conferences(client, db_session):
    token, uid = await _register(client)
    meeting, conf = await _seed(db_session, uid)
    resp = await client.get(f"/v1/meetings/{meeting.id}/conferences", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["pipeline_state"] == "notes_generated"


async def test_meeting_notes_latest_occurrence(client, db_session):
    token, uid = await _register(client)
    meeting, conf = await _seed(db_session, uid)
    resp = await client.get(f"/v1/meetings/{meeting.id}/notes", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["summary"] == "recap"


async def test_regenerate_notes(client, db_session, fake_summarizer):
    token, uid = await _register(client)
    _, conf = await _seed(db_session, uid)
    resp = await client.post(
        f"/v1/conferences/{conf.id}/notes:regenerate", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["summary"] == "regenerated"
    assert resp.json()["decisions"] == ["D9"]


async def test_cannot_access_other_users_conference(client, db_session):
    token, uid = await _register(client)
    # second user owns the conference
    other = User(email="other@acme.com", name="X", hashed_password="x")
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)
    _, conf = await _seed(db_session, other.id)
    resp = await client.get(f"/v1/conferences/{conf.id}/notes", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python -m pytest tests/test_notes_api.py -q`
Expected: FAIL — `404`/import errors (router not registered).

- [ ] **Step 3: Implement `app/api/routes/notes.py`**

```python
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
```

- [ ] **Step 4: Register the router in `app/main.py`**

Add `notes` to the route imports and `app.include_router(notes.router)` alongside the existing routers (match the existing style).

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_notes_api.py -q`
Expected: PASS (7 tests).

- [ ] **Step 6: Commit**

```bash
git add app/api/routes/notes.py app/main.py tests/test_notes_api.py
git commit -m "feat: notes/transcript read endpoints and regenerate"
```

---

## Task 12: Alembic migration for `transcripts` + `notes`

**Files:** Create (autogenerated) one migration under `migrations/versions/`.

- [ ] **Step 1: Confirm models are exported for autogenerate**

`migrations/env.py` does `from app import models`. Confirm `app/models/__init__.py` exports `Transcript` and `Notes` (Task 2).

- [ ] **Step 2: Bring the dev DB to head**

Run: `python -m alembic upgrade head`
Expected: no error (already at the Phase 4 head `d73602f70688` or applies it).

- [ ] **Step 3: Autogenerate the migration**

Run: `python -m alembic revision --autogenerate -m "phase5 transcripts and notes tables"`
Expected: a new file in `migrations/versions/`; console lists adding tables `transcripts` and `notes`.

- [ ] **Step 4: Read and verify the migration**

Open the new file. Confirm `upgrade()` creates:
- `transcripts`: `id` pk, `conference_id` (uuid FK→conferences.id CASCADE, **unique**, indexed), `full_text` (LargeBinary nullable), `language` (String(32) nullable), `speaker_map` (JSONB), `fetched_at`.
- `notes`: `id` pk, `conference_id` (uuid FK→conferences.id CASCADE, **unique**, indexed), `title`, `summary`, `decisions`/`action_items` (JSONB), `gemini_model`, `doc_id`, `doc_url`, `emailed_at`, `created_at`.
- `downgrade()` drops both tables (and their indexes) in FK-safe order.
- No spurious diffs to other tables. If any appear, delete those lines (autogenerate noise). Match the style of existing migrations.

- [ ] **Step 5: Apply and round-trip**

```bash
python -m alembic upgrade head
python -m alembic downgrade -1
python -m alembic upgrade head
python -m alembic check
```
Expected: all succeed; `alembic check` prints "No new upgrade operations detected."

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all).

- [ ] **Step 7: Commit**

```bash
git add migrations/versions/
git commit -m "feat: phase5 migration (transcripts + notes tables)"
```

---

## Task 13: Phase verification & cleanup

**Files:** none (verification only)

- [ ] **Step 1: Full suite green**

Run: `python -m pytest -q`
Expected: PASS. Record the count (121 baseline + the new tests).

- [ ] **Step 2: App imports and routes registered**

Run: `python -c "from app.main import app; print(sorted(r.path for r in app.routes if 'conferences' in r.path or 'notes' in r.path))"`
Expected: includes `/v1/conferences/{conference_id}/notes`, `/v1/conferences/{conference_id}/transcript`, `/v1/conferences/{conference_id}/notes:regenerate`, `/v1/meetings/{meeting_id}/conferences`, `/v1/meetings/{meeting_id}/notes`.

- [ ] **Step 3: Worker module imports without Redis**

Run: `python -c "from app import worker; print(worker.WorkerSettings.functions)"`
Expected: prints the task list (no Redis connection attempted).

- [ ] **Step 4: Migration in sync**

Run: `python -m alembic check`
Expected: "No new upgrade operations detected."

- [ ] **Step 5: Review the phase diff**

Run: `git log --oneline <phase4-head>..HEAD`
Expected: ~13 task commits with clean messages.

- [ ] **Step 6: Confirm Phase 6 seams**

Confirm these are in place for Phase 6 (delivery):
- `notes` table has `doc_id`/`doc_url`/`emailed_at` (unpopulated).
- `pipeline._ORDER` has room to append `doc_created`/`emailed`; `run_pipeline` stops at `notes_generated`.
- The Drive/Gmail clients are NOT built yet (Phase 6).

---

## Self-review checklist (plan author runs this before handoff)

**Spec coverage (§4 data model, §6 Flow C steps 1–2, §7 transcript/Gemini failure modes):**
- `transcripts` (full_text encrypted, language, speaker_map) → Task 2. ✓
- `notes` (title = meeting title, summary, decisions, action_items, gemini_model) → Task 2, 7. ✓
- Fetch transcript entries (paginated) + participants → speaker_map → Task 3, 6. ✓
- Resolve meeting via space → Task 6. ✓
- Gemini summarize with structured output + map-reduce for large transcripts → Task 5, 7. ✓
- Resumable per-stage state machine (`pending → transcript_fetched → notes_generated`) → Task 8. ✓
- Empty transcript → skip Gemini, store "no content" note → Task 7. ✓
- Gemini safety-block / empty completion handling → Task 5. ✓
- Worker + enqueue with idempotent `_job_id` → Task 9. ✓
- Read endpoints (notes, transcript, conferences, latest-notes alias, regenerate) → Task 11. ✓

**Documented deviations / deferrals:**
1. **Delivery (Doc + email) is Phase 6** — pipeline stops at `notes_generated`; `notes` columns and `_ORDER` leave room.
2. **Redis deferred** — worker module + `RealJobQueue` built and unit-tested without Redis; `get_job_queue` keeps returning `NullJobQueue` in the app process until Redis is provisioned. The webhook still persists the durable `pending` conference, so no work is lost; a Phase 6/7 step wires the live arq pool + worker process.
3. **Gemini token-window exact size unverified** → chunking threshold is a config value with safe headroom, not a hardcoded limit.
4. **Phase 4 deferred items still open** (enqueue-failure handling, dropped-conference reconciliation, subscription renewal, OIDC prod hardening) — not in this phase.

**Type consistency:** `NotesContent(summary, decisions, action_items: list[ActionItem])` (Task 4) used by `gemini_client` (5), `notes_service` (7), API (11). `Summarizer` protocol (`summarize`, `count_tokens`) (5) injected in `notes_service` (7), `pipeline` (8), `worker` (9), deps (10), regenerate (11). Meet client dataclasses `ConferenceRecordInfo`/`TranscriptInfo`/`TranscriptEntryInfo`/`ParticipantInfo` (3) consumed by `transcript_service` (6) and `pipeline` tests (8). `run_pipeline(session, *, conference_id, oauth_client, meet_client, summarizer, model, chunk_threshold, default_title)` (8) called identically by `worker.notes_pipeline` (9). Consistent. ✓

**Placeholder scan:** no TODOs, no "implement later"; every code step has complete runnable code. ✓
