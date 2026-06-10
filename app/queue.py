from __future__ import annotations

import logging
from typing import Protocol

logger = logging.getLogger(__name__)


class JobQueue(Protocol):
    async def enqueue_notes_pipeline(self, conference_id: str) -> None: ...


class NullJobQueue:
    """No-op queue used until the arq worker lands in Phase 5.

    The durable `conferences` row (pipeline_state='pending') is the source of
    truth; the Phase 7 sweeper will pick up anything not yet processed, so
    dropping the enqueue here loses no work.
    """

    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def enqueue_notes_pipeline(self, conference_id: str) -> None:
        self.enqueued.append(conference_id)
        logger.info("notes pipeline enqueue (noop) for conference %s", conference_id)
