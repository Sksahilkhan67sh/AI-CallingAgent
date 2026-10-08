"""Global do-not-call list -- CP14. Reads: any authenticated role. Writes (add, import,
remove): admin only, on the CP11 budgets (mutation / import), every one audited."""

import uuid

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile, status
from sqlalchemy.orm import Session

from app.api.admin_deps import require_admin, require_role
from app.api.route_limits import IMPORT_LIMIT, MUTATION_LIMIT
from app.core.database import get_db
from app.core.errors import ValidationError
from app.schemas.pagination import Page
from app.schemas.suppression import (
    SuppressionAddRequest,
    SuppressionAddResponse,
    SuppressionImportResult,
    SuppressionRemoveRequest,
    SuppressionResponse,
)
from app.services.admin.auth import AdminPrincipal
from app.services.contact_import_service import MAX_IMPORT_FILE_BYTES
from app.services.suppression_service import SuppressionService

router = APIRouter(prefix="/api/v1/admin/suppressions", tags=["Admin Suppressions"])

_ADMIN = Depends(require_role("admin"))


@router.get("", response_model=Page[SuppressionResponse], dependencies=[Depends(require_admin)])
def list_suppressions(
    phone: str | None = Query(default=None, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> Page[SuppressionResponse]:
    items, total = SuppressionService(db, actor="reader").search(phone, limit, offset)
    return Page[SuppressionResponse](items=list(items), total=total, limit=limit, offset=offset)


@router.post(
    "",
    response_model=SuppressionAddResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[MUTATION_LIMIT],
)
def add_suppression(
    data: SuppressionAddRequest,
    response: Response,
    db: Session = Depends(get_db),
    principal: AdminPrincipal = _ADMIN,
) -> SuppressionAddResponse:
    result = SuppressionService(db, actor=principal.username).add(
        data.phone_number, data.reason, data.contact_id
    )
    if not result.created:
        response.status_code = status.HTTP_200_OK  # idempotent no-op returning the row
    return result


@router.post(
    "/import",
    response_model=SuppressionImportResult,
    dependencies=[IMPORT_LIMIT],
)
async def import_suppressions(
    file: UploadFile = File(...),
    reason: str | None = Form(default=None, max_length=200),
    db: Session = Depends(get_db),
    principal: AdminPrincipal = _ADMIN,
) -> SuppressionImportResult:
    contents = await file.read(MAX_IMPORT_FILE_BYTES + 1)
    if len(contents) > MAX_IMPORT_FILE_BYTES:
        raise ValidationError(
            f"Import file exceeds the maximum size of {MAX_IMPORT_FILE_BYTES} bytes"
        )
    return SuppressionService(db, actor=principal.username).import_csv(contents, reason)


@router.delete(
    "/{suppression_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[MUTATION_LIMIT],
)
def remove_suppression(
    suppression_id: uuid.UUID,
    data: SuppressionRemoveRequest,
    db: Session = Depends(get_db),
    principal: AdminPrincipal = _ADMIN,
) -> Response:
    SuppressionService(db, actor=principal.username).remove(suppression_id, data.reason)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
