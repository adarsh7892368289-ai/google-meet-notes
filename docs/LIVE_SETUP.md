# Live Setup Runbook (local dev + ngrok)

How to run the **full live workflow** on your own machine: a user logs in, connects a real
Google account, creates a real meeting, the meeting is recorded, and notes are generated
automatically. This targets **local development** — everything runs on your laptop, with
**ngrok** exposing your local server to Google so OAuth redirects and the "transcript ready"
push can reach it.

> **Automatic notes are wired up.** When `REDIS_URL` is set in `.env`, the API connects to
> Redis at startup and the webhook auto-enqueues note generation to the `arq` worker — so a real
> meeting's transcript generates notes hands-free (you just need the worker running, Step 8). If
> `REDIS_URL` is empty, the webhook still records a durable `pending` conference (no work lost),
> and you can finish it with the manual trigger in [Step 9](#step-9-temporary--generate-notes-the-manual-trigger).

> **Hard prerequisite:** any Google account that *creates* meetings must be on **Google
> Workspace Business Standard or higher**. Free/personal Gmail does **not** produce Meet
> transcripts, so notes can never be generated for it.

---

## Overview of the moving parts

```
                         ngrok (public HTTPS)  ──►  your local FastAPI (uvicorn)
   Google OAuth  ───────────────────────────────────►  /v1/connections/google/callback
   Google Workspace Events ──(Pub/Sub push)──────────►  /v1/webhooks/google/events
                                                              │ enqueue
   PostgreSQL  ◄───────────────  FastAPI / worker  ──────────┘
   Redis  ◄─────────────────────  arq worker  ──►  Google Meet API + Gemini API
```

You will run, locally and simultaneously:
1. **PostgreSQL** (already set up)
2. **Redis**
3. **ngrok** (public tunnel to your local port)
4. **The API** (`uvicorn app.main:app`)
5. **The worker** (`arq app.worker.WorkerSettings`)

---

## Step 1 — Google Cloud project + APIs

1. Go to https://console.cloud.google.com → create a project (e.g. `meet-notes-dev`).
2. **APIs & Services → Library** → enable each of:
   - Google Calendar API
   - Google Meet API
   - Google Workspace Events API
   - Google Drive API
   - Gmail API
   - Cloud Pub/Sub API
3. (The Gemini key is separate — see Step 4.)

## Step 2 — Start ngrok and note your public URL

You need a public HTTPS URL before configuring OAuth/Pub-Sub, because Google must reach your
machine.

```bash
# install ngrok (https://ngrok.com/download), authenticate once, then:
ngrok http 8000
```

ngrok prints a forwarding URL like `https://abc123.ngrok-free.app`. **Keep this terminal open**;
the URL changes each restart on the free plan (you'd then have to update the OAuth redirect and
re-create the Pub/Sub push subscription). Call this `PUBLIC_URL` below.

## Step 3 — OAuth consent screen + web client

1. **APIs & Services → OAuth consent screen**:
   - User type: **External** (or **Internal** if your Workspace org allows — simpler, no
     verification needed).
   - Add the **scopes** this app uses (must match `app/config.py` `google_scopes` exactly):
     ```
     openid
     email
     https://www.googleapis.com/auth/meetings.space.created
     https://www.googleapis.com/auth/meetings.space.settings
     https://www.googleapis.com/auth/calendar.events
     https://www.googleapis.com/auth/drive.file
     https://www.googleapis.com/auth/gmail.send
     ```
   - Add your own Workspace account as a **Test user** (so you can consent without full app
     verification while developing).
2. **APIs & Services → Credentials → Create credentials → OAuth client ID → Web application**:
   - **Authorized redirect URI**: `PUBLIC_URL/v1/connections/google/callback`
     (e.g. `https://abc123.ngrok-free.app/v1/connections/google/callback`).
   - Save the **Client ID** and **Client secret**.

> Note on tokens: in "Testing" mode, Google refresh tokens expire after 7 days. Fine for dev;
> for anything longer-lived, publish the OAuth app to Production (needs verification because the
> scopes are sensitive).

## Step 4 — Gemini API key

1. Go to https://aistudio.google.com/apikey → create an API key.
2. Save it for `GEMINI_API_KEY` below.

## Step 5 — Redis

Install and run Redis locally (any of these):
- **Windows**: install **Memurai** (Redis-compatible) or run Redis under WSL/Docker.
- **Mac/Linux**: `brew install redis && brew services start redis`, or `docker run -p 6379:6379 redis`.

Confirm it responds: `redis-cli ping` → `PONG`. Connection string: `redis://localhost:6379`.

## Step 6 — Pub/Sub topic + push subscription

The Workspace Events subscription (created automatically when a user connects) publishes to a
**Pub/Sub topic**; a **push subscription** on that topic forwards events to your webhook.

1. **Pub/Sub → Topics → Create topic**: id `meet-events`.
   Full name: `projects/<PROJECT_ID>/topics/meet-events` — this is `WORKSPACE_EVENTS_TOPIC`.
2. Grant the **Workspace Events service account** permission to publish to it. Add this principal
   as a **Pub/Sub Publisher** on the topic:
   `meet-api-event-push@system.gserviceaccount.com`
   (this is Google's service account that delivers Meet events).
3. **Create a push subscription** on `meet-events`:
   - Delivery type: **Push**.
   - Endpoint URL: `PUBLIC_URL/v1/webhooks/google/events`.
   - **Enable authentication** → choose/create a **service account** (e.g.
     `pubsub-push@<PROJECT_ID>.iam.gserviceaccount.com`). This is `PUSH_SERVICE_ACCOUNT_EMAIL`.
   - (Optional) set an **audience**; if you do, it becomes `PUSH_AUDIENCE`. If left default, the
     audience is the push endpoint URL.
4. Give that push service account the **Service Account Token Creator** role if Pub/Sub prompts
   for it.

> **Local-dev shortcut:** the webhook verifies the push token via `PUSH_AUDIENCE` /
> `PUSH_SERVICE_ACCOUNT_EMAIL`. If you leave both **empty** in `.env`, the app skips those
> checks (it still requires a Google-signed token). That's acceptable for a local proof; for
> anything real, set them. (This is the documented Phase-7 hardening item.)

## Step 7 — Fill in `.env`

Copy `.env.example` to `.env` and set:

```ini
APP_ENV=local
JWT_SECRET=<long random string>
DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/meetnotes
ENCRYPTION_KEY=<output of: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">

GOOGLE_CLIENT_ID=<from Step 3>
GOOGLE_CLIENT_SECRET=<from Step 3>
GOOGLE_REDIRECT_URI=https://abc123.ngrok-free.app/v1/connections/google/callback   # PUBLIC_URL + callback path

WORKSPACE_EVENTS_TOPIC=projects/<PROJECT_ID>/topics/meet-events
PUSH_AUDIENCE=                       # set if you configured one in Step 6, else leave empty for dev
PUSH_SERVICE_ACCOUNT_EMAIL=          # the push SA from Step 6, or empty for dev

GEMINI_API_KEY=<from Step 4>
GEMINI_MODEL=gemini-2.5-flash

REDIS_URL=redis://localhost:6379
```

> `GOOGLE_REDIRECT_URI` must **exactly** equal the redirect URI you registered in Step 3,
> including the `https://` and path. A mismatch causes a `redirect_uri_mismatch` error at consent.

## Step 8 — Run everything

Four terminals (Postgres assumed already running, Redis from Step 5):

```bash
# Terminal A — migrate (once) + API
alembic upgrade head
uvicorn app.main:app --port 8000

# Terminal B — the worker (note: see the pending-wiring caveat at the top)
arq app.worker.WorkerSettings

# Terminal C — the public tunnel
ngrok http 8000
```

Sanity check: `curl https://abc123.ngrok-free.app/healthz` → `{"status":"ok"}`.

## Step 9 — Drive the full workflow as a user

All requests use the live ngrok base URL (or `http://localhost:8000` for the non-Google steps).

**a. Register + log in**
```bash
curl -X POST http://localhost:8000/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"you@yourworkspace.com","name":"You","password":"a-strong-password"}'
# → {"access_token":"<JWT>"}
```
Save the JWT as `TOKEN`. (Other people register the same way — each gets their own token.)

**b. Connect the Google account**
```bash
curl http://localhost:8000/v1/connections/google/start -H "Authorization: Bearer $TOKEN"
# → {"authorization_url":"https://accounts.google.com/o/oauth2/v2/auth?..."}
```
Open that URL in a browser, sign in with your **Workspace** account, approve. Google redirects
to your callback (via ngrok) and the connection is stored — and the Workspace Events
subscription is created automatically. Verify:
```bash
curl http://localhost:8000/v1/connections/google -H "Authorization: Bearer $TOKEN"
# → {"connected":true,"google_email":"...","status":"active",...}
```

**c. Create a meeting with notes ON**
```bash
curl -X POST http://localhost:8000/v1/meetings \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
        "title":"Q3 Roadmap Sync",
        "start_time":"2026-06-13T10:00:00+05:30",
        "end_time":"2026-06-13T11:00:00+05:30",
        "attendees":["colleague@yourworkspace.com"],
        "notes_enabled":true
      }'
# → {"id":"...","meet_join_uri":"https://meet.google.com/...","status":"scheduled"}
```

**d. Hold the meeting**
Join the `meet_join_uri` with at least one other person and talk for a couple of minutes so Meet
produces a transcript. End the meeting.

**e. Notes get generated**
A minute or two after the meeting ends, Google publishes "transcript ready" → Pub/Sub pushes it
to your webhook → the API enqueues the job → the `arq` worker fetches the transcript, summarizes
it with Gemini, and saves the notes. (Requires `REDIS_URL` set and the worker running, per Step 8.)

**f. Read the notes**
```bash
curl http://localhost:8000/v1/meetings/<MEETING_ID>/notes -H "Authorization: Bearer $TOKEN"
# → {"title":"Q3 Roadmap Sync","summary":"...","decisions":[...],"action_items":[...]}
```
Also useful: `GET /v1/meetings/<id>/conferences` shows each occurrence and its `pipeline_state`
(`pending` → `transcript_fetched` → `notes_generated`), so you can watch progress.

### Step 9 (temporary) — generate notes the manual trigger

Until the `get_job_queue` → Redis wiring lands, the webhook records the `pending` conference but
doesn't auto-run the worker. To finish the pipeline for a real conference right now, run the
pipeline directly against that conference id (you can find it via `GET /meetings/<id>/conferences`):

```bash
# one-off: run the pipeline for a specific real conference
python - <<'PY'
import asyncio, uuid
from app.config import get_settings
from app.db import SessionLocal, engine
from app.api.deps import get_oauth_client, get_meet_client, get_summarizer
from app.services import pipeline

CONFERENCE_ID = "REPLACE-WITH-CONFERENCE-UUID"

async def main():
    s = get_settings()
    async with SessionLocal() as session:
        await pipeline.run_pipeline(
            session, conference_id=uuid.UUID(CONFERENCE_ID),
            oauth_client=get_oauth_client(), meet_client=get_meet_client(),
            summarizer=get_summarizer(), model=s.gemini_model,
            chunk_threshold=s.gemini_chunk_token_threshold, default_title=s.notes_default_title,
        )
    await engine.dispose()

asyncio.run(main())
PY
```
This uses the **real** Google + Gemini clients against a **real** conference — so it requires a
real recorded meeting and a valid connection. (For a no-Google smoke test, use
`python scripts/proof_run.py` instead, which simulates the transcript.)

---

## How multi-user works (why anyone can just use it)

- **No pre-provisioning.** Anyone calls `POST /v1/auth/register` → they exist. Their JWT is their
  identity on every later request (`get_current_user` decodes it).
- **Per-user Google connection.** Each user runs the connect flow once; the app stores *their*
  encrypted token in `oauth_connections` and creates *their* events subscription. The app acts as
  each user using their own token.
- **Strict ownership.** Meetings are stamped with `user_id`; conferences/notes are reached only
  via `conference → connection → user_id`. User A literally cannot query user B's data — every
  endpoint filters by the caller's JWT. (Verified: a second user gets `404` on another's
  conference.)
- **Events route to the right user.** An incoming Meet event names the firing subscription, which
  maps to one connection → one user, and the conference is created under that user. The
  conference→meeting mapping is also scoped to the owning user.

So "make sure any user can log in, create meetings, and get notes" requires **no per-user work** —
it's the design. You only stand up the shared infrastructure once (this runbook); users self-serve
from there.

---

## Common pitfalls

- **`redirect_uri_mismatch`** at consent → `GOOGLE_REDIRECT_URI` in `.env` doesn't byte-for-byte
  match the URI registered on the OAuth client (and must use the current ngrok URL).
- **No transcript ever appears** → the account isn't on a Workspace plan that records Meet
  transcripts, or auto-transcript wasn't enabled (only happens when `notes_enabled:true` at
  creation), or the meeting was too short / had no speech.
- **Webhook never fires** → ngrok URL changed (re-register the push subscription endpoint), the
  Workspace Events service account lacks Publisher on the topic, or the subscription expired
  (max 7 days; renewal is a Phase-7 scheduler item — for dev, reconnect the account to recreate it).
- **`401`/`needs_reconnect`** on a user's calls → their Google refresh token was revoked or expired
  (7-day limit in OAuth "Testing" mode); have them reconnect.
- **Notes never generate despite a transcript** → the pending queue-wiring (see top); use the
  manual trigger in Step 9 for now.

---

## What "production" would add (not needed for this local runbook)

- The `NullJobQueue` → real arq-pool wiring so the webhook auto-triggers the worker.
- Set `PUSH_AUDIENCE` + `PUSH_SERVICE_ACCOUNT_EMAIL` and fail-fast if missing (security hardening).
- Publish the OAuth app to Production (longer-lived refresh tokens).
- The Phase-7 scheduler: renew event subscriptions before the 7-day expiry, sweep stuck/failed
  conferences, token-health checks, retention cleanup.
- Phase 6: deliver notes as a Google Doc + email (today notes are read via the API only).
- A stable public host (Cloud Run etc.) instead of ngrok.
```
