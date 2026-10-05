"""Admin dashboard login -- Checkpoint 07 §4. The only unauthenticated
route under /api/v1/admin/. Rate-limited per Checkpoint 09 §8.5 --
brute-force credential guessing is exactly what a public login
endpoint needs protection from.
"""

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import get_db
from app.core.rate_limit import rate_limit_dependency
from app.schemas.admin import LoginRequest, LoginResponse
from app.services.admin.auth import InvalidCredentialsError, authenticate, create_access_token
from app.services.security_audit import record_security_event

router = APIRouter(prefix="/api/v1/admin/auth", tags=["Admin Auth"])


def _login_rate_limit():
    return rate_limit_dependency(
        limit=lambda: get_settings().login_rate_limit_per_minute,
        window_seconds=60,
        key_prefix="admin_login",
        fail_closed=True,  # CP11: an unthrottled login endpoint is a brute-force oracle
    )


@router.post("/login", response_model=LoginResponse, dependencies=[Depends(_login_rate_limit())])
def login(
    data: LoginRequest, request: Request, db: Session = Depends(get_db)
) -> LoginResponse:
    source_ip = request.client.host if request.client else "unknown"
    try:
        principal = authenticate(data.username, data.password)
    except InvalidCredentialsError as exc:
        # Never the password, and the attempted username only as a short fingerprint
        # (people paste passwords into the username box).
        fingerprint = hashlib.sha256(data.username.encode()).hexdigest()[:12]
        record_security_event(
            db,
            action="auth.login_failed",
            actor="anonymous",
            metadata={"username_fingerprint": fingerprint, "source_ip": source_ip},
            throttle_key=source_ip,
            commit=True,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password"
        ) from exc

    record_security_event(
        db,
        action="auth.login_succeeded",
        actor=principal.username,
        metadata={"role": principal.role, "source_ip": source_ip},
        commit=True,
    )
    token, expires_in = create_access_token(principal)
    return LoginResponse(
        access_token=token,
        expires_in=expires_in,
        role=principal.role,
        username=principal.username,
    )
