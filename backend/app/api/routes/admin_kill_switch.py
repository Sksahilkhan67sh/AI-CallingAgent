"""CP11 -- global outbound kill switch control.

Reading the state is open to any authenticated role (operators need to see why
nothing is dialing). Changing it is admin-only. Every real state transition is
audited; a no-op (already on / already off) is not, so concurrent toggles cannot
produce duplicate audit events.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.admin_deps import require_admin, require_role
from app.api.route_limits import MUTATION_LIMIT
from app.core.database import get_db
from app.core.errors import ServiceUnavailableError
from app.services import kill_switch
from app.services.admin.auth import AdminPrincipal
from app.services.audit_service import record_audit_event

router = APIRouter(prefix="/api/v1/admin/kill-switch", tags=["Admin Kill Switch"])

_UNAVAILABLE = "Kill switch state is unavailable"


class KillSwitchEnableRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class KillSwitchChangeResult(BaseModel):
    enabled: bool
    changed: bool


@router.get("", dependencies=[Depends(require_admin)])
def get_kill_switch() -> dict[str, object]:
    try:
        return kill_switch.get_state()
    except kill_switch.KillSwitchUnavailableError as exc:
        raise ServiceUnavailableError(_UNAVAILABLE) from exc


@router.post(
    "",
    response_model=KillSwitchChangeResult,
    dependencies=[MUTATION_LIMIT],
)
def enable_kill_switch(
    data: KillSwitchEnableRequest | None = None,
    db: Session = Depends(get_db),
    principal: AdminPrincipal = Depends(require_role("admin")),
) -> KillSwitchChangeResult:
    reason = data.reason if data else None
    try:
        changed = kill_switch.enable(principal.username, reason)
    except kill_switch.KillSwitchUnavailableError as exc:
        raise ServiceUnavailableError(_UNAVAILABLE) from exc
    if changed:
        record_audit_event(
            db,
            actor=principal.username,
            action="kill_switch.enabled",
            entity_type="system",
            entity_id=None,
            metadata={"reason": reason},
        )
        db.commit()
    return KillSwitchChangeResult(enabled=True, changed=changed)


@router.delete(
    "",
    response_model=KillSwitchChangeResult,
    dependencies=[MUTATION_LIMIT],
)
def disable_kill_switch(
    db: Session = Depends(get_db),
    principal: AdminPrincipal = Depends(require_role("admin")),
) -> KillSwitchChangeResult:
    try:
        changed = kill_switch.disable()
    except kill_switch.KillSwitchUnavailableError as exc:
        raise ServiceUnavailableError(_UNAVAILABLE) from exc
    if changed:
        record_audit_event(
            db,
            actor=principal.username,
            action="kill_switch.disabled",
            entity_type="system",
            entity_id=None,
        )
        db.commit()
    # The env backstop is independent of the runtime flag: DELETE cannot clear it.
    return KillSwitchChangeResult(enabled=kill_switch.block_reason() is not None, changed=changed)
