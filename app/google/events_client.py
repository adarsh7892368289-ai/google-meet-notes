from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

EVENTS_BASE = "https://workspaceevents.googleapis.com/v1"

MEET_EVENT_TYPES = [
    "google.workspace.meet.conference.v2.started",
    "google.workspace.meet.transcript.v2.fileGenerated",
]


@dataclass
class SubscriptionResult:
    subscription_name: str
    expire_time: str | None
    state: str


class EventsClient(Protocol):
    async def create_subscription(
        self, access_token: str, *, google_user_id: str, topic: str, ttl_seconds: int
    ) -> SubscriptionResult: ...
    async def renew_subscription(
        self, access_token: str, *, subscription_name: str, ttl_seconds: int
    ) -> SubscriptionResult: ...
    async def delete_subscription(
        self, access_token: str, *, subscription_name: str
    ) -> None: ...


def _parse_subscription(payload: dict) -> SubscriptionResult:
    sub = payload.get("response", payload)
    return SubscriptionResult(
        subscription_name=sub["name"],
        expire_time=sub.get("expireTime"),
        state=sub.get("state", "STATE_UNSPECIFIED"),
    )


class GoogleEventsClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    def _http(self, access_token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=30.0,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def create_subscription(
        self, access_token: str, *, google_user_id: str, topic: str, ttl_seconds: int
    ) -> SubscriptionResult:
        body = {
            "targetResource": f"//cloudidentity.googleapis.com/users/{google_user_id}",
            "eventTypes": MEET_EVENT_TYPES,
            "notificationEndpoint": {"pubsubTopic": topic},
            "payloadOptions": {"includeResource": False},
            "ttl": f"{ttl_seconds}s",
        }
        async with self._http(access_token) as http:
            resp = await http.post(f"{EVENTS_BASE}/subscriptions", json=body)
            resp.raise_for_status()
            return _parse_subscription(resp.json())

    async def renew_subscription(
        self, access_token: str, *, subscription_name: str, ttl_seconds: int
    ) -> SubscriptionResult:
        body = {"ttl": f"{ttl_seconds}s"}
        async with self._http(access_token) as http:
            resp = await http.patch(
                f"{EVENTS_BASE}/{subscription_name}",
                params={"updateMask": "ttl"},
                json=body,
            )
            resp.raise_for_status()
            return _parse_subscription(resp.json())

    async def delete_subscription(
        self, access_token: str, *, subscription_name: str
    ) -> None:
        async with self._http(access_token) as http:
            resp = await http.delete(f"{EVENTS_BASE}/{subscription_name}")
            if resp.status_code not in (200, 204, 404, 410):
                resp.raise_for_status()
