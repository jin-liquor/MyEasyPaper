from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlmodel import Session, select

from ..core.config import get_config
from ..core.db import get_session
from ..core.security import create_access_token, get_password_hash, verify_password
from ..models.user import Token, User, UserCreate, UserRead
from .ai_settings_routes import require_account_llm
from .deps import get_current_user

router = APIRouter(tags=["auth"])
_optional_bearer = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)
limiter = Limiter(key_func=get_remote_address)


@router.post("/register", response_model=UserRead)
@limiter.limit("5/minute")
def register(request: Request, user_in: UserCreate, session: Session = Depends(get_session)) -> Any:
    if not get_config().security.allow_registration:
        raise HTTPException(
            status_code=403,
            detail="Self-service registration is disabled. Ask an administrator to create your account.",
        )
    user = session.exec(select(User).where(User.email == user_in.email)).first()
    if user:
        raise HTTPException(
            status_code=400,
            detail="The user with this email already exists in the system",
        )
    user = User(
        email=user_in.email,
        hashed_password=get_password_hash(user_in.password),
        is_active=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@router.post("/login", response_model=Token)
@limiter.limit("10/minute")
def login(
    request: Request, form_data: OAuth2PasswordRequestForm = Depends(), session: Session = Depends(get_session)
) -> Any:
    # OAuth2PasswordRequestForm uses 'username' for the email field
    user = session.exec(select(User).where(User.email == form_data.username.strip().lower())).first()
    if (
        len(form_data.password.encode("utf-8")) > 72
        or not user
        or not verify_password(form_data.password, user.hashed_password)
    ):
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")

    access_token_expires = timedelta(hours=24)  # 1 day
    access_token = create_access_token(
        subject=user.id, expires_delta=access_token_expires, token_version=user.token_version
    )
    return {"access_token": access_token, "token_type": "bearer"}


@router.get("/features")
def features(
    response: Response,
    session: Session = Depends(get_session),
    token: str | None = Depends(_optional_bearer),
):
    config = get_config()
    response.headers["Cache-Control"] = "private, no-store"
    ai_available = False
    if token:
        try:
            user = get_current_user(session=session, token=token)
            require_account_llm(user.id, session)
        except HTTPException:
            pass  # The public endpoint still reports non-account capabilities.
        else:
            ai_available = True
    return {
        "registration": config.security.allow_registration,
        "local_sync": config.security.local_sync_enabled,
        "ai_available": ai_available,
        "max_upload_mb": config.processing.max_upload_mb,
        "max_pages": config.processing.max_pages,
        "gloss_preparation_site": "server",
        "gloss_worker_configured": False,
    }
