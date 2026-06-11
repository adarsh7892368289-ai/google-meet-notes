from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

MEET_BASE = "https://meet.googleapis.com/v2"


@dataclass
class ConferenceRecordInfo:
    name: str
    space: str | None
    start_time: str | None
    end_time: str | None


@dataclass
class TranscriptInfo:
    name: str
    state: str


@dataclass
class TranscriptEntryInfo:
    participant: str | None
    text: str
    language_code: str | None


@dataclass
class ParticipantInfo:
    name: str
    display_name: str


class MeetClient(Protocol):
    async def get_space_name(self, access_token: str, meeting_code: str) -> str: ...
    async def enable_auto_transcript(self, access_token: str, space_name: str) -> None: ...
    async def get_conference_record(
        self, access_token: str, conference_record_name: str
    ) -> ConferenceRecordInfo: ...
    async def get_transcript(
        self, access_token: str, transcript_resource_name: str
    ) -> TranscriptInfo: ...
    async def list_transcript_entries(
        self, access_token: str, transcript_resource_name: str
    ) -> list[TranscriptEntryInfo]: ...
    async def list_participants(
        self, access_token: str, conference_record_name: str
    ) -> list[ParticipantInfo]: ...


class GoogleMeetClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def _http(self, access_token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=30.0,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def get_space_name(self, access_token: str, meeting_code: str) -> str:
        async with self._http(access_token) as http:
            resp = await http.get(f"{MEET_BASE}/spaces/{meeting_code}")
            resp.raise_for_status()
            return resp.json()["name"]

    async def enable_auto_transcript(self, access_token: str, space_name: str) -> None:
        body = {
            "config": {
                "artifactConfig": {
                    "transcriptionConfig": {"autoTranscriptionGeneration": "ON"}
                }
            }
        }
        async with self._http(access_token) as http:
            resp = await http.patch(
                f"{MEET_BASE}/{space_name}",
                params={"updateMask": "config.artifactConfig"},
                json=body,
            )
            resp.raise_for_status()

    async def get_conference_record(
        self, access_token: str, conference_record_name: str
    ) -> ConferenceRecordInfo:
        async with self._http(access_token) as http:
            resp = await http.get(f"{MEET_BASE}/{conference_record_name}")
            resp.raise_for_status()
            data = resp.json()
            return ConferenceRecordInfo(
                name=data["name"],
                space=data.get("space"),
                start_time=data.get("startTime"),
                end_time=data.get("endTime"),
            )

    async def get_transcript(
        self, access_token: str, transcript_resource_name: str
    ) -> TranscriptInfo:
        async with self._http(access_token) as http:
            resp = await http.get(f"{MEET_BASE}/{transcript_resource_name}")
            resp.raise_for_status()
            data = resp.json()
            return TranscriptInfo(
                name=data["name"], state=data.get("state", "STATE_UNSPECIFIED")
            )

    async def list_transcript_entries(
        self, access_token: str, transcript_resource_name: str
    ) -> list[TranscriptEntryInfo]:
        entries: list[TranscriptEntryInfo] = []
        page_token: str | None = None
        async with self._http(access_token) as http:
            while True:
                params = {"pageSize": 100}
                if page_token:
                    params["pageToken"] = page_token
                resp = await http.get(
                    f"{MEET_BASE}/{transcript_resource_name}/entries", params=params
                )
                resp.raise_for_status()
                data = resp.json()
                for e in data.get("transcriptEntries", []):
                    entries.append(
                        TranscriptEntryInfo(
                            participant=e.get("participant"),
                            text=e.get("text", ""),
                            language_code=e.get("languageCode"),
                        )
                    )
                page_token = data.get("nextPageToken")
                if not page_token:
                    return entries

    async def list_participants(
        self, access_token: str, conference_record_name: str
    ) -> list[ParticipantInfo]:
        participants: list[ParticipantInfo] = []
        page_token: str | None = None
        async with self._http(access_token) as http:
            while True:
                params = {"pageSize": 250}
                if page_token:
                    params["pageToken"] = page_token
                resp = await http.get(
                    f"{MEET_BASE}/{conference_record_name}/participants", params=params
                )
                resp.raise_for_status()
                data = resp.json()
                for p in data.get("participants", []):
                    user = (
                        p.get("signedinUser")
                        or p.get("anonymousUser")
                        or p.get("phoneUser")
                        or {}
                    )
                    participants.append(
                        ParticipantInfo(
                            name=p.get("name", ""), display_name=user.get("displayName", "")
                        )
                    )
                page_token = data.get("nextPageToken")
                if not page_token:
                    return participants
