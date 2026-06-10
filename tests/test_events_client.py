import httpx
import pytest

from app.google.events_client import GoogleEventsClient


def _client(handler) -> GoogleEventsClient:
    return GoogleEventsClient(transport=httpx.MockTransport(handler))


async def test_create_subscription_posts_user_target_and_event_types():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        captured["auth"] = request.headers["Authorization"]
        return httpx.Response(
            200,
            json={
                "done": True,
                "response": {
                    "name": "subscriptions/sub-1",
                    "expireTime": "2026-06-20T00:00:00Z",
                    "state": "ACTIVE",
                },
            },
        )

    result = await _client(handler).create_subscription(
        "at-1",
        google_user_id="108200001",
        topic="projects/p/topics/meet-events",
        ttl_seconds=604800,
    )
    assert captured["method"] == "POST"
    assert captured["url"] == "https://workspaceevents.googleapis.com/v1/subscriptions"
    assert captured["auth"] == "Bearer at-1"
    assert "//cloudidentity.googleapis.com/users/108200001" in captured["body"]
    assert "google.workspace.meet.transcript.v2.fileGenerated" in captured["body"]
    assert "google.workspace.meet.conference.v2.started" in captured["body"]
    assert "604800s" in captured["body"]
    assert result.subscription_name == "subscriptions/sub-1"
    assert result.state == "ACTIVE"


async def test_create_subscription_unwraps_operation_without_response_block():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"name": "operations/op-1", "done": True,
                  "response": {"name": "subscriptions/sub-2",
                               "expireTime": "2026-06-21T00:00:00Z", "state": "ACTIVE"}},
        )

    result = await _client(handler).create_subscription(
        "at-1", google_user_id="1", topic="projects/p/topics/t", ttl_seconds=604800
    )
    assert result.subscription_name == "subscriptions/sub-2"


async def test_renew_subscription_patches_ttl():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={"done": True, "response": {
                "name": "subscriptions/sub-1",
                "expireTime": "2026-06-27T00:00:00Z", "state": "ACTIVE"}},
        )

    result = await _client(handler).renew_subscription(
        "at-1", subscription_name="subscriptions/sub-1", ttl_seconds=604800
    )
    assert captured["method"] == "PATCH"
    assert "subscriptions/sub-1" in captured["url"]
    assert "updateMask=ttl" in captured["url"]
    assert "604800s" in captured["body"]
    assert result.subscription_name == "subscriptions/sub-1"


async def test_delete_subscription_calls_delete():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(200, json={})

    await _client(handler).delete_subscription("at-1", subscription_name="subscriptions/sub-1")
    assert captured["method"] == "DELETE"
    assert captured["url"].endswith("/v1/subscriptions/sub-1")


async def test_delete_subscription_ignores_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "not found"}})

    await _client(handler).delete_subscription("at-1", subscription_name="subscriptions/gone")
