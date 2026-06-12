# Google Meet Notes

An API service that connects **Google Calendar + Google Meet + Gemini** to automatically
generate meeting notes. You create a meeting through the API with a "notes on/off" switch;
when the meeting ends, the service fetches the Meet transcript, summarizes it with Gemini, and
saves notes titled with the meeting's name. It is multi-tenant — many users can connect their
own Google accounts.

> Full design: [`docs/superpowers/specs/2026-06-09-meet-gemini-notes-design.md`](docs/superpowers/specs/2026-06-09-meet-gemini-notes-design.md).
> Phase-by-phase implementation plans live in [`docs/superpowers/plans/`](docs/superpowers/plans/).

---

## How it works (plain English)

The service is an assistant that does four things:

1. **You connect your Google account (once).** You log in, click "connect Google," and approve
   the permissions. The service stores an encrypted token and subscribes to Google so it gets
   pinged whenever one of your meetings produces a transcript.
2. **You create a meeting** through the API, including one switch — `notes_enabled` — that says
   whether you want AI notes for it. The service creates a real Calendar event + Meet link, and
   (if notes are on) turns on Meet's automatic transcript for that meeting.
3. **The meeting happens.** People join the Meet link and talk; Google records the transcript in
   the background. When it's ready, Google **automatically pings the service** — you do nothing.
4. **Notes get made.** Triggered by that ping, the service fetches the transcript (with real
   speaker names), sends it to Gemini for a summary + decisions + action items, and saves the
   notes titled with your meeting's name. You read them back through the API.

```
You  ── create meeting (notes: ON) ──►  Service ──► Google Calendar event + Meet link
                                                       (auto-transcript turned on)
        [the meeting happens]
Google ── "transcript ready" ping ──►  Service ──► fetch transcript ──► Gemini ──► saved notes
You  ── GET notes ──►  Service ──►  { title, summary, decisions, action_items }
```

---

## What's built vs. not yet

**Built and tested (178 tests passing):**
- User accounts (register / login, JWT auth)
- Connect a Google account via OAuth (encrypted token storage, auto-refresh)
- Create / list / get / delete meetings, with the per-meeting `notes_enabled` switch
- Receiving Google's OIDC-verified "transcript ready" push (dedup + idempotent)
- Fetching the transcript (with speaker attribution) and summarizing it with Gemini
- A resumable pipeline (`pending → transcript_fetched → notes_generated`)
- Read endpoints for notes, transcripts, occurrences, and on-demand re-generation

