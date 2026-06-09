import httpx

from app.google.meet_client import GoogleMeetClient


def _client(handler) -> GoogleMeetClient:
    return GoogleMeetClient(transport=httpx.MockTransport(handler))


async def test_get_space_name_resolves_canonical_name():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/spaces/abc-defg-hij"
        assert request.headers["Authorization"] == "Bearer at-1"
        return httpx.Response(200, json={"name": "spaces/SERVERID", "meetingCode": "abc-defg-hij"})

    name = await _client(handler).get_space_name("at-1", "abc-defg-hij")
    assert name == "spaces/SERVERID"


async def test_enable_auto_transcript_patches_artifact_config():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["mask"] = request.url.params.get("updateMask")
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"name": "spaces/SERVERID"})

    await _client(handler).enable_auto_transcript("at-1", "spaces/SERVERID")
    assert captured["method"] == "PATCH"
    assert captured["path"] == "/v2/spaces/SERVERID"
    assert captured["mask"] == "config.artifactConfig"
    assert "autoTranscriptionGeneration" in captured["body"]
    assert "ON" in captured["body"]


async def test_enable_auto_transcript_raises_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "not supported"}})

    import pytest

    with pytest.raises(httpx.HTTPStatusError):
        await _client(handler).enable_auto_transcript("at-1", "spaces/SERVERID")
