"""Synthetic data for the CP15B restore drills. Uses the application's own models and
services (run with PYTHONPATH=backend and PRIMARY_DB_URL pointing at the DRILL cluster).
Every number is a synthetic Indian-region test number; nothing here is real data.

    drill_seed.py base            campaign + contacts incl. stranded DIALING / RETRY_SCHEDULED,
                                  an INITIATED attempt, a PROCESSING analysis, two DNC numbers
    drill_seed.py person NUMBER   a contact with a 3-turn transcript and an analysis
    drill_seed.py dnc NUMBER...   add numbers to the do-not-call list
    drill_seed.py erase NUMBER    erase a person through the CP15A service
"""

import sys
import uuid
from datetime import UTC, datetime, timedelta

from app.core.database import SessionLocal
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import ConversationMessage, ConversationSession
from app.models.enums import (
    AnalysisStatus,
    CallAttemptState,
    CampaignStatus,
    ContactStatus,
    ConversationRole,
    SuppressionSource,
)
from app.repositories.suppression_repository import SuppressionRepository
from app.services.audit_service import record_audit_event
from app.services.data_lifecycle.erasure import erase


def _campaign(db, name):
    campaign = Campaign(id=uuid.uuid4(), name=name, status=CampaignStatus.ACTIVE)
    db.add(campaign)
    db.flush()
    return campaign


def _contact(db, campaign, number, status):
    contact = Contact(
        id=uuid.uuid4(),
        campaign_id=campaign.id,
        phone_number=number,
        normalized_phone_number=number,
        status=status,
        attempt_count=1,
    )
    db.add(contact)
    db.flush()
    return contact


def _attempt(db, contact, state):
    attempt = CallAttempt(
        id=uuid.uuid4(),
        contact_id=contact.id,
        attempt_number=1,
        state=state,
        started_at=datetime.now(UTC) - timedelta(hours=3),
    )
    db.add(attempt)
    db.flush()
    return attempt


def seed_base(db):
    campaign = _campaign(db, "drill-base")
    dialing = _contact(db, campaign, "+919800000101", ContactStatus.DIALING)
    _contact(db, campaign, "+919800000102", ContactStatus.RETRY_SCHEDULED)
    for n in (103, 104, 105):
        _contact(db, campaign, f"+9198000001{n - 100:02d}", ContactStatus.PENDING)
    stuck = _attempt(db, dialing, CallAttemptState.INITIATED)
    db.add(
        CallAnalysis(
            id=uuid.uuid4(),
            call_attempt_id=stuck.id,
            contact_id=dialing.id,
            campaign_id=campaign.id,
            status=AnalysisStatus.PROCESSING,
            claim_token=uuid.uuid4(),
            lease_expires_at=datetime.now(UTC) - timedelta(hours=2),  # a dead worker's lease
            processing_started_at=datetime.now(UTC) - timedelta(hours=3),
        )
    )
    repo = SuppressionRepository(db)
    for number in ("+919800000201", "+919800000202"):
        repo.insert_if_absent(number, source=SuppressionSource.MANUAL_API, reason="drill seed")
    record_audit_event(
        db, actor="drill", action="drill.seed", entity_type="drill", entity_id=None, metadata={}
    )


def seed_person(db, number):
    campaign = _campaign(db, f"drill-person-{number[-4:]}")
    contact = _contact(db, campaign, number, ContactStatus.COMPLETED)
    attempt = _attempt(db, contact, CallAttemptState.ENDED_NORMALLY)
    session = ConversationSession(id=uuid.uuid4(), call_attempt_id=attempt.id)
    db.add(session)
    db.flush()
    for seq in (1, 2, 3):
        db.add(
            ConversationMessage(
                id=uuid.uuid4(),
                session_id=session.id,
                sequence=seq,
                role=ConversationRole.CONTACT,
                content=f"synthetic drill turn {seq}",
            )
        )
    db.add(
        CallAnalysis(
            id=uuid.uuid4(),
            call_attempt_id=attempt.id,
            contact_id=contact.id,
            campaign_id=campaign.id,
            status=AnalysisStatus.COMPLETED,
            summary="synthetic drill summary",
        )
    )


def main(argv):
    action, args = argv[1], argv[2:]
    with SessionLocal() as db:
        if action == "base":
            seed_base(db)
        elif action == "person":
            seed_person(db, args[0])
        elif action == "dnc":
            repo = SuppressionRepository(db)
            for number in args:
                repo.insert_if_absent(
                    number, source=SuppressionSource.MANUAL_API, reason="drill dnc"
                )
        elif action == "erase":
            erase(
                db,
                actor="drill",
                now=datetime.now(UTC),
                phone_number=args[0],
                reason="drill erasure",
            )
        else:
            raise SystemExit(__doc__)
        db.commit()


if __name__ == "__main__":
    main(sys.argv)
