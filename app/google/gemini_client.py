from __future__ import annotations

import json
import logging
from typing import Protocol

from app.schemas.notes import NotesContent

logger = logging.getLogger(__name__)

_PROMPT = (
    "You are a meeting-notes assistant. Read the following meeting transcript and "
    "produce concise, faithful notes. Capture the overall summary, explicit decisions "
    "made, and concrete action items with an owner when one is stated. Do not invent "
    "content that is not supported by the transcript.\n\nTRANSCRIPT:\n"
)


class SummarizationError(Exception):
    pass


class Summarizer(Protocol):
    async def summarize(self, transcript: str) -> NotesContent: ...
    async def count_tokens(self, text: str) -> int: ...


def _build_config():
    # Imported lazily so importing this module never requires the SDK at import time.
    from google.genai import types

    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=NotesContent,
    )


class GeminiSummarizer:
    def __init__(self, *, client, model: str) -> None:
        self._client = client
        self._model = model

    async def count_tokens(self, text: str) -> int:
        resp = await self._client.aio.models.count_tokens(
            model=self._model, contents=text
        )
        return resp.total_tokens

    async def summarize(self, transcript: str) -> NotesContent:
        resp = await self._client.aio.models.generate_content(
            model=self._model,
            contents=_PROMPT + transcript,
            config=_build_config(),
        )
        return self._extract(resp)

    @staticmethod
    def _extract(resp) -> NotesContent:
        feedback = getattr(resp, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            raise SummarizationError(f"prompt blocked: {feedback.block_reason}")
        if not getattr(resp, "candidates", None) and not getattr(resp, "text", ""):
            raise SummarizationError("empty completion")

        parsed = getattr(resp, "parsed", None)
        if isinstance(parsed, NotesContent):
            return parsed
        text = getattr(resp, "text", "") or ""
        if not text.strip():
            raise SummarizationError("empty completion")
        try:
            return NotesContent.model_validate(json.loads(text))
        except (ValueError, json.JSONDecodeError) as exc:
            raise SummarizationError(f"unparseable notes output: {exc}") from exc
