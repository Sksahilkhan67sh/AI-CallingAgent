"""Re-normalize stored phone numbers with the CP14 normalizer.

    python -m app.scripts.renormalize_phones            # DRY RUN: reads, reports, writes nothing
    python -m app.scripts.renormalize_phones --apply    # writes

Why: before CP14 a bare 10-digit number was read as +1 (US) and a leading trunk "0" was kept
("+09876..."). Those rows are wrong and, correctly, no longer dialable (region check).

What it does
* contact.normalized_phone_number is recomputed from the contact's RAW `phone_number` using
  its campaign's `default_region` (region restriction is NOT applied: the fix must also reach
  numbers that stay undialable).
* suppression.phone_number is recomputed the same way (from the linked contact's raw number,
  or from the stored value with DEFAULT_REGION when there is no contact). A linked
  suppression is only moved when its contact moved to the same number, so a skipped contact
  never ends up un-suppressed.
* COLLISIONS (two rows that would become the same number inside one campaign, or two
  suppression rows that would share a number) are NEVER merged, deleted or overwritten: they
  are listed and skipped. Re-running after resolving them picks them up.
* Idempotent: a second run reports everything unchanged. --apply commits per batch.
* Output contains ids and masked numbers only.

Deploy order: migrate -> dry run -> review -> --apply -> deploy the new code.
"""

import argparse
import sys
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import SessionLocal
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.suppression import Suppression
from app.services.phone import InvalidPhoneError, mask_phone_number, normalize_phone

_BATCH = 1000
_LIST_LIMIT = 50


@dataclass
class Tally:
    changed: int = 0
    unchanged: int = 0
    invalid: int = 0
    collisions: int = 0
    notes: list[str] = field(default_factory=list)

    def note(self, line: str) -> None:
        if len(self.notes) < _LIST_LIMIT:
            self.notes.append(line)


@dataclass
class Summary:
    applied: bool
    contacts: Tally = field(default_factory=Tally)
    suppressions: Tally = field(default_factory=Tally)


def _renormalize_contacts(
    db: Session, apply: bool, tally: Tally, planned_by_contact: dict[uuid.UUID, str]
) -> None:
    taken: dict[tuple[uuid.UUID, str], uuid.UUID] = {}  # (campaign, number) -> holder, in-run
    cursor: uuid.UUID | None = None
    while True:
        stmt = (
            select(
                Contact.id,
                Contact.campaign_id,
                Contact.phone_number,
                Contact.normalized_phone_number,
                Campaign.default_region,
            )
            .join(Campaign, Campaign.id == Contact.campaign_id)
            .order_by(Contact.id)
            .limit(_BATCH)
        )
        if cursor is not None:
            stmt = stmt.where(Contact.id > cursor)
        rows = db.execute(stmt).all()
        if not rows:
            return
        cursor = rows[-1].id

        wanted: list[tuple[uuid.UUID, uuid.UUID, str, str]] = []
        for row in rows:
            try:
                new = normalize_phone(row.phone_number, row.default_region).e164
            except InvalidPhoneError as exc:
                tally.invalid += 1
                tally.note(f"invalid contact={row.id} code={exc.code}")
                continue
            if new == row.normalized_phone_number:
                tally.unchanged += 1
                continue
            wanted.append((row.id, row.campaign_id, row.normalized_phone_number, new))

        # Who already holds each target number in its campaign right now?
        holders: dict[tuple[uuid.UUID, str], uuid.UUID] = {}
        if wanted:
            existing = db.execute(
                select(Contact.campaign_id, Contact.normalized_phone_number, Contact.id).where(
                    Contact.normalized_phone_number.in_({w[3] for w in wanted}),
                    Contact.campaign_id.in_({w[1] for w in wanted}),
                )
            ).all()
            holders = {(e.campaign_id, e.normalized_phone_number): e.id for e in existing}

        for contact_id, campaign_id, _old, new in wanted:
            holder = holders.get((campaign_id, new)) or taken.get((campaign_id, new))
            if holder is not None and holder != contact_id:
                tally.collisions += 1
                tally.note(
                    f"collision contact={contact_id} with contact={holder} "
                    f"campaign={campaign_id} number={mask_phone_number(new)}"
                )
                continue
            taken[(campaign_id, new)] = contact_id
            planned_by_contact[contact_id] = new
            tally.changed += 1
            if apply:
                db.execute(
                    update(Contact).where(Contact.id == contact_id).values(
                        normalized_phone_number=new
                    )
                )
        if apply:
            db.commit()
        else:
            db.rollback()


