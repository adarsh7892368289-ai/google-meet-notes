import base64
import json

import pytest

from app.events.parser import EventParseError, parse_push


def _envelope(*, data: dict, attributes: dict, message_id="msg-1"):
    return {
        "subscription": "projects/p/subscriptions/s",
        "message": {
            "data": base64.b64encode(json.dumps(data).encode()).decode(),
            "messageId": message_id,
            "publishTime": "2026-06-10T10:00:00Z",
            "attributes": attributes,
        },
    }


def test_parse_transcript_event():
    env = _envelope(
        data={"transcript": {"name": "conferenceRecords/cr-1/transcripts/t-1"}},
        attributes={
            "ce-id": "spaces/SP/spaceEvents/E1",
            "ce-source": "//workspaceevents.googleapis.com/subscriptions/sub-1",
            "ce-type": "google.workspace.meet.transcript.v2.fileGenerated",
            "ce-time": "2026-06-10T10:00:00Z",
        },
    )
    ev = parse_push(env)
    assert ev.message_id == "msg-1"
    assert ev.event_type == "google.workspace.meet.transcript.v2.fileGenerated"
    assert ev.subscription_name == "subscriptions/sub-1"
    assert ev.transcript_resource_name == "conferenceRecords/cr-1/transcripts/t-1"
    assert ev.conference_record_name == "conferenceRecords/cr-1"


def test_parse_conference_started_event():
    env = _envelope(
        data={"conferenceRecord": {"name": "conferenceRecords/cr-9"}},
        attributes={
            "ce-source": "//workspaceevents.googleapis.com/subscriptions/sub-2",
            "ce-type": "google.workspace.meet.conference.v2.started",
        },
    )
    ev = parse_push(env)
    assert ev.event_type == "google.workspace.meet.conference.v2.started"
    assert ev.conference_record_name == "conferenceRecords/cr-9"
    assert ev.transcript_resource_name is None


def test_parse_missing_message_raises():
    with pytest.raises(EventParseError):
        parse_push({"subscription": "x"})


def test_parse_bad_base64_raises():
    env = {"message": {"data": "!!!notbase64!!!", "messageId": "m", "attributes": {}}}
    with pytest.raises(EventParseError):
        parse_push(env)


def test_parse_missing_message_id_raises():
    env = _envelope(
        data={"conferenceRecord": {"name": "conferenceRecords/c"}},
        attributes={"ce-source": "//workspaceevents.googleapis.com/subscriptions/s",
                    "ce-type": "google.workspace.meet.conference.v2.started"},
        message_id="",
    )
    with pytest.raises(EventParseError):
        parse_push(env)


def test_subscription_name_none_when_source_missing():
    env = _envelope(
        data={"conferenceRecord": {"name": "conferenceRecords/c"}},
        attributes={"ce-type": "google.workspace.meet.conference.v2.started"},
    )
    ev = parse_push(env)
    assert ev.subscription_name is None


def test_parse_rejects_non_dict_json():
    for payload in [[], "string", 123, None, True]:
        env = {
            "message": {
                "data": base64.b64encode(json.dumps(payload).encode()).decode(),
                "messageId": "m",
                "attributes": {},
            }
        }
        with pytest.raises(EventParseError, match="must be a JSON object"):
            parse_push(env)
