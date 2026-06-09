# Meet + Calendar + Gemini Notes — Design Spec

**Date:** 2026-06-09
**Status:** Approved design (pre-implementation)

## 1. Problem statement

Build a **multi-tenant API service** that connects Google Calendar, Google Meet, and the
Gemini API to automatically produce meeting notes.

Users connect their Google account, create meetings through our API, and decide per meeting
whether AI notes should be generated. When an enabled meeting ends, the system fetches the
Meet transcript, summarizes it with Gemini, writes a Google Doc titled with the meeting
title, and emails it to the participants.

### Goals
- Create a meeting (Calendar event + Meet link) via API and carry a per-meeting config flag
  (`notes_enabled`) controlling whether notes are generated.
- When enabled, capture the meeting content (auto-transcript) and summarize it with Gemini.
- Deliver notes as a Google Doc (title = meeting title) and via email.
- Expose the whole flow as REST API endpoints.
- Be robust: idempotent, self-healing, resumable, secure.

### Non-goals
- Real-time/in-meeting features.
- Building our own meeting-recorder bot (we rely on Google's native transcript).
- Performance tracking or user evaluation of meeting participants (prohibited by Google's
  Meet API policy).

## 2. Critical constraints (verified June 2026)

- **Meet transcripts/recordings require Google Workspace Business Standard or higher.**
  Personal Gmail and Business Starter do not generate Meet transcripts, so there is nothing
  for the API to fetch. Every organizing user must be on an eligible plan; we capability-check
  on connect.
- **Transcript text is available directly from the Meet REST API** via
  `conferenceRecords.transcripts.entries.list` using the `meetings.space.created` (sensitive,
  not restricted) scope — no Drive download required. This lets us **avoid the restricted-scope
  CASA security assessment**.
- **Writing the notes Doc uses `drive.file`** (app-created files only, non-restricted).
- **Workspace Events subscriptions expire** — with resource-name-only payloads the maximum TTL
  is **7 days**, and Google advises against relying on expiry reminder events. We actively renew.
- **Transcript entries are deleted by Google 30 days after the conference.**
- OAuth app must be **published to Production** (testing-mode refresh tokens expire in 7 days).

### Chosen path
Path A — Workspace upgrade + native Meet REST API pipeline. (Alternatives considered: a
meeting-recorder bot that works on free Gmail, and personal Google AI Pro + Drive; both
rejected — bot is high-infra, AI Pro API access is fragile.)

## 3. Architecture

Modular monolith + background worker + scheduler (Approach 1). Chosen over serverless and
microservices for simplicity, testability, and low operational overhead; the worker can later
be lifted to serverless if scaling demands it.

```
   FastAPI app  ──►  Worker (arq)  ──►  Google Meet / Gemini / Drive / Gmail
        │                 ▲
        │ enqueue         │ retries (resume from last stage)
        ▼                 │
   PostgreSQL  ◄──────────┘   (users, oauth_connections, event_subscriptions,
        ▲                      meetings, conferences, transcripts, notes,
        │                      processed_events)
        │
   Scheduler: • subscription renewal/reactivation
              • retry sweeper / DLQ drain
              • token-health checks
              • retention cleanup

   Workspace Events API ──(Pub/Sub push, OIDC-verified)──► events/ webhook
```

### Tech stack
- **Python + FastAPI** (API), **arq** + **Redis** (worker queue), **PostgreSQL** (state),
  **Google API Python clients**, **Gemini SDK** (AI Studio or Vertex AI).

### Modules (each independently testable; all Google access funnels through `google/`)
- **auth/** — OAuth 2.0 connect flow, encrypted token storage, single-flight token refresh,
  scope management.
- **meetings/** — create-meeting endpoint, persists the config flag, lists/returns meeting state.
- **google/** — typed clients wrapping Calendar, Meet, Drive, Gmail. Isolates all Google calls.
- **notes/** — transcript → Gemini summarization (chunk + map-reduce for large transcripts).
- **delivery/** — renders notes into a Google Doc (title = meeting title) and sends email.
- **events/** — Pub/Sub push handler: verify OIDC, dedup, enqueue.
- **worker** — async pipeline with per-stage resumable state and retries.
- **scheduler** — subscription lifecycle, retry sweeper, token health, retention.

## 4. Data model (PostgreSQL)

Relationships:
```
users ──1:1── oauth_connections ──1:1── event_subscriptions
  │
  └──1:N── meetings ──1:N── conferences ──1:1── transcripts
                                  │
                                  └──1:1── notes
processed_events  (standalone dedup ledger)
```

### users — accounts that log into our API
- `id` (uuid, pk), `email` (unique), `name`, `created_at`
- API auth via issued API key / JWT.

### oauth_connections — the linked Google account per user
- `id`, `user_id` (fk), `google_email`
- `refresh_token_encrypted` (bytea, encrypted at rest), `access_token_cache`,
  `access_token_expiry`
- `granted_scopes` (text[]), `status` (`active` | `needs_reconnect`), `created_at`, `updated_at`

### event_subscriptions — the Workspace Events subscription to keep alive
- `id`, `oauth_connection_id` (fk), `subscription_name`
- `expire_time`, `state` (`active` | `suspended`), `last_renewed_at`

### meetings — created via our API (maps to a Meet space + Calendar event)
- `id`, `user_id` (fk), `title`, `description`, `start_time`, `end_time`, `attendees` (jsonb)
- `calendar_event_id`, `meet_space_name`, `meet_join_uri`, `meeting_code`
- `notes_enabled` (bool — the config flag), `notes_config` (jsonb: language, style,
  extra_recipients)
- `created_at`, `updated_at`

### conferences — one row per actual occurrence (idempotency anchor)
- `id`, `meeting_id` (fk), `conference_record_name` (**unique**)
- `actual_start_time`, `actual_end_time`, `transcript_resource_name`
- `pipeline_state` (`pending` | `transcript_fetched` | `notes_generated` | `doc_created` |
  `emailed` | `failed`)
- `attempts` (int), `last_error`, `created_at`, `updated_at`

### transcripts — fetched transcript (separate for retention/deletion control)
- `id`, `conference_id` (fk), `full_text` (encrypted), `language`,
  `speaker_map` (jsonb: participant→name), `fetched_at`

### notes — generated output
- `id`, `conference_id` (fk), `title` (= meeting title), `summary`, `decisions` (jsonb),
  `action_items` (jsonb)
- `gemini_model`, `doc_id`, `doc_url`, `emailed_at`, `created_at`

### processed_events — at-least-once Pub/Sub dedup ledger
- `id`, `message_id` (**unique**), `event_type`, `conference_record_name`, `received_at`

### Key design points
- Idempotency enforced at DB level: unique `conference_record_name` and unique `message_id`.
- Encryption at rest for `refresh_token_encrypted` and `transcripts.full_text` (KMS envelope).
- Retention job nulls `transcripts.full_text` after the configured window; delete endpoint
  cascades `conferences → transcripts/notes`.
- Indexes: `conferences(conference_record_name)`, `meetings(user_id)`,
  `conferences(pipeline_state)`, `event_subscriptions(expire_time)`.

## 5. API endpoints

Base path `/v1`. App endpoints require Bearer auth; Google-facing callback/webhook endpoints
are public but verified (signed OAuth `state` + Pub/Sub OIDC).

### Auth & account linking
| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/auth/register` | Create app user, returns API token |
| POST | `/v1/auth/login` | Get an API token |
| GET | `/v1/connections/google/start` | Returns Google OAuth consent URL (signed `state`) |
| GET | `/v1/connections/google/callback` | Exchange code, store encrypted token, create Events subscription |
| GET | `/v1/connections/google` | Connection status (active/needs_reconnect, scopes, google_email) |
| DELETE | `/v1/connections/google` | Revoke token + delete subscription |

### Meetings
**POST /v1/meetings** — request:
```json
{
  "title": "Q3 Roadmap Sync",
  "description": "Planning the next quarter",
  "start_time": "2026-06-12T10:00:00+05:30",
  "end_time": "2026-06-12T11:00:00+05:30",
  "attendees": ["alice@acme.com", "bob@acme.com"],
  "notes_enabled": true,
  "notes_config": {
    "language": "en",
    "style": "detailed",
    "extra_recipients": ["pm@acme.com"]
  }
}
```
Server-side: create Calendar event with Meet link; if `notes_enabled`, patch Meet space
settings → auto-transcript ON; persist meeting. Response:
```json
{
  "id": "mtg_abc123",
  "title": "Q3 Roadmap Sync",
  "meet_join_uri": "https://meet.google.com/abc-defg-hij",
  "calendar_event_id": "evt_...",
  "notes_enabled": true,
  "status": "scheduled"
}
```

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/meetings` | List meetings (filter status/date; paginated) |
| GET | `/v1/meetings/{id}` | Meeting detail incl. occurrences + notes status |
| PATCH | `/v1/meetings/{id}` | Update fields or toggle `notes_enabled` (re-patches Meet settings) |
| DELETE | `/v1/meetings/{id}` | Cancel meeting (removes Calendar event) |

### Notes & occurrences
| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/meetings/{id}/conferences` | List occurrences + pipeline_state |
| GET | `/v1/conferences/{id}/notes` | Get generated notes (summary, decisions, action_items, doc_url) |
| GET | `/v1/conferences/{id}/transcript` | Get raw transcript (if retained) |
| POST | `/v1/conferences/{id}/notes:regenerate` | Re-run Gemini |
| GET | `/v1/meetings/{id}/notes` | Convenience alias → notes for latest occurrence |

### System
| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/webhooks/google/events` | Pub/Sub push; OIDC-verified; dedup + enqueue |
| GET | `/healthz` | Liveness/readiness |

### Conventions
- **Async by design**: `POST /meetings` returns immediately; notes arrive later via the
  pipeline. Clients poll notes/`pipeline_state` (optional outbound webhook can be added later).
- **Errors**: JSON envelope `{ "error": { "code", "message", "details" } }`; `409` when
  connection `needs_reconnect`; `422` when the Google account lacks transcript capability.
- **Idempotency**: `POST /meetings` honors an `Idempotency-Key` header.
- **Pagination**: cursor-based (`?cursor=&limit=`).

## 6. End-to-end flow

### Flow A — Connect a Google account
1. `GET /v1/connections/google/start` → consent URL (scopes below; signed `state`).
2. User consents → redirect to `/callback` → exchange code → store encrypted refresh token.
3. Create Workspace Events subscription (events: `conferenceRecord.started`,
   `transcript.fileGenerated`; resource-name-only payload → 7-day TTL); save
   `subscription_name` + `expire_time`.

### Flow B — Create a meeting
1. `POST /v1/meetings`.
2. Calendar API inserts event with `conferenceData` → Meet link.
3. If `notes_enabled`: Meet API patches space settings → auto-transcript ON.
4. Persist meeting; return `meet_join_uri` + status.

### Flow C — Meeting happens → notes generated
1. Google sends Pub/Sub push to the webhook (OIDC JWT).
2. Verify JWT; insert `processed_events(message_id)` (dedup — duplicate → ack & stop).
3. Upsert `conferences(conference_record_name)`; enqueue job; ack 200 fast.
4. Worker (resumes from `pipeline_state`):
   1. Fetch transcript entries (paginated) + participants → assemble `full_text`
      → `transcript_fetched`.
   2. Gemini summarize (chunk + map-reduce if large) → `notes` (title = meeting title)
      → `notes_generated`.
   3. Drive (`drive.file`) create Doc titled with meeting title → `doc_created`.
   4. Gmail send to organizer + attendees + extra_recipients → `emailed` (done).
   Each stage commits before the next → resumable, no duplicate side effects.

### Flow D — Scheduler (continuous)
- **Renewal**: subscriptions near `expire_time` → patch `ttl=0s`; reactivate if suspended.
- **Sweeper**: conferences stuck non-terminal past threshold → re-enqueue; exhausted attempts
  → `failed` + DLQ + alert.
- **Tokens**: probe connections; on `invalid_grant` → `needs_reconnect`.
- **Retention**: null `transcripts.full_text` past the retention window.
- **Reconciliation**: if a subscription gap may have missed a meeting, list recent
  `conferenceRecords` and backfill.

## 7. Error handling & failure modes

### Account / capability
- Account lacks transcript capability → `422` at create time, store `notes_enabled=false`;
  capability cached per account on connect.
- No `transcript.fileGenerated` within expected window → sweeper marks `failed`
  (`no_transcript`).

### OAuth / tokens
- `invalid_grant`/revoked → `needs_reconnect`; pause jobs; `409` with reconnect URL.
- Missing/declined scopes → surface which scope; re-prompt.
- Single-flight token refresh (lock per connection).

### Event delivery
- Duplicates → dropped via `message_id` unique constraint.
- Out-of-order events → upsert `conferences` (order-independent).
- Bad OIDC JWT → `401`, never enqueue.
- Unmappable event → ack `200`, ignore.
- Subscription expired/suspended → renew/reactivate + reconciliation backfill.

### Transcript fetch
- Not ready despite event → backoff retry.
- Large transcript → paginate + stream-assemble.
- Expired (>30 days) → `failed` (`transcript_expired`).
- Empty transcript → skip Gemini, mark `emailed` with "no content captured".

### Gemini
- 429/5xx → exponential backoff + jitter; capped retries; then `failed` (resumable via
  `:regenerate`).
- Context overflow → map-reduce.
- Safety block / empty → simpler-prompt fallback; else store transcript + flag for review.
- Malformed structured output → schema-validate; one corrective retry; else prose-only.

### Delivery
- Doc creation fails → retry; never email without a Doc.
- Gmail fails/partial → Doc already exists; retry email only; per-recipient tracking.
- Quota exhaustion → backoff + alert.

### Data / concurrency
- Concurrent workers → `SELECT … FOR UPDATE SKIP LOCKED` on job claim.
- Recurring meetings → per-`conferenceRecord` independence.
- Meeting deleted mid-pipeline → check cancellation, abort cleanly.

### Cross-cutting
- Structured logging + per-conference correlation IDs.
- Metrics/alerts: subscription health, pipeline success rate, DLQ depth, Gemini latency/errors.
- DLQ for permanent failures, drainable via `:regenerate`.
- Graceful degradation: transcripts persisted before Gemini, so outages never lose data.

## 8. Google Cloud setup & prerequisites

### Accounts & plans
1. Workspace Business Standard+ for organizing accounts (capability-checked on connect).
2. A Google Cloud project for OAuth app, Pub/Sub, and API enablement.

### Cloud project
3. Enable APIs: Calendar, Meet, Workspace Events, Drive, Gmail, Pub/Sub, Gemini (AI Studio or
   Vertex AI).
4. OAuth consent screen: External, **published to Production**; register scopes
   `meetings.space.created`, `meetings.space.settings`, `calendar.events`, `drive.file`,
   `gmail.send`. Requires Google app verification (sensitive scopes) but **not** CASA.
5. OAuth web client; redirect URI `…/v1/connections/google/callback`.

### Pub/Sub
6. Topic `meet-events`; grant the Workspace Events service account publish rights.
7. Push subscription → `…/v1/webhooks/google/events` with OIDC auth (dedicated service account).
8. Dead-letter topic + DLQ subscription.
9. Per-user Workspace Events subscriptions are created in code at connect time, pointing at the
   topic.

### Gemini
10. AI Studio API key or Vertex AI; store in secret manager.
11. Use a long-context Gemini model for summarization.

### Secrets, security, infra
12. Secret manager for OAuth client secret, Gemini key, DB creds, KMS encryption key.
13. PostgreSQL (Cloud SQL or equivalent).
14. Redis for the arq queue.
15. Hosting (Cloud Run or container host); HTTPS required.
16. Domain + TLS for callback/webhook.

### Local dev
17. Tunnel (ngrok) for webhook/redirect.
18. Test Workspace account on an eligible plan for end-to-end testing.

### Cost awareness
19. Workspace seats (per organizer), Gemini usage (scales with transcript length), minor
    Pub/Sub/Cloud Run/SQL costs.

## 9. Testing strategy
- **Unit**: each module against fakes; `google/` clients mocked so business logic is testable
  without live APIs.
- **Idempotency tests**: duplicate Pub/Sub messages, redelivered events, re-run pipelines →
  exactly one Doc/email.
- **Resumability tests**: kill the worker between each pipeline stage → resumes correctly.
- **Failure-injection**: Gemini 429, Drive/Gmail errors, expired tokens, expired transcripts.
- **Integration**: end-to-end against a real test Workspace account on an eligible plan.

## 10. Open decisions (sensible defaults chosen, revisit if needed)
- Stack defaulted to Python/FastAPI (swap to Node/TS if preferred).
- Gemini via AI Studio key initially; Vertex AI later for enterprise quotas.
- Notes delivered as Doc + email (DB persisted internally; a GET notes endpoint exists for
  retrieval/debugging).
- Retention window defaults to **30 days** (matches Google's own transcript-entry deletion),
  configurable per deployment.
