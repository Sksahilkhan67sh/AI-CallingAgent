"""Admin dashboard authentication -- Checkpoint 07 §4.

No user table, no registration/password-reset flow -- two fixed
operator identities configured via Settings (same "env-configured
secret, dev-only-insecure default" convention as
`telephony_webhook_secret`), matching the checkpoint's own "do not
invent a large RBAC system" instruction. JWTs are signed with the
`jwt_signing_key` setting, which existed in Settings since Checkpoint
00 but was never actually used until this checkpoint.

The backend is authoritative: a token's `role` claim is set here, once,
at login, from server-side config -- never from anything the client
sends on later requests (Checkpoint 07 §4: "Never trust ... client-
provided role").
"""

import secrets
import time
from dataclasses import dataclass

import jwt

from app.core.config import get_settings


class InvalidCredentialsError(Exception):
    pass


class InvalidTokenError(Exception):
    pass


@dataclass(frozen=True)
class AdminPrincipal:
    username: str
    role: str  # "admin" | "operator"


_ALGORITHM = "HS256"


def authenticate(username: str, password: str) -> AdminPrincipal:
    """Constant-time comparison against the two configured identities --
    same approach the existing telephony webhook secret check already
    uses (`x_webhook_secret != settings.telephony_webhook_secret`)."""
    settings = get_settings()

    if secrets.compare_digest(username, settings.admin_username) and (
        secrets.compare_digest(password, settings.admin_password)
    ):
        return AdminPrincipal(username=username, role="admin")

    if secrets.compare_digest(username, settings.operator_username) and (
        secrets.compare_digest(password, settings.operator_password)
    ):
        return AdminPrincipal(username=username, role="operator")

    raise InvalidCredentialsError("Invalid username or password")


def create_access_token(principal: AdminPrincipal) -> tuple[str, int]:
    settings = get_settings()
    now = int(time.time())
    expires_in = settings.jwt_expiry_seconds
    payload = {
        "sub": principal.username,
        "role": principal.role,
        "iat": now,
        "exp": now + expires_in,
    }
    token = jwt.encode(payload, settings.jwt_signing_key, algorithm=_ALGORITHM)
    return token, expires_in


def decode_access_token(token: str) -> AdminPrincipal:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_signing_key, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    role = payload.get("role")
    username = payload.get("sub")
    if role not in ("admin", "operator") or not username:
        raise InvalidTokenError("Token missing required claims")
    return AdminPrincipal(username=username, role=role)
