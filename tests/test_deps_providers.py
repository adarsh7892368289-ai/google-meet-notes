import pytest


@pytest.fixture(autouse=True)
def _clear_settings():
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_get_job_queue_returns_null_when_no_redis(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    from app.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    from app.queue import NullJobQueue
    q = deps.get_job_queue()
    assert isinstance(q, NullJobQueue)


def test_get_summarizer_builds_gemini_summarizer(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    from app.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    from app.google.gemini_client import GeminiSummarizer
    s = deps.get_summarizer()
    assert isinstance(s, GeminiSummarizer)


def test_set_job_queue_overrides_active_queue(monkeypatch):
    from app.api import deps
    from app.queue import NullJobQueue

    class Sentinel:
        async def enqueue_notes_pipeline(self, conference_id: str) -> None:
            pass

    original = deps.get_job_queue()
    try:
        sentinel = Sentinel()
        deps.set_job_queue(sentinel)
        assert deps.get_job_queue() is sentinel
    finally:
        deps.set_job_queue(original)
    assert isinstance(deps.get_job_queue(), NullJobQueue)
