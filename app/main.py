import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import deps
from app.api.routes import auth, connections, health, meetings, notes, webhooks
from app.config import get_settings
from app.queue import NullJobQueue

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    pool = None
    if settings.redis_url:
        try:
            import arq

            from app.worker import RealJobQueue, WorkerSettings

            pool = await arq.create_pool(WorkerSettings.redis_settings())
            deps.set_job_queue(RealJobQueue(pool))
            logger.info("job queue: connected to Redis, using RealJobQueue")
        except Exception:
            logger.exception(
                "job queue: failed to connect to Redis; falling back to NullJobQueue"
            )
            pool = None
            deps.set_job_queue(NullJobQueue())
    else:
        logger.info("job queue: REDIS_URL not set, using NullJobQueue")
    try:
        yield
    finally:
        if pool is not None:
            await pool.aclose()
        deps.set_job_queue(NullJobQueue())


def create_app() -> FastAPI:
    application = FastAPI(title="Google Meet Notes", lifespan=lifespan)
    application.include_router(health.router)
    application.include_router(auth.router)
    application.include_router(connections.router)
    application.include_router(meetings.router)
    application.include_router(notes.router)
    application.include_router(webhooks.router)
    return application


app = create_app()
