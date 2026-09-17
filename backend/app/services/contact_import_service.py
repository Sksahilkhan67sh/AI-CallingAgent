"""Bulk CSV contact import -- Checkpoint 02 Steps 17-19.

FR-1.1-FR-1.4 (docs/specs/Product/AI-Calling-Agent-FRS.docx) tie import
directly to campaign creation: a campaign is only created if the file
has at least one valid contact. See docs/CHECKPOINT-02-NOTES.md.

Two passes, both bounded:
1. Stream the CSV row by row (never materialize the whole file),
   validating/normalizing/deduplicating into an in-memory list of at
   most MAX_IMPORT_ROWS phone numbers plus a list of row errors.
2. Only if that list is non-empty: create the campaign and insert
   contacts in fixed-size chunks. If it's empty, nothing touches the
   database -- no campaign, no partial state.
"""

import csv
import io
from collections.abc import Iterable

from sqlalchemy.orm import Session

from app.core.errors import ValidationError
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.repositories.campaign_repository import CampaignRepository
from app.schemas.campaign_contact import BulkContactImportResult, BulkImportRowError
from app.services.audit_service import record_audit_event
from app.services.phone import InvalidPhoneNumberError, normalize_phone_number

MAX_IMPORT_ROWS = 10_000
CHUNK_SIZE = 500

_ACTOR = "api-client"


class ContactImportService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.campaigns = CampaignRepository(db)

    def import_csv(self, campaign_name: str, csv_bytes: bytes) -> BulkContactImportResult:
        rows = self._read_rows(csv_bytes)

        valid_normalized: list[tuple[str, str]] = []  # (raw, normalized)
        seen_normalized: set[str] = set()
        errors: list[BulkImportRowError] = []
        duplicates = 0
        total = 0

        for row_number, raw_phone in rows:
            total += 1
            if total > MAX_IMPORT_ROWS:
                raise ValidationError(
                    f"Import file exceeds the maximum of {MAX_IMPORT_ROWS} rows"
                )

            if not raw_phone or not raw_phone.strip():
                errors.append(BulkImportRowError(row=row_number, reason="missing_phone_number"))
                continue

            try:
                normalized = normalize_phone_number(raw_phone)
            except InvalidPhoneNumberError:
                errors.append(BulkImportRowError(row=row_number, reason="invalid_phone_number"))
                continue

            if normalized in seen_normalized:
                duplicates += 1
                continue

            seen_normalized.add(normalized)
            valid_normalized.append((raw_phone, normalized))

        if not valid_normalized:
            return BulkContactImportResult(
                campaign_id=None,
                total=total,
                created=0,
                duplicates=duplicates,
                invalid=len(errors),
                errors=errors,
            )

        campaign = self.campaigns.add(Campaign(name=campaign_name))

        created = 0
        for chunk_start in range(0, len(valid_normalized), CHUNK_SIZE):
            chunk = valid_normalized[chunk_start : chunk_start + CHUNK_SIZE]
            self.db.add_all(
                Contact(
                    campaign_id=campaign.id,
                    phone_number=raw,
                    normalized_phone_number=normalized,
                )
                for raw, normalized in chunk
            )
            self.db.flush()
            created += len(chunk)

        record_audit_event(
            self.db,
            actor=_ACTOR,
            action="campaign.bulk_import_completed",
            entity_type="campaign",
            entity_id=campaign.id,
            metadata={"created": created, "duplicates": duplicates, "invalid": len(errors)},
        )

        return BulkContactImportResult(
            campaign_id=campaign.id,
            total=total,
            created=created,
            duplicates=duplicates,
            invalid=len(errors),
            errors=errors,
        )

    @staticmethod
    def _read_rows(csv_bytes: bytes) -> Iterable[tuple[int, str]]:
        text_stream = io.StringIO(csv_bytes.decode("utf-8-sig"))
        reader = csv.DictReader(text_stream)
        if reader.fieldnames is None or "phone_number" not in reader.fieldnames:
            raise ValidationError("CSV file must have a 'phone_number' column")

        for row_number, row in enumerate(reader, start=2):  # header is row 1
            yield row_number, (row.get("phone_number") or "")
