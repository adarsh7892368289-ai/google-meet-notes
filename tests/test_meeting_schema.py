from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.meeting import MeetingCreate


def test_meeting_create_rejects_naive_datetimes():
    with pytest.raises(ValidationError):
        MeetingCreate(
            title="X",
            start_time=datetime(2026, 6, 12, 10, 0),  # naive
            end_time=datetime(2026, 6, 12, 11, 0),     # naive
        )


def test_meeting_create_accepts_tz_aware():
    m = MeetingCreate(
        title="X",
        start_time=datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 6, 12, 11, 0, tzinfo=timezone.utc),
    )
    assert m.title == "X"
