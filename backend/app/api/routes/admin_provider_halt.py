"""HF1 1.5 -- release the automatic provider halt.

The halt is ENGAGED by the system (the dialer) the moment the provider's configuration is
rejected or the provider fails systemically; it is released only here, by an admin, with a
mandatory reason, and the release is audited. Releasing resurrects nothing automatically --
nothing was burned: held contacts are still dialable and their jobs are still queued.

The current state is shown, read-only, by GET /api/v1/admin/kill-switch (`provider_halt`) and
by GET below.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.api.admin_deps import require_admin, require_role
from app.api.route_limits import MUTATION_LIMIT
from app.core.database import DB_SESSION
from app.core.errors import ServiceUnavailableError
from app.services import outbound_control, provider_halt
from app.services.admin.auth import AdminPrincipal
from app.services.audit_service import record_audit_event

router = APIRouter(prefix="/api/v1/admin/provider-halt", tags=["Admin Provider Halt"])

_UNAVAILABLE = "Provider halt state is unavailable"


class ProviderHaltReleaseRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason is required")
        return value


class ProviderHaltReleaseResult(BaseModel):
    engaged: bool
    changed: bool


@router.get("", dependencies=[Depends(require_admin)])
def get_provider_halt() -> dict[str, object]:
    try:
        return provider_halt.get_state()
    except outbound_control.ControlUnavailableError as exc:
        raise ServiceUnavailableError(_UNAVAILABLE) from exc


@router.post("/release", response_model=ProviderHaltReleaseResult, dependencies=[MUTATION_LIMIT])
def release_provider_halt(
    data: ProviderHaltReleaseRequest,
    db: Session = DB_SESSION,
    principal: AdminPrincipal = Depends(require_role("admin")),
) -> ProviderHaltReleaseResult:
    try:
        changed = provider_halt.release(principal.username, data.reason)
    except outbound_control.ControlUnavailableError as exc:
        raise ServiceUnavailableError(_UNAVAILABLE) from exc
    if changed:  # a no-op release is not audited, so repeated calls cannot spam the trail
        record_audit_event(
            db,
            actor=principal.username,
            action="provider_halt.released",
            entity_type="system",
            entity_id=None,
            metadata={"reason": data.reason},
        )
        db.commit()
    return ProviderHaltReleaseResult(engaged=False, changed=changed)
