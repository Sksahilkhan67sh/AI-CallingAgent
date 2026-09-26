"""Server-side enforcement of admin dashboard auth -- Checkpoint 07 §4,
§36. Every `/api/v1/admin/*` route (other than login) depends on
`require_admin` or `require_role("admin")`; hiding a page in the
Next.js frontend is never treated as sufficient on its own.
"""

from fastapi import Depends, Header, HTTPException, status

from app.services.admin.auth import AdminPrincipal, InvalidTokenError, decode_access_token


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authorization.split(" ", 1)[1].strip()


def require_admin(authorization: str | None = Header(default=None)) -> AdminPrincipal:
    """Any authenticated principal (admin or operator) -- the default for
    read/monitoring endpoints (Checkpoint 07 §5)."""
    token = _extract_bearer_token(authorization)
    try:
        return decode_access_token(token)
    except InvalidTokenError as exc:
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

    def _dependency(principal: AdminPrincipal = Depends(require_admin)) -> AdminPrincipal:
        if principal.role != role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"This action requires the '{role}' role",
            )
        return principal

    return _dependency
