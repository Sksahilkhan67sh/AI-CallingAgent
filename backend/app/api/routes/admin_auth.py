"""Admin dashboard login -- Checkpoint 07 §4. The only unauthenticated
route under /api/v1/admin/. Rate-limited per Checkpoint 09 §8.5 --
brute-force credential guessing is exactly what a public login
endpoint needs protection from.

HF1 1.1: only FAILED attempts are counted, keyed by (client address, username) with a higher
per-address ceiling across usernames. A correct credential from an address that is not itself
over a limit is never blocked by someone else's failures (they are other addresses' counters).
Residual risk (DOCUMENTED LIMITATION): anyone who can reach the same client address -- a shared
NAT or office egress -- can slow logins for that address's username; a distributed attacker
cannot lock out a username for OTHER addresses, but can still make guessing slower for its own.
When Redis cannot be read the endpoint fails CLOSED (503), as before.
"""

import hashlib

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.client_address import client_address, rate_limit_identity
from app.core.config import get_settings
from app.core.database import DB_SESSION
from app.core.rate_limit import FailureBucket, enforce_failure_buckets, record_failure_buckets
from app.schemas.admin import LoginRequest, LoginResponse
from app.services.admin.auth import InvalidCredentialsError, authenticate, create_access_token
from app.services.security_audit import record_security_event

router = APIRouter(prefix="/api/v1/admin/auth", tags=["Admin Auth"])


_ENDPOINT = "admin_login"


def _failure_buckets(request: Request, username: str) -> list[FailureBucket]:
    """Per (address, username) and per address. The username is hashed (and normalised) so an
    attacker-chosen 5,000-character name cannot bloat the key space or dodge the limit by
    changing case."""
    settings = get_settings()
    identity = rate_limit_identity(request)
    user_hash = hashlib.sha256(username.strip().lower().encode()).hexdigest()[:16]
    return [
        FailureBucket(
            f"login_fail_user:{identity}:{user_hash}", settings.login_rate_limit_per_minute
        ),
        FailureBucket(f"login_fail_ip:{identity}", settings.login_ip_failure_ceiling_per_minute),
    ]


@router.post("/login", response_model=LoginResponse)
def login(
    data: LoginRequest, request: Request, db: Session = DB_SESSION
) -> LoginResponse:
    source_ip = client_address(request)
    buckets = _failure_buckets(request, data.username)
    # Before authenticating: 429 if this address is already over a failure limit, 503 if the
    # limiter cannot be read (fail closed -- even a correct credential gets no token).
    enforce_failure_buckets(buckets, endpoint=_ENDPOINT)
    try:
        principal = authenticate(data.username, data.password)
    except InvalidCredentialsError as exc:
        record_failure_buckets(buckets, endpoint=_ENDPOINT)
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
