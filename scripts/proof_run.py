"""Local end-to-end proof run for the notes pipeline.

Drives the REAL pipeline against the REAL local Postgres DB, simulating only the
one step that needs live Google: fetching the Meet transcript. It seeds a demo
user + connection + meeting + a `pending` conference (as if a meeting happened and
the webhook fired), then runs `pipeline.run_pipeline` to fetch (simulated) transcript
-> summarize -> persist notes, and prints a ready-to-run curl to read the notes back
over the live HTTP API.

Usage (from the repo root, venv active, .env configured, `alembic upgrade head` done):

    python scripts/proof_run.py

Set GEMINI_API_KEY in your environment/.env to use the REAL Gemini model; otherwise a
deterministic fake summarizer is used so the run works fully offline.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.config import get_settings
from app.crypto import decrypt
from app.db import SessionLocal, engine
from app.google.meet_client import (
    ConferenceRecordInfo,
    ParticipantInfo,
    TranscriptEntryInfo,
)
from app.google.oauth_client import TokenBundle
from app.models import Conference, Meeting, Notes, OAuthConnection, Transcript, User
from app.schemas.notes import ActionItem, NotesContent
from app.security import create_access_token
from app.services import connection_service, pipeline

DEMO_EMAIL = "demo@example.com"
DEMO_SPACE = "spaces/DEMO-PROOF-RUN"
DEMO_RECORD = "conferenceRecords/demo-proof-run"
DEMO_TRANSCRIPT = f"{DEMO_RECORD}/transcripts/t-demo"

# A simulated transcript: speaker resource name -> display name, and the spoken turns.
PARTICIPANTS = {
    f"{DEMO_RECORD}/participants/p1": "Alice",
    f"{DEMO_RECORD}/participants/p2": "Bob",
}
TURNS = [
    (f"{DEMO_RECORD}/participants/p1", "Welcome everyone. Today we're deciding the Q3 roadmap."),
    (f"{DEMO_RECORD}/participants/p2", "I think we should ship the billing revamp first."),
    (f"{DEMO_RECORD}/participants/p1", "Agreed. Let's commit to billing in Q3. Bob, can you draft the spec?"),
    (f"{DEMO_RECORD}/participants/p2", "Yes, I'll have a draft spec ready by next Friday."),
    (f"{DEMO_RECORD}/participants/p1", "Great. We'll also defer the mobile rewrite to Q4."),
]


class FakeOAuthClient:
    """Returns a fresh token so the pipeline never needs a real refresh."""

    async def refresh(self, refresh_token: str) -> TokenBundle:
        return TokenBundle(access_token="demo-access", expires_in=3599, scope="openid")


class FakeMeetClient:
    """Stands in for live Google Meet, returning the simulated transcript above."""

    async def get_conference_record(self, access_token, conference_record_name):
        return ConferenceRecordInfo(
            name=conference_record_name,
            space=DEMO_SPACE,
            start_time="2026-06-12T10:00:00Z",
            end_time="2026-06-12T11:00:00Z",
        )

    async def list_participants(self, access_token, conference_record_name):
        return [ParticipantInfo(name=n, display_name=d) for n, d in PARTICIPANTS.items()]

    async def list_transcript_entries(self, access_token, transcript_resource_name):
        return [
            TranscriptEntryInfo(participant=p, text=t, language_code="en-US")
            for p, t in TURNS
        ]


class FakeSummarizer:
    """Deterministic offline summarizer (used when GEMINI_API_KEY is not set)."""

    async def count_tokens(self, text: str) -> int:
        return len(text)

    async def summarize(self, transcript: str) -> NotesContent:
        return NotesContent(
            summary=(
                "The team reviewed the Q3 roadmap and aligned on priorities. "
                "(This summary was produced by the OFFLINE fake summarizer — set "
                "GEMINI_API_KEY to use the real Gemini model.)"
            ),
            decisions=[
                "Ship the billing revamp in Q3",
                "Defer the mobile rewrite to Q4",
            ],
            action_items=[ActionItem(who="Bob", what="Draft the billing spec by next Friday")],
        )


def _build_summarizer(settings):
    if settings.gemini_api_key:
        from google import genai

        from app.google.gemini_client import GeminiSummarizer

        client = genai.Client(api_key=settings.gemini_api_key)
        print(f"  Using REAL Gemini model: {settings.gemini_model}")
        return GeminiSummarizer(client=client, model=settings.gemini_model)
    print("  Using OFFLINE fake summarizer (no GEMINI_API_KEY set)")
    return FakeSummarizer()


async def _seed(session):
    """Idempotently create the demo user, connection, meeting, and a pending conference."""
    user = await session.scalar(select(User).where(User.email == DEMO_EMAIL))
    if user is None:
        user = User(email=DEMO_EMAIL, name="Demo User", hashed_password="x")
        session.add(user)
        await session.commit()
        await session.refresh(user)

    bundle = TokenBundle(
        access_token="demo-access", expires_in=3599, scope="openid", refresh_token="demo-refresh"
    )
    await connection_service.upsert_connection(
        session, user=user, bundle=bundle, google_email=DEMO_EMAIL, google_user_id="demo-uid"
    )

    meeting = await session.scalar(
        select(Meeting).where(Meeting.user_id == user.id, Meeting.meet_space_name == DEMO_SPACE)
    )
    if meeting is None:
        now = datetime.now(timezone.utc)
        meeting = Meeting(
            user_id=user.id,
            title="Q3 Roadmap Sync",
            description="Proof-run demo meeting",
            start_time=now,
            end_time=now + timedelta(hours=1),
            attendees=["alice@example.com", "bob@example.com"],
            meet_space_name=DEMO_SPACE,
            notes_enabled=True,
            notes_config={},
        )
        session.add(meeting)
        await session.commit()
        await session.refresh(meeting)

    conn = await session.scalar(
        select(OAuthConnection).where(OAuthConnection.user_id == user.id)
    )
    conf = await session.scalar(
        select(Conference).where(Conference.conference_record_name == DEMO_RECORD)
    )
    if conf is None:
        conf = Conference(
            oauth_connection_id=conn.id,
            conference_record_name=DEMO_RECORD,
            transcript_resource_name=DEMO_TRANSCRIPT,
            pipeline_state="pending",
        )
        session.add(conf)
    else:
        # Reset so a repeated run re-executes the full pipeline.
        conf.pipeline_state = "pending"
        conf.meeting_id = None
        conf.last_error = None
    await session.commit()
    await session.refresh(conf)
    return user, conf


async def main():
    settings = get_settings()
    if not settings.encryption_key:
        raise SystemExit(
            "ENCRYPTION_KEY is not set. Add it to your .env (generate with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")'
        )

    print("Notes pipeline — local end-to-end proof run")
    print("=" * 60)
    print(f"  Database: {settings.database_url.rsplit('@', 1)[-1]}")
    summarizer = _build_summarizer(settings)

    async with SessionLocal() as session:
        print("\n[1/3] Seeding demo user + connection + meeting + pending conference...")
        user, conf = await _seed(session)
        print(f"  user_id       = {user.id}")
        print(f"  conference_id = {conf.id}")
        print(f"  initial state = {conf.pipeline_state}")

        print("\n[2/3] Running the pipeline (transcript -> summarize -> persist notes)...")
        await pipeline.run_pipeline(
            session,
            conference_id=conf.id,
            oauth_client=FakeOAuthClient(),
            meet_client=FakeMeetClient(),
            summarizer=summarizer,
            model=settings.gemini_model,
            chunk_threshold=settings.gemini_chunk_token_threshold,
            default_title=settings.notes_default_title,
        )
        await session.refresh(conf)
        print(f"  final state   = {conf.pipeline_state}")

        transcript = await session.scalar(
            select(Transcript).where(Transcript.conference_id == conf.id)
        )
        notes = await session.scalar(select(Notes).where(Notes.conference_id == conf.id))

        print("\n[3/3] Result")
        print("-" * 60)
        print("Assembled transcript (decrypted, with speaker attribution):")
        for line in decrypt(transcript.full_text).splitlines():
            print(f"    {line}")
        print("\nGenerated notes:")
        print(f"    title       : {notes.title}")
        print(f"    summary     : {notes.summary}")
        print(f"    decisions   : {notes.decisions}")
        print(f"    action_items: {notes.action_items}")
        print(f"    model       : {notes.gemini_model}")

        token = create_access_token(str(user.id))

    await engine.dispose()

    print("\n" + "=" * 60)
    print("Now read the notes back over the LIVE HTTP API:")
    print("  1. In another terminal:  uvicorn app.main:app --port 8000")
    print("  2. Then run:\n")
    print(f'     curl -s http://localhost:8000/v1/conferences/{conf.id}/notes \\')
    print(f'       -H "Authorization: Bearer {token}"')
    print()


if __name__ == "__main__":
    asyncio.run(main())
