"""Campaign-membership eligibility -- Checkpoint 02 Step 16 -- plus the
final dial-time eligibility check -- Checkpoint 03 Step 6. Kept in one
file since they share the same small set of dependencies and result
type, but are deliberately two different checks: campaign-membership
eligibility answers "can this contact belong to this campaign" at
management time; dial eligibility answers "is it still safe to dial
this contact right now" immediately before a worker places the call --
a contact can be one without the other (e.g. eligible for membership
in a paused campaign, but not eligible to dial while paused).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus
from app.repositories.suppression_repository import SuppressionRepository

if TYPE_CHECKING:
    from app.models.retry_policy import RetryPolicy


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


class DialEligibilityService:
    """Checkpoint 03 Step 6 -- the *final* check, run immediately before
    dialing, not just at queue-admission time. A contact being eligible
    when it entered the queue does not guarantee it still is: the
    campaign may have been paused, the contact suppressed, or the
    calling window closed in the meantime.

    Calling-window note (Step 7): no per-campaign timezone field exists
    anywhere in the reconciled schema, so this compares against UTC --
    documented as a known limitation in docs/CHECKPOINT-03-NOTES.md, not
    silently assumed correct. A campaign with no `retry_policy` row (most
    campaigns, since one isn't auto-created) has no window restriction.
    """

    def __init__(self, suppressions: SuppressionRepository) -> None:
        self.suppressions = suppressions

    def check(
        self,
        contact: Contact,
        campaign: Campaign,
        retry_policy: "RetryPolicy | None",
        *,
        now: datetime | None = None,
    ) -> EligibilityResult:
        if campaign.status != CampaignStatus.ACTIVE:
            return EligibilityResult(
                False, f"Campaign is {campaign.status.value}, not active"
            )

        if contact.status not in (ContactStatus.PENDING, ContactStatus.DIALING):
            return EligibilityResult(
                False, f"Contact is {contact.status.value}, not eligible to dial"
            )

        if self.suppressions.is_suppressed(contact.normalized_phone_number):
            return EligibilityResult(False, "Contact is suppressed")

        if retry_policy is not None:
            current_time = (now or datetime.now(UTC)).time()
            if not (retry_policy.window_start <= current_time <= retry_policy.window_end):
                return EligibilityResult(False, "Outside calling window")

        return EligibilityResult(True)
