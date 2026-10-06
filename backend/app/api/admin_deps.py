"""Server-side enforcement of admin dashboard auth -- Checkpoint 07 §4,
§36. Every `/api/v1/admin/*` route (other than login) depends on
`require_admin` or `require_role("admin")`; hiding a page in the
Next.js frontend is never treated as sufficient on its own.
"""

import logging

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services.admin.auth import AdminPrincipal, InvalidTokenError, decode_access_token
from app.services.security_audit import record_security_event

logger = logging.getLogger("app.security")


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authorization.split(" ", 1)[1].strip()


def _log_auth_rejected(request: Request, reason: str) -> None:
    """Log-only (never a DB write): an unauthenticated caller must not be able to turn
    requests into database rows. A reason code, the method and the route TEMPLATE -- never
    the token, the header, or the raw path. The request_id is stamped on the record."""
    route = getattr(request.scope.get("route"), "path", "unknown")
    logger.warning(
        "auth_rejected",
        extra={"sec_reason": reason, "sec_method": request.method, "sec_route": route},
    )


def require_admin(
    request: Request, authorization: str | None = Header(default=None)
) -> AdminPrincipal:
    """Any authenticated principal (admin or operator) -- the default for
    read/monitoring endpoints (Checkpoint 07 §5)."""
    try:
        token = _extract_bearer_token(authorization)
    except HTTPException:
        _log_auth_rejected(request, "missing_or_malformed_header")
        raise
    try:
        return decode_access_token(token)
    except InvalidTokenError as exc:
        _log_auth_rejected(request, "invalid_or_expired_token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def require_role(role: str):
    """Stricter dependency for operational controls (campaign
    activate/pause/resume) -- currently only ever called with "admin",
    since OPERATOR (per §5) is a strict subset of ADMIN's permissions
    with no operator-only endpoint of its own. Read/monitoring endpoints
    use `require_admin` (any authenticated role) instead."""

    def _dependency(
        request: Request,
        principal: AdminPrincipal = Depends(require_admin),
        db: Session = Depends(get_db),
    ) -> AdminPrincipal:
        if principal.role != role:
            route = getattr(request.scope.get("route"), "path", "unknown")
            record_security_event(
                db,
                action="authz.denied",
                actor=principal.username,
                metadata={
                    "role": principal.role,
                    "required_role": role,
                    "method": request.method,
                    "route": route,
                    # route template + resource ids only: never query strings or bodies
                    "resource_ids": {
                        k: str(v) for k, v in request.path_params.items() if k.endswith("_id")
                    },
                },
                throttle_key=f"{principal.username}:{request.method}:{route}",
                commit=True,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires the '{role}' role",
            )
        return principal

    return _dependency


def principal_identity(principal: AdminPrincipal = Depends(require_admin)) -> str:
    """Rate-limit bucket owner for authenticated routes: the VERIFIED token subject.
    Resolving it runs authentication first, so an unauthenticated request is a 401
    and never consumes (or probes) a bucket."""
    return f"user:{principal.username}"
