"""SQLAlchemy models.

Every model module is imported here so that `Base.metadata` knows about all
tables when Alembic autogenerates migrations (Django finds models through
INSTALLED_APPS; SQLAlchemy only knows about classes that have been imported).
"""

from app.models.auth import LoginToken, UserSession
from app.models.base import Base
from app.models.chat import AccessRequest, AccessRequestStatus, ChatGrant, LlmUsage
from app.models.user import User

__all__ = [
    "AccessRequest",
    "AccessRequestStatus",
    "Base",
    "ChatGrant",
    "LlmUsage",
    "LoginToken",
    "User",
    "UserSession",
]
