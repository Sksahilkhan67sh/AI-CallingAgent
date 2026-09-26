"""Campaigns for the admin dashboard -- Checkpoint 07 §11-12.

Reuses CampaignService for the underlying list/get (no duplicated
lookup/validation logic); adds only the aggregated metrics the
dashboard needs on top, as server-side `GROUP BY`/`func.count` queries
-- never by downloading each campaign's contacts into the browser.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.contact import Contact
from app.models.enums import ContactStatus, InterestStatus
from app.schemas.admin import CampaignDetail, CampaignListItem, CampaignMetrics
from app.services.campaign_service import CampaignService

_ACTIVE_CONTACT_STATUSES = (
    ContactStatus.PENDING,
    ContactStatus.DIALING,
    ContactStatus.IN_CONVERSATION,
    ContactStatus.RECONNECTING,
)


def list_campaigns(
    db: Session, *, status=None, limit: int, offset: int
) -> tuple[list[CampaignListItem], int]:
    items, total = CampaignService(db).list_campaigns(status=status, limit=limit, offset=offset)
    campaign_ids = [c.id for c in items]
    contact_counts: dict[uuid.UUID, int] = {
        row.campaign_id: row.n
        for row in db.execute(
            select(Contact.campaign_id, func.count().label("n"))
            .where(Contact.campaign_id.in_(campaign_ids))
            .group_by(Contact.campaign_id)
        ).all()
    }
    return [
        CampaignListItem(
            id=c.id,
            name=c.name,
            status=c.status,
            created_at=c.created_at,
            contact_count=contact_counts.get(c.id, 0),
        )
        for c in items
    ], total


def get_campaign_detail(db: Session, campaign_id: uuid.UUID) -> CampaignDetail:
    campaign = CampaignService(db).get_campaign(campaign_id)  # raises NotFoundError

    contact_status_counts: dict[ContactStatus, int] = {
        row.status: row.n
        for row in db.execute(
            select(Contact.status, func.count().label("n"))
            .where(Contact.campaign_id == campaign_id)
            .group_by(Contact.status)
        ).all()
    }
    total_contacts = sum(contact_status_counts.values())

    attempts_total = db.execute(
        select(func.count())
        .select_from(CallAttempt)
        .join(Contact, CallAttempt.contact_id == Contact.id)
        .where(Contact.campaign_id == campaign_id)
    ).scalar_one()

    interested_and_score = db.execute(
        select(
            func.count().filter(CallAnalysis.interest_status == InterestStatus.INTERESTED),
            func.avg(CallAnalysis.lead_score),
        ).where(CallAnalysis.campaign_id == campaign_id)
    ).one()

    return CampaignDetail(
        id=campaign.id,
        name=campaign.name,
        status=campaign.status,
        created_at=campaign.created_at,
        metrics=CampaignMetrics(
            contacts=total_contacts,
            attempts=attempts_total,
            completed=contact_status_counts.get(ContactStatus.COMPLETED, 0),
            completed_partial=contact_status_counts.get(ContactStatus.COMPLETED_PARTIAL, 0),
            active=sum(contact_status_counts.get(s, 0) for s in _ACTIVE_CONTACT_STATUSES),
            retry_scheduled=contact_status_counts.get(ContactStatus.RETRY_SCHEDULED, 0),
            interested=interested_and_score[0] or 0,
            average_lead_score=(
                float(interested_and_score[1]) if interested_and_score[1] is not None else None
            ),
        ),
    )
