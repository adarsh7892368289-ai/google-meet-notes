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
        f"Action items: {'; '.join(f'{a.who}: {a.what}' if a.who else a.what for a in p.action_items)}"
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
