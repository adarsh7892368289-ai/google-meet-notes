import pytest


@pytest.fixture(autouse=True)
def _clear_settings():
    from app.config import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_lifespan_keeps_null_queue_without_redis(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    from app.config import get_settings
    get_settings.cache_clear()

    from app.api import deps
    from app.main import create_app
    from app.queue import NullJobQueue

    app = create_app()
    async with app.router.lifespan_context(app):
        assert isinstance(deps.get_job_queue(), NullJobQueue)


async def test_lifespan_installs_real_queue_with_redis(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    from app.config import get_settings
    get_settings.cache_clear()

    created = {}
    closed = {"n": 0}

    class FakePool:
        async def aclose(self):
            closed["n"] += 1

    async def fake_create_pool(redis_settings):
        created["called"] = True
        return FakePool()

    # Patch the arq entrypoint used by the lifespan.
    import arq
    monkeypatch.setattr(arq, "create_pool", fake_create_pool)

    from app.api import deps
    from app.main import create_app
    from app.queue import NullJobQueue
    from app.worker import RealJobQueue

    app = create_app()
    async with app.router.lifespan_context(app):
        assert created.get("called") is True
        assert isinstance(deps.get_job_queue(), RealJobQueue)
    # after shutdown the pool is closed and the queue reverts to the no-op
    assert closed["n"] == 1
    assert isinstance(deps.get_job_queue(), NullJobQueue)
