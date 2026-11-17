"""Request/response schemas for sign-in."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class LoginLinkIn(BaseModel):
    email: EmailStr


class VerifyIn(BaseModel):
    token: str = Field(min_length=1, max_length=200)


class MessageOut(BaseModel):
    detail: str


class UserOut(BaseModel):
    # from_attributes lets Pydantic read a SQLAlchemy object directly
    # (like a DRF ModelSerializer), but only the fields listed here.
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    is_admin: bool
    created_at: datetime