def _renormalize_suppressions(
    db: Session, apply: bool, tally: Tally, planned_by_contact: dict[uuid.UUID, str]
) -> None:
    default_region = get_settings().default_region
    taken: dict[str, uuid.UUID] = {}
    cursor: uuid.UUID | None = None
    while True:
        stmt = (
            select(
                Suppression.id,
                Suppression.contact_id,
                Suppression.phone_number,
                Contact.phone_number.label("raw"),
                Contact.normalized_phone_number.label("contact_number"),
                Campaign.default_region,
            )
            .outerjoin(Contact, Contact.id == Suppression.contact_id)
            .outerjoin(Campaign, Campaign.id == Contact.campaign_id)
            .order_by(Suppression.id)
            .limit(_BATCH)
        )
        if cursor is not None:
            stmt = stmt.where(Suppression.id > cursor)
        rows = db.execute(stmt).all()
        if not rows:
            return
        cursor = rows[-1].id

        wanted = []
        for row in rows:
            source_text = row.raw if row.raw is not None else row.phone_number
            region = row.default_region if row.default_region is not None else default_region
            try:
                new = normalize_phone(source_text, region).e164
            except InvalidPhoneError as exc:
                tally.invalid += 1
                tally.note(f"invalid suppression={row.id} code={exc.code}")
                continue
            if new == row.phone_number:
                tally.unchanged += 1
                continue
            if row.contact_id is not None:
                # Only move a linked row together with its contact (see module docstring).
                contact_now = planned_by_contact.get(row.contact_id, row.contact_number)
                if contact_now != new:
                    tally.collisions += 1
                    tally.note(
                        f"skipped suppression={row.id}: contact={row.contact_id} was not moved"
                    )
                    continue
            wanted.append((row.id, new))

        holders: dict[str, uuid.UUID] = {}
        if wanted:
            holders = {
                r.phone_number: r.id
                for r in db.execute(
                    select(Suppression.id, Suppression.phone_number).where(
                        Suppression.phone_number.in_({w[1] for w in wanted})
                    )
                )
            }
        for suppression_id, new in wanted:
            holder = holders.get(new) or taken.get(new)
            if holder is not None and holder != suppression_id:
                tally.collisions += 1
                tally.note(
                    f"collision suppression={suppression_id} with suppression={holder} "
                    f"number={mask_phone_number(new)}"
                )
                continue
            taken[new] = suppression_id
            tally.changed += 1
            if apply:
                db.execute(
                    update(Suppression)
                    .where(Suppression.id == suppression_id)
                    .values(phone_number=new)
                )
        if apply:
            db.commit()
        else:
            db.rollback()


def run(db: Session, *, apply: bool) -> Summary:
    summary = Summary(applied=apply)
    planned: dict[uuid.UUID, str] = {}
    _renormalize_contacts(db, apply, summary.contacts, planned)
    _renormalize_suppressions(db, apply, summary.suppressions, planned)
    return summary


def _print(summary: Summary) -> None:
    mode = "APPLIED" if summary.applied else "DRY RUN (nothing written; use --apply)"
    print(f"renormalize_phones: {mode}")
    for name, t in (("contacts", summary.contacts), ("suppressions", summary.suppressions)):
        print(
            f"  {name}: changed={t.changed} unchanged={t.unchanged} "
            f"invalid={t.invalid} collisions={t.collisions}"
        )
        for line in t.notes:
            print(f"    - {line}")
        if t.invalid + t.collisions > len(t.notes):
            print(f"    ... {t.invalid + t.collisions - len(t.notes)} more not listed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = parser.parse_args(argv)
    with SessionLocal() as db:
        summary = run(db, apply=args.apply)
    _print(summary)
    return 0


if __name__ == "__main__":
    sys.exit(main())
