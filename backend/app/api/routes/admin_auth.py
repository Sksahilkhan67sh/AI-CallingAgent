"""Admin dashboard login -- Checkpoint 07 §4. The only unauthenticated
route under /api/v1/admin/.
"""

from fastapi import APIRouter, HTTPException, status

from app.schemas.admin import LoginRequest, LoginResponse
from app.services.admin.auth import InvalidCredentialsError, authenticate, create_access_token

router = APIRouter(prefix="/api/v1/admin/auth", tags=["Admin Auth"])


@router.post("/login", response_model=LoginResponse)
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
