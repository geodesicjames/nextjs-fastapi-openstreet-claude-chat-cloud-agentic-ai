"""Sign-in endpoints.

Next.js calls these server-side and passes the session cookie on to the
browser, so the cookie belongs to the frontend's origin (see README,
"Decisions"). They also work directly from the interactive docs at /docs.
"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status

from app.config import Settings, get_settings
from app.deps import CurrentUser, DbSession, SessionToken
from app.schemas.auth import LoginLinkIn, MessageOut, UserOut, VerifyIn
from app.services import auth as auth_service
from app.services.email import EmailSender, get_email_sender

router = APIRouter(prefix="/auth", tags=["auth"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post("/login-link", status_code=status.HTTP_202_ACCEPTED, response_model=MessageOut)
async def request_login_link(
    body: LoginLinkIn,
    db: DbSession,
    settings: SettingsDep,
    background: BackgroundTasks,
    email_sender: Annotated[EmailSender, Depends(get_email_sender)],
) -> MessageOut:
    """Email a sign-in link.

    The response is identical whether or not the address has an account (or
    was rate-limited), so it cannot be used to discover who has signed up.
    """
    token = await auth_service.create_login_token(db, settings, body.email)
    if token is not None:
        link = f"{settings.frontend_url}/auth/verify?token={token}"
        # BackgroundTasks run after the response is sent, so slow email
        # delivery does not delay it (or make its timing reveal anything).
        background.add_task(
            email_sender.send,
            to=auth_service.normalize_email(body.email),
            subject="Your sign-in link",
            body=(
                f"Click to sign in:\n\n{link}\n\n"
                f"This link expires in {settings.login_link_lifetime_minutes} minutes "
                "and can be used once. If you did not ask for it, ignore this email."
            ),
        )
    return MessageOut(detail="If that address can sign in, a link is on its way.")


@router.post("/verify", response_model=UserOut)
async def verify(
    body: VerifyIn, db: DbSession, settings: SettingsDep, response: Response
) -> UserOut:
    """Exchange a magic-link token for a session cookie."""
    user = await auth_service.consume_login_token(db, body.token)
    if user is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "This sign-in link is invalid or has expired."
        )

    session_token = await auth_service.create_session(db, settings, user)
    response.set_cookie(
        settings.session_cookie_name,
        session_token,
        max_age=settings.session_lifetime_days * 24 * 60 * 60,
        httponly=True,  # not readable from JavaScript
        secure=settings.cookie_secure,
        samesite="lax",  # not sent on cross-site POSTs (basic CSRF protection)
        path="/",
    )
    return UserOut.model_validate(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    db: DbSession,
    settings: SettingsDep,
    response: Response,
    token: SessionToken,
) -> None:
    """End the current session. Safe to call when not signed in."""
    if token is not None:
        await auth_service.delete_session(db, token)
    response.delete_cookie(
        settings.session_cookie_name,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
