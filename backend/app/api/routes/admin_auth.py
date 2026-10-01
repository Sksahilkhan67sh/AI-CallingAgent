"""Admin dashboard login -- Checkpoint 07 §4. The only unauthenticated
route under /api/v1/admin/. Checkpoint 09: rate limited per client IP and
every attempt is audited (the submitted password is never recorded).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.rate_limit import rate_limit
from app.schemas.admin import LoginRequest, LoginResponse
from app.services.admin.auth import InvalidCredentialsError, authenticate, create_access_token
from app.services.audit_service import record_audit_event

logger = logging.getLogger("admin_auth")

router = APIRouter(prefix="/api/v1/admin/auth", tags=["Admin Auth"])


@router.post(
    "/login",
    response_model=LoginResponse,
    dependencies=[Depends(rate_limit("admin_login", "login_rate_limit_per_minute"))],
)
def login(data: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    try:
        principal = authenticate(data.username, data.password)
    except InvalidCredentialsError as exc:
        record_audit_event(
            db,
            actor=data.username[:64],
            action="admin.login.failed",
            entity_type="admin_session",
            entity_id=None,
        )
        db.commit()  # the 401 below must not roll the audit row back
        logger.warning("admin_login_failed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password"
        ) from exc

    record_audit_event(
        db,
        actor=principal.username,
        action="admin.login.succeeded",
        entity_type="admin_session",
        entity_id=None,
        metadata={"role": principal.role},
    )
    token, expires_in = create_access_token(principal)
    return LoginResponse(
        access_token=token,
        expires_in=expires_in,
        role=principal.role,
        username=principal.username,
    )
