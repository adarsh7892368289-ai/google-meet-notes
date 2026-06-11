import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_job_queue, get_push_verifier
from app.db import get_session
from app.events.oidc import PushVerificationError, PushVerifier
from app.events.parser import EventParseError, parse_push
from app.queue import JobQueue
from app.services import event_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/webhooks/google", tags=["webhooks"])


@router.post("/events")
async def receive_events(
    request: Request,
    session: AsyncSession = Depends(get_session),
    verifier: PushVerifier = Depends(get_push_verifier),
    queue: JobQueue = Depends(get_job_queue),
) -> Response:
    try:
        verifier.verify(request.headers.get("Authorization"))
    except PushVerificationError as exc:
        logger.warning("rejected unverified pub/sub push: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid push token"
        ) from exc

    envelope = await request.json()
    try:
        event = parse_push(envelope)
    except EventParseError as exc:
        # Ack poison messages so Pub/Sub stops redelivering them.
        logger.warning("unparseable pub/sub push acked: %s", exc)
        return Response(status_code=status.HTTP_200_OK)

    try:
        outcome = await event_service.handle_event(session, event, queue)
    except Exception:
        # Unexpected failure: NACK (500) so Pub/Sub retries with backoff.
        logger.exception("error handling event %s", event.message_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="processing error"
        )

    logger.info("event %s outcome=%s", event.message_id, outcome)
    return Response(status_code=status.HTTP_200_OK)
