from app.queue import NullJobQueue


async def test_null_job_queue_records_enqueues():
    q = NullJobQueue()
    await q.enqueue_notes_pipeline("conf-123")
    await q.enqueue_notes_pipeline("conf-456")
    assert q.enqueued == ["conf-123", "conf-456"]
