"""Campaign-membership eligibility -- Checkpoint 02 Step 16.

This is a campaign-management-time check only: "can this contact
belong to this campaign." It is intentionally *not* the final
dialing-time check a future queue/dialer will run immediately before
placing a call -- that's a later checkpoint's concern and may consider
things (calling window, rate limits, concurrent-call caps) that have
nothing to do with campaign membership.
"""

from dataclasses import dataclass

from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus
from app.repositories.suppression_repository import SuppressionRepository


@dataclass
class EligibilityResult:
    eligible: bool
    reason: str | None = None


# Campaign states that can still accept members. A completed campaign
# is done; a paused campaign is still meaningfully "the same campaign"
# and can accept membership changes even while dialing is paused.
_MEMBERSHIP_ELIGIBLE_CAMPAIGN_STATUSES = {
    CampaignStatus.DRAFT,
    CampaignStatus.ACTIVE,
    CampaignStatus.PAUSED,
}


class CampaignEligibilityService:
    def __init__(self, suppressions: SuppressionRepository) -> None:
        self.suppressions = suppressions

    def check(self, contact: Contact, campaign: Campaign) -> EligibilityResult:
        if campaign.status not in _MEMBERSHIP_ELIGIBLE_CAMPAIGN_STATUSES:
            return EligibilityResult(
                False, f"Campaign is {campaign.status.value} and not accepting members"
            )

        if contact.status == ContactStatus.CLOSED:
            return EligibilityResult(False, "Contact is closed/deactivated")

        if self.suppressions.is_suppressed(contact.normalized_phone_number):
            return EligibilityResult(False, "Contact is suppressed")

        return EligibilityResult(True)
