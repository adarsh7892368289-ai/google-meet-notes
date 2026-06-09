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
        if self.start_time.tzinfo is None or self.end_time.tzinfo is None:
            raise ValueError("start_time and end_time must be timezone-aware")
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
