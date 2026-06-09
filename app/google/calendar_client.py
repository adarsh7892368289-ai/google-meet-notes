from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlsplit

import httpx

CALENDAR_EVENTS_URI = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


@dataclass
class CreatedEvent:
    event_id: str
    meet_uri: str | None
    meeting_code: str | None
    conference_status: str


class CalendarClient(Protocol):
    async def create_event(
        self,
        access_token: str,
        *,
        summary: str,
        description: str | None,
        start: datetime,
        end: datetime,
        attendees: list[str],
    ) -> CreatedEvent: ...

    async def delete_event(self, access_token: str, event_id: str) -> None: ...


class GoogleCalendarClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def _http(self, access_token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=30.0,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def create_event(
        self,
        access_token: str,
        *,
        summary: str,
        description: str | None,
        start: datetime,
        end: datetime,
        attendees: list[str],
    ) -> CreatedEvent:
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
            "attendees": [{"email": e} for e in attendees],
            "conferenceData": {
                "createRequest": {
                    "requestId": str(uuid.uuid4()),
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            },
        }
        async with self._http(access_token) as http:
            resp = await http.post(
                CALENDAR_EVENTS_URI, params={"conferenceDataVersion": "1"}, json=body
            )
            resp.raise_for_status()
            data = resp.json()
        return _parse_event(data)

    async def delete_event(self, access_token: str, event_id: str) -> None:
        async with self._http(access_token) as http:
            resp = await http.delete(f"{CALENDAR_EVENTS_URI}/{event_id}")
            if resp.status_code not in (200, 204, 404, 410):
                resp.raise_for_status()


def _parse_event(data: dict) -> CreatedEvent:
    conf = data.get("conferenceData") or {}
    status = (conf.get("status") or {}).get("statusCode", "none")
    meet_uri = None
    for ep in conf.get("entryPoints", []):
        if ep.get("entryPointType") == "video":
            meet_uri = ep.get("uri")
            break
    meeting_code = None
    if meet_uri:
        path = urlsplit(meet_uri).path.rstrip("/")
        meeting_code = path.split("/")[-1] or None
    return CreatedEvent(
        event_id=data["id"],
        meet_uri=meet_uri,
        meeting_code=meeting_code,
        conference_status=status,
    )
