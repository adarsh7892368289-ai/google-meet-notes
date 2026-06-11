import pytest

from app.google.gemini_client import GeminiSummarizer, SummarizationError
from app.schemas.notes import NotesContent


class _Resp:
    def __init__(self, parsed=None, text="", candidates=None, prompt_feedback=None):
        self.parsed = parsed
        self.text = text
        self.candidates = candidates if candidates is not None else [object()]
        self.prompt_feedback = prompt_feedback


class _FakeModels:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc
        self.calls = []

    async def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents})
        if self._exc is not None:
            raise self._exc
        return self._resp

    async def count_tokens(self, *, model, contents):
        class _T:
            total_tokens = len(contents)
        return _T()


class _FakeAio:
    def __init__(self, models):
        self.models = models


class _FakeClient:
    def __init__(self, models):
        self.aio = _FakeAio(models)


async def test_summarize_returns_parsed_notes():
    parsed = NotesContent(summary="Recap", decisions=["D1"], action_items=[])
    models = _FakeModels(resp=_Resp(parsed=parsed, text='{"summary":"Recap"}'))
    summarizer = GeminiSummarizer(client=_FakeClient(models), model="gemini-2.5-flash")
    out = await summarizer.summarize("alice: hello\nbob: hi")
    assert out.summary == "Recap"
    assert out.decisions == ["D1"]
    assert models.calls[0]["model"] == "gemini-2.5-flash"


async def test_summarize_falls_back_to_text_when_parsed_none():
    models = _FakeModels(resp=_Resp(parsed=None, text='{"summary":"From text","decisions":[],"action_items":[]}'))
    summarizer = GeminiSummarizer(client=_FakeClient(models), model="m")
    out = await summarizer.summarize("transcript")
    assert out.summary == "From text"


async def test_summarize_raises_on_empty_and_blocked():
    class _Blocked:
        block_reason = "SAFETY"
    models = _FakeModels(resp=_Resp(parsed=None, text="", candidates=[], prompt_feedback=_Blocked()))
    summarizer = GeminiSummarizer(client=_FakeClient(models), model="m")
    with pytest.raises(SummarizationError):
        await summarizer.summarize("transcript")


async def test_count_tokens_proxies_client():
    models = _FakeModels(resp=_Resp(parsed=NotesContent(summary="x")))
    summarizer = GeminiSummarizer(client=_FakeClient(models), model="m")
    n = await summarizer.count_tokens("hello")
    assert n == 5