**Not yet built:**
- **Delivery (Phase 6):** writing the notes into a Google Doc and emailing attendees.
- **Production wiring:** the always-on background worker (needs Redis) and the Google Cloud
  Pub/Sub setup are scaffolded but not connected — see [Limitations](#limitations--prerequisites).

---

## Requirements to run end-to-end

These are hard prerequisites for *live* meetings (the test suite needs none of them — it uses
fakes and a local Postgres):

1. **Google Workspace Business Standard or higher** on any account that creates meetings.
   Personal/free Gmail **does not generate Meet transcripts**, so there would be nothing to
   summarize.
2. **A Google Cloud project** with OAuth credentials and the Calendar, Meet, Workspace Events,
   Drive, Gmail, and Pub/Sub APIs enabled (see the design doc, section 8).
3. **A Gemini API key** (Google AI Studio) for summarization.
4. **PostgreSQL** (state) and, for the live background worker, **Redis** + Cloud Pub/Sub.

---

## Local setup

Requires Python 3.12+ and a running PostgreSQL with `meetnotes` and `meetnotes_test` databases.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env    # then edit the values (see Configuration below)
```

Create the databases (once):

```powershell
psql -U postgres -c "CREATE DATABASE meetnotes"
psql -U postgres -c "CREATE DATABASE meetnotes_test"
```

Apply migrations and run the API:

```powershell
alembic upgrade head
uvicorn app.main:app --reload
```

Run the tests:

```powershell
pytest -q
```

Interactive API docs (once running): http://localhost:8000/docs

---

## Configuration (`.env`)

| Variable | What it's for |
|---|---|
| `APP_ENV` | `local` for dev. |
| `JWT_SECRET` | Secret for signing app login tokens. Use a long random string. |
| `DATABASE_URL` / `TEST_DATABASE_URL` | Postgres connection strings. |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OAuth web-client credentials from your Google Cloud project. |
| `GOOGLE_REDIRECT_URI` | Must match the redirect URI registered on the OAuth client. |
| `ENCRYPTION_KEY` | Fernet key encrypting stored Google tokens + transcripts. Generate with: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `WORKSPACE_EVENTS_TOPIC` | Pub/Sub topic for Meet events (e.g. `projects/<id>/topics/meet-events`). Leave empty in dev to skip subscriptions. |
| `PUSH_AUDIENCE` / `PUSH_SERVICE_ACCOUNT_EMAIL` | Verify the Pub/Sub push token. **Must be set in production** (empty disables those checks). |
| `GEMINI_API_KEY` | Google AI Studio key. Required for notes generation / regenerate. |
| `GEMINI_MODEL` | Summarization model (default `gemini-2.5-flash`). |
| `REDIS_URL` | Redis connection for the background worker (e.g. `redis://localhost:6379`). Empty = no live worker. |

---

## Using the API

All app endpoints need a Bearer token from register/login. Base path is `/v1`.

### 1. Create an account and log in

```
POST /v1/auth/register   { "email": "you@acme.com", "name": "You", "password": "..." }
POST /v1/auth/login      { "email": "you@acme.com", "password": "..." }
→ { "access_token": "<JWT>" }
```

Send `Authorization: Bearer <JWT>` on every call below.

### 2. Connect your Google account

```
GET /v1/connections/google/start   → { "authorization_url": "https://accounts.google.com/..." }
```
Open that URL, approve, and Google redirects to the callback, which stores your connection.
Check it with `GET /v1/connections/google`; disconnect with `DELETE /v1/connections/google`.

### 3. Create a meeting (the notes switch lives here)

```
POST /v1/meetings
{
  "title": "Q3 Roadmap Sync",
  "description": "Planning the next quarter",
  "start_time": "2026-06-13T10:00:00+05:30",
  "end_time":   "2026-06-13T11:00:00+05:30",
  "attendees": ["alice@acme.com", "bob@acme.com"],
  "notes_enabled": true
}
→ { "id": "...", "title": "...", "meet_join_uri": "https://meet.google.com/...", "status": "scheduled" }
```
Set `"notes_enabled": false` for a normal meeting with no notes.

### 4. After the meeting — get the notes

Notes are produced automatically once Google reports the transcript is ready. Then:

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/meetings` | List your meetings |
| GET | `/v1/meetings/{id}` | One meeting's details |
| GET | `/v1/meetings/{id}/conferences` | The occurrences of a meeting + their pipeline state |
| GET | `/v1/meetings/{id}/notes` | Notes for the latest occurrence |
| GET | `/v1/conferences/{id}/notes` | Notes for a specific occurrence |
| GET | `/v1/conferences/{id}/transcript` | The raw transcript (if retained) |
| POST | `/v1/conferences/{id}/notes:regenerate` | Re-run the summarization |
| DELETE | `/v1/meetings/{id}` | Cancel the meeting (removes the Calendar event) |

Notes look like:
```json
{
  "title": "Q3 Roadmap Sync",
  "summary": "The team agreed on the Q3 roadmap...",
  "decisions": ["Ship feature X in Q3"],
  "action_items": [{ "who": "Alice", "what": "Draft the spec" }]
}
```

### System endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/webhooks/google/events` | Google Pub/Sub push target (OIDC-verified) — not for end users |
| GET | `/healthz` | Liveness check |

---

## Limitations & prerequisites

- **Free Gmail won't work** — Meet transcripts require a paid Workspace plan (Business Standard+).
- **Notes generation needs a `GEMINI_API_KEY`.** Without one, `:regenerate` and the worker fail.
- **The automatic background trigger isn't wired up locally.** Receiving Google's push and
  generating notes is fully implemented and tested, but running it continuously needs Redis +
  Cloud Pub/Sub configured (`REDIS_URL`, `WORKSPACE_EVENTS_TOPIC`). The durable conference row is
  the source of truth, so no work is lost in the meantime.

---

## Project layout

```
app/
  api/routes/      HTTP endpoints (auth, connections, meetings, webhooks, notes, health)
  google/          typed clients for Google APIs (oauth, calendar, meet, events, gemini)
  models/          SQLAlchemy ORM models
  schemas/         Pydantic request/response models
  services/        business logic (connection, meeting, subscription, event, transcript, notes, pipeline)
  worker.py        arq background worker (notes pipeline task) — Redis deferred
  config.py        settings    crypto.py  encryption    db.py  database    queue.py  job-queue port
migrations/        Alembic database migrations
tests/             pytest suite (178 tests)
docs/              design spec + per-phase implementation plans
```
