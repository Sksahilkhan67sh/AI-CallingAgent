"""Global do-not-call administration (CP14): add, bulk import, search, remove.

Reads and writes go through SuppressionRepository and the shared phone normalizer, so a
number typed as "09876543210", "+91 98765-43210" or "919876543210" is one entry. Audit rows
and logs carry a keyed fingerprint plus the last four digits -- never the full number.

Registry scrubbing (NDNC/DND) and consent management are NOT implemented: no registry
credentials exist. Bulk import of a list scrubbed elsewhere is the supported path.
"""

import csv
import io
import uuid
from collections.abc import Iterator, Sequence

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationError
from app.models.enums import SuppressionSource
from app.models.suppression import Suppression
from app.repositories.contact_repository import ContactRepository
from app.repositories.suppression_repository import SuppressionRepository
from app.schemas.suppression import (
    SuppressionAddResponse,
    SuppressionImportResult,
    SuppressionImportRowError,
    SuppressionResponse,
)
from app.services.audit_service import record_audit_event
from app.services.contact_import_service import MAX_IMPORT_ROWS
from app.services.phone import (
    EMPTY,
    InvalidPhoneError,
    last4,
    mask_phone_number,
    normalize_phone,
    phone_fingerprint,
)

_CHUNK = 500


def _normalize(raw: str) -> str:
    try:
        return normalize_phone(raw, get_settings().default_region).e164
    except InvalidPhoneError as exc:
        raise ValidationError(f"Invalid phone number ({exc.code})") from None


def _safe_identity(e164: str) -> dict[str, str]:
    return {
        "fingerprint": phone_fingerprint(e164, get_settings().jwt_signing_key),
        "last4": last4(e164),
    }


def to_response(row: Suppression) -> SuppressionResponse:
    return SuppressionResponse(
        id=row.id,
        phone_masked=mask_phone_number(row.phone_number),
        last4=last4(row.phone_number),
        source=row.source,
        reason=row.reason,
        contact_id=row.contact_id,
        created_by=row.created_by,
        requested_at=row.requested_at,
    )


class SuppressionService:
    def __init__(self, db: Session, actor: str) -> None:
        self.db = db
        self.actor = actor
        self.repo = SuppressionRepository(db)

    def add(
        self, raw_number: str, reason: str | None, contact_id: uuid.UUID | None
    ) -> SuppressionAddResponse:
        e164 = _normalize(raw_number)

        if contact_id is not None:
            contact = ContactRepository(self.db).get_by_id(contact_id)
            if contact is None:
                raise NotFoundError(f"Contact {contact_id} not found")
            if contact.normalized_phone_number != e164:
                raise ValidationError("contact_id does not belong to this phone number")

        new_id = self.repo.insert_if_absent(
            e164,
            source=SuppressionSource.MANUAL_API,
            reason=reason,
            contact_id=contact_id,
            created_by=self.actor,
        )
        if new_id is None and self.repo.get_by_phone(e164) is None:
            # The contact already has a row under another number: record the number anyway.
            new_id = self.repo.insert_if_absent(
                e164,
                source=SuppressionSource.MANUAL_API,
                reason=reason,
                created_by=self.actor,
            )
        row = self.repo.get_by_phone(e164)
        assert row is not None
        created = new_id is not None
        if created:  # only the call that inserted audits: an idempotent repeat is silent
            record_audit_event(
                self.db,
                actor=self.actor,
                action="suppression.added",
                entity_type="suppression",
                entity_id=row.id,
                metadata={**_safe_identity(e164), "source": "manual_api", "reason": reason},
            )
        return SuppressionAddResponse(**to_response(row).model_dump(), created=created)

    def import_csv(self, csv_bytes: bytes, reason: str | None) -> SuppressionImportResult:
        errors: list[SuppressionImportRowError] = []
        unique: dict[str, None] = {}  # insertion-ordered set of canonical numbers
        total = repeated = 0
        for row_number, raw in self._read_rows(csv_bytes):
            total += 1
            if total > MAX_IMPORT_ROWS:
                raise ValidationError(f"Import file exceeds the maximum of {MAX_IMPORT_ROWS} rows")
            if not raw.strip():
                errors.append(SuppressionImportRowError(row=row_number, reason=EMPTY))
                continue
            try:
                e164 = normalize_phone(raw, get_settings().default_region).e164
            except InvalidPhoneError as exc:
                errors.append(SuppressionImportRowError(row=row_number, reason=exc.code))
                continue
            if e164 in unique:
                repeated += 1  # the same number twice in one file
            unique[e164] = None

        numbers = list(unique)
        added = 0
        for start in range(0, len(numbers), _CHUNK):
            added += self.repo.bulk_insert_if_absent(
                numbers[start : start + _CHUNK],
                source=SuppressionSource.MANUAL_API,
                reason=reason,
                created_by=self.actor,
            )
        result = SuppressionImportResult(
            total=total,
            added=added,
            already_present=(len(numbers) - added) + repeated,
            invalid=len(errors),
            errors=errors,
        )
        record_audit_event(
            self.db,
            actor=self.actor,
            action="suppression.imported",
            entity_type="suppression",
            entity_id=None,
            metadata={
                "total": result.total,
                "added": result.added,
                "already_present": result.already_present,
                "invalid": result.invalid,
            },
        )
        return result

    def search(
        self, phone: str | None, limit: int, offset: int
    ) -> tuple[Sequence[SuppressionResponse], int]:
        e164 = _normalize(phone) if phone is not None else None
        rows, total = self.repo.list(phone=e164, limit=limit, offset=offset)
        return [to_response(r) for r in rows], total

    def remove(self, suppression_id: uuid.UUID, reason: str) -> None:
        row = self.repo.get_by_id(suppression_id)
        if row is None:
            raise NotFoundError(f"Suppression {suppression_id} not found")
        identity = _safe_identity(row.phone_number)
        source = row.source.value
        self.repo.remove(row)
        record_audit_event(
            self.db,
            actor=self.actor,
            action="suppression.removed",
            entity_type="suppression",
            entity_id=suppression_id,
            metadata={**identity, "source": source, "reason": reason},
        )

    @staticmethod
    def _read_rows(csv_bytes: bytes) -> Iterator[tuple[int, str]]:
        try:
            text = csv_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValidationError("CSV file must be UTF-8 encoded") from exc
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None or "phone_number" not in reader.fieldnames:
            raise ValidationError("CSV file must have a 'phone_number' column")
        for row_number, row in enumerate(reader, start=2):  # header is row 1
            yield row_number, (row.get("phone_number") or "")
