"""Builders for the CP15A data-lifecycle tests (synthetic data only; library-valid numbers)."""

import itertools
import os
import uuid
from datetime import UTC, datetime, timedelta

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
)
from app.models.working_memory_snapshot import WorkingMemorySnapshot
from tests.phone_helpers import valid_in

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
_numbers = itertools.count(70_000_000 + (os.getpid() % 1000) * 10_000)


def next_number() -> str:
    return valid_in(next(_numbers))


def days_ago(days: float, now: datetime = NOW) -> datetime:
    return now - timedelta(days=days)


def make_campaign(db, *, status=CampaignStatus.COMPLETED, closed_days_ago=None, hold=False):
    campaign = Campaign(
        id=uuid.uuid4(),
        name="synthetic",
        status=status,
        legal_hold=hold,
        closed_at=None if closed_days_ago is None else days_ago(closed_days_ago),
    )
    db.add(campaign)
    db.flush()
    return campaign


def make_contact(db, campaign, number=None, *, hold=False, status=ContactStatus.COMPLETED):
    number = number or next_number()
    contact = Contact(
        id=uuid.uuid4(),
        campaign_id=campaign.id,
        phone_number=number,
        normalized_phone_number=number,
        status=status,
        attempt_count=1,
        legal_hold=hold,
        updated_at=days_ago(1000),
    )
    db.add(contact)
    db.flush()
    return contact


def make_attempt(db, contact):
    attempt = CallAttempt(
        id=uuid.uuid4(),
        contact_id=contact.id,
        attempt_number=1,
        state=CallAttemptState.ENDED_NORMALLY,
        duration_seconds=42.0,
    )
    db.add(attempt)
    db.flush()
    return attempt


def add_transcript(db, attempt, *, age_days: float, turns: int = 3):
    session = ConversationSession(id=uuid.uuid4(), call_attempt_id=attempt.id)
    db.add(session)
    db.flush()
    for seq in range(1, turns + 1):
        db.add(
            ConversationMessage(
                id=uuid.uuid4(),
                session_id=session.id,
                sequence=seq,
                role=ConversationRole.CONTACT,
                content=f"synthetic turn {seq}",
                created_at=days_ago(age_days),
            )
        )
    db.flush()
    return session


def add_snapshot(db, attempt, *, age_days: float):
    snap = WorkingMemorySnapshot(
        id=uuid.uuid4(),
        attempt_id=attempt.id,
        captured_entities={"k": "synthetic"},
        objections_raised=["synthetic"],
        last_agent_utterance="synthetic",
        snapshotted_at=days_ago(age_days),
    )
    db.add(snap)
    db.flush()
    return snap


def add_analysis(db, attempt, contact, *, age_days: float, status=AnalysisStatus.COMPLETED):
    analysis = CallAnalysis(
        id=uuid.uuid4(),
        call_attempt_id=attempt.id,
        contact_id=contact.id,
        campaign_id=contact.campaign_id,
        status=status,
        summary="synthetic summary",
        feedback="synthetic feedback",
        key_facts=["synthetic"],
        objections=["synthetic"],
        customer_needs=["synthetic"],
        lead_score=70,
        created_at=days_ago(age_days),
    )
    db.add(analysis)
    db.flush()
    return analysis


# CP15A: production now refuses to boot unless every retention period and the erasure pepper
# are set explicitly. Existing production-config fixtures spread this in so they keep testing
# what they always tested.
CP15_PRODUCTION_SETTINGS = dict(
    retention_transcript_days=180,
    retention_analysis_days=365,
    retention_contact_days=365,
    retention_audit_log_days=730,
    retention_processed_event_days=90,
    retention_run_history_days=365,
    erasure_fingerprint_pepper="Pp9Zk2Lm7Qw4Xr1Ty6Uv3Bn8Cd5Ef0Gh2Jk",
)
