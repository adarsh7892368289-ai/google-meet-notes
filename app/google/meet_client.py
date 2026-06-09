from __future__ import annotations

from typing import Protocol

import httpx

MEET_BASE = "https://meet.googleapis.com/v2"


class MeetClient(Protocol):
    async def get_space_name(self, access_token: str, meeting_code: str) -> str: ...
    async def enable_auto_transcript(self, access_token: str, space_name: str) -> None: ...


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
