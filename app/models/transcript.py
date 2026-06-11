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
