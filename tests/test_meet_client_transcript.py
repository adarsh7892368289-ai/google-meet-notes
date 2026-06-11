import httpx
import pytest

from app.google.meet_client import GoogleMeetClient


def _client(handler) -> GoogleMeetClient:
    return GoogleMeetClient(transport=httpx.MockTransport(handler))


async def test_get_conference_record_returns_space_and_times():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/conferenceRecords/cr-1"
        assert request.headers["Authorization"] == "Bearer at-1"
        return httpx.Response(200, json={
            "name": "conferenceRecords/cr-1",
            "space": "spaces/SERVERID",
            "startTime": "2026-06-11T10:00:00Z",
            "endTime": "2026-06-11T11:00:00Z",
        })

    rec = await _client(handler).get_conference_record("at-1", "conferenceRecords/cr-1")
    assert rec.space == "spaces/SERVERID"
    assert rec.start_time == "2026-06-11T10:00:00Z"
    assert rec.end_time == "2026-06-11T11:00:00Z"


async def test_get_transcript_returns_state():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/conferenceRecords/cr-1/transcripts/t-1"
        return httpx.Response(200, json={
            "name": "conferenceRecords/cr-1/transcripts/t-1",
            "state": "FILE_GENERATED",
        })

    t = await _client(handler).get_transcript("at-1", "conferenceRecords/cr-1/transcripts/t-1")
    assert t.state == "FILE_GENERATED"


async def test_list_transcript_entries_paginates():
    pages = {
        None: {"transcriptEntries": [
                   {"name": "e1", "participant": "conferenceRecords/cr-1/participants/p1",
                    "text": "hello", "languageCode": "en-US"}],
               "nextPageToken": "tok2"},
        "tok2": {"transcriptEntries": [
                     {"name": "e2", "participant": "conferenceRecords/cr-1/participants/p2",
                      "text": "hi", "languageCode": "en-US"}]},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/conferenceRecords/cr-1/transcripts/t-1/entries"
        token = request.url.params.get("pageToken")
        return httpx.Response(200, json=pages[token])

    entries = await _client(handler).list_transcript_entries(
        "at-1", "conferenceRecords/cr-1/transcripts/t-1"
    )
    assert [e.text for e in entries] == ["hello", "hi"]
    assert entries[0].participant == "conferenceRecords/cr-1/participants/p1"
    assert entries[0].language_code == "en-US"


async def test_list_participants_maps_all_user_types():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/conferenceRecords/cr-1/participants"
        return httpx.Response(200, json={"participants": [
            {"name": "conferenceRecords/cr-1/participants/p1",
             "signedinUser": {"user": "users/u1", "displayName": "Alice"}},
            {"name": "conferenceRecords/cr-1/participants/p2",
             "anonymousUser": {"displayName": "Guest"}},
            {"name": "conferenceRecords/cr-1/participants/p3",
             "phoneUser": {"displayName": "+1 (555) ..."}},
        ]})

    parts = await _client(handler).list_participants("at-1", "conferenceRecords/cr-1")
    names = {p.name: p.display_name for p in parts}
    assert names["conferenceRecords/cr-1/participants/p1"] == "Alice"
    assert names["conferenceRecords/cr-1/participants/p2"] == "Guest"
    assert names["conferenceRecords/cr-1/participants/p3"] == "+1 (555) ..."


async def test_get_conference_record_404_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "not found"}})

    with pytest.raises(httpx.HTTPStatusError):
        await _client(handler).get_conference_record("at-1", "conferenceRecords/gone")
