"""Public "request chat access" endpoint."""

import logging
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, status

from app.config import Settings, get_settings
from app.deps import DbSession
from app.schemas.access import AccessRequestIn
from app.schemas.auth import MessageOut
from app.services import access_requests as service
from app.services.email import EmailSender, get_email_sender

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/access-requests", tags=["access"])


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=MessageOut)
async def request_access(
    body: AccessRequestIn,
    db: DbSession,
    settings: Annotated[Settings, Depends(get_settings)],
    background: BackgroundTasks,
    email_sender: Annotated[EmailSender, Depends(get_email_sender)],
) -> MessageOut:
    """Ask for chat access. Admins are notified by email.

    The response is the same whether the request was stored or dropped by
    the per-address limit, so the form reveals nothing about who has asked.
    """
    request = await service.create_access_request(db, settings, body.email, body.message)
    if request is not None:
        admins = await service.admin_emails(db)
        if not admins:
            logger.warning("Access request from %s but there are no admins", request.email)
        for admin in admins:
            background.add_task(
                email_sender.send,
                to=admin,
                subject=f"Chat access request from {request.email}",
                body=(
                    f"{request.email} asked for chat access.\n\n"
                    f"Message:\n{request.message or '(none)'}\n\n"
                    f"Review it at {settings.frontend_url}/admin"
                ),
            )
    return MessageOut(detail="Thanks! You'll get an email if your request is approved.")
