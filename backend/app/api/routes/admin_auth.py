"""Admin dashboard login -- Checkpoint 07 §4. The only unauthenticated
route under /api/v1/admin/. Rate-limited per Checkpoint 09 §8.5 --
brute-force credential guessing is exactly what a public login
endpoint needs protection from.
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.config import get_settings
from app.core.rate_limit import rate_limit_dependency
from app.schemas.admin import LoginRequest, LoginResponse
from app.services.admin.auth import InvalidCredentialsError, authenticate, create_access_token

router = APIRouter(prefix="/api/v1/admin/auth", tags=["Admin Auth"])


def _login_rate_limit():
    return rate_limit_dependency(
        limit=get_settings().login_rate_limit_per_minute,
        window_seconds=60,
        key_prefix="admin_login",
    )


@router.post("/login", response_model=LoginResponse, dependencies=[Depends(_login_rate_limit())])
def login(data: LoginRequest) -> LoginResponse:
    try:
        principal = authenticate(data.username, data.password)
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password"
        ) from exc

    token, expires_in = create_access_token(principal)
    return LoginResponse(
        access_token=token,
        expires_in=expires_in,
        role=principal.role,
        username=principal.username,
    )
