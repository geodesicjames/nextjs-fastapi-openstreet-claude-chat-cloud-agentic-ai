"""Chat routes. For now just the access check; streaming chat comes in phase 3.

Every route that calls Claude must depend on `ChatAccess` (see app/deps.py).
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.deps import CurrentUser, DbSession
from app.schemas.chat import ChatAccessOut
from app.services.chat_access import get_chat_status

router = APIRouter(prefix="/chat", tags=["chat"])


@router.get("/access", response_model=ChatAccessOut)
async def chat_access(
    db: DbSession, user: CurrentUser, settings: Annotated[Settings, Depends(get_settings)]
) -> ChatAccessOut:
    """The caller's chat access and today's usage.

    Needs a signed-in user but *not* chat access: someone without a grant gets
    `allowed: false` and a reason, rather than a 403.
    """
    chat_status = await get_chat_status(db, settings, user)
    return ChatAccessOut.from_status(chat_status, settings.chat_mode)
