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
    decisions: list[str]
    action_items: list[ActionItem]
    doc_url: str | None = None
    created_at: datetime


class TranscriptResponse(BaseModel):
    conference_id: uuid.UUID
    language: str | None
    text: str
    speaker_map: dict[str, str]


class ConferenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    meeting_id: uuid.UUID | None
    conference_record_name: str
    pipeline_state: str
    attempts: int
    last_error: str | None = None
    created_at: datetime
