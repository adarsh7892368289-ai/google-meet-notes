from datetime import datetime, timezone

import httpx

from app.google.calendar_client import CreatedEvent, GoogleCalendarClient


def _client(handler) -> GoogleCalendarClient:
    return GoogleCalendarClient(transport=httpx.MockTransport(handler))


async def test_create_event_parses_meet_link_and_code():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/calendars/primary/events")
        assert request.url.params.get("conferenceDataVersion") == "1"
        assert request.headers["Authorization"] == "Bearer at-1"
        body = request.content.decode()
        assert "hangoutsMeet" in body
        return httpx.Response(
            200,
            json={
                "id": "evt-123",
                "conferenceData": {
                    "conferenceId": "abc-defg-hij",
                    "status": {"statusCode": "success"},
                    "entryPoints": [
                        {"entryPointType": "video", "uri": "https://meet.google.com/abc-defg-hij"},
                        {"entryPointType": "phone", "uri": "tel:+1-111"},
                    ],
                },
            },
        )

    client = _client(handler)
    result = await client.create_event(
        "at-1",
        summary="Sync",
        description="desc",
        start=datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc),
        end=datetime(2026, 6, 12, 11, 0, tzinfo=timezone.utc),
        attendees=["a@acme.com", "b@acme.com"],
    )
    assert isinstance(result, CreatedEvent)
    assert result.event_id == "evt-123"
    assert result.meet_uri == "https://meet.google.com/abc-defg-hij"
    assert result.meeting_code == "abc-defg-hij"


async def test_create_event_handles_missing_conference():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "evt-x"})

    result = await _client(handler).create_event(
        "at-1",
        summary="S",
        description=None,
        start=datetime(2026, 6, 12, 10, 0, tzinfo=timezone.utc),
        end=datetime(2026, 6, 12, 11, 0, tzinfo=timezone.utc),
        attendees=[],
    )
    assert result.event_id == "evt-x"
    assert result.meet_uri is None
    assert result.meeting_code is None


async def test_delete_event_calls_delete():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(204)

    await _client(handler).delete_event("at-1", "evt-123")
    assert seen["method"] == "DELETE"
    assert seen["path"].endswith("/calendars/primary/events/evt-123")
