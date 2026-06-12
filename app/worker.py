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
