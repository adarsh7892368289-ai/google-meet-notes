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
