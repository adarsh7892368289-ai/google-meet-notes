from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass


class EventParseError(Exception):
    pass


@dataclass
class MeetEvent:
    message_id: str
    event_type: str
    subscription_name: str | None
    conference_record_name: str | None
    transcript_resource_name: str | None


def _subscription_name_from_source(source: str | None) -> str | None:
    if not source:
        return None
    marker = "/subscriptions/"
    idx = source.find(marker)
    if idx == -1:
        return None
    return "subscriptions/" + source[idx + len(marker):]


def _conference_record_from(name: str) -> str | None:
    parts = name.split("/")
    if len(parts) >= 2 and parts[0] == "conferenceRecords":
        return f"{parts[0]}/{parts[1]}"
    return None


def parse_push(envelope: dict) -> MeetEvent:
    message = envelope.get("message")
    if not isinstance(message, dict):
        raise EventParseError("missing message")

    message_id = message.get("messageId") or message.get("message_id")
    if not message_id:
        raise EventParseError("missing messageId")

    raw = message.get("data")
    if not raw:
        raise EventParseError("missing data")
    try:
        decoded = base64.b64decode(raw, validate=True)
        data = json.loads(decoded)
    except (binascii.Error, ValueError) as exc:
        raise EventParseError(f"invalid data payload: {exc}") from exc
    if not isinstance(data, dict):
        raise EventParseError("data payload must be a JSON object")

    attributes = message.get("attributes") or {}
    event_type = attributes.get("ce-type", "")
    subscription_name = _subscription_name_from_source(attributes.get("ce-source"))

    transcript_resource_name: str | None = None
    conference_record_name: str | None = None

    transcript = data.get("transcript")
    if isinstance(transcript, dict) and transcript.get("name"):
        transcript_resource_name = transcript["name"]
        conference_record_name = _conference_record_from(transcript_resource_name)

    record = data.get("conferenceRecord")
    if conference_record_name is None and isinstance(record, dict) and record.get("name"):
        conference_record_name = _conference_record_from(record["name"])

    return MeetEvent(
        message_id=message_id,
        event_type=event_type,
        subscription_name=subscription_name,
        conference_record_name=conference_record_name,
        transcript_resource_name=transcript_resource_name,
    )
