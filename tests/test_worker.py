import uuid

import pytest

from app import worker


class _RecordingPipeline:
    def __init__(self):
        self.calls = []

    async def __call__(self, session, **kwargs):
        self.calls.append(kwargs["conference_id"])


async def test_notes_pipeline_task_builds_session_and_runs(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    from app.config import get_settings
    get_settings.cache_clear()

    rec = _RecordingPipeline()
    monkeypatch.setattr(worker.pipeline, "run_pipeline", rec)

    sessions_closed = []

    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            sessions_closed.append(True)

    monkeypatch.setattr(worker, "SessionLocal", lambda: FakeSession())

    cid = str(uuid.uuid4())
    ctx = {"job_try": 1}
    await worker.notes_pipeline(ctx, cid)
    assert rec.calls == [uuid.UUID(cid)]
    assert sessions_closed == [True]
    get_settings.cache_clear()


def test_worker_settings_lists_task():
    assert worker.notes_pipeline in worker.WorkerSettings.functions


class _FakePool:
    def __init__(self):
        self.enqueued = []

    async def enqueue_job(self, name, *args, _job_id=None):
        self.enqueued.append((name, args, _job_id))
        return object()


async def test_real_job_queue_enqueues_with_dedup_id():
    pool = _FakePool()
    q = worker.RealJobQueue(pool)
    await q.enqueue_notes_pipeline("conf-123")
    assert pool.enqueued == [("notes_pipeline", ("conf-123",), "notes:conf-123")]
