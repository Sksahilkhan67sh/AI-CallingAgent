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

from app.core.config import get_settings
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus
from app.repositories.suppression_repository import SuppressionRepository
from app.services.calling_window import InvalidTimezoneError
from app.services.phone import is_dialable_region
from app.services.retry_policy_service import build_default_policy, window_is_open

if TYPE_CHECKING:
    from app.models.retry_policy import RetryPolicy


# Machine-readable reasons. The dialer treats TRANSIENT ones as "not now" (never acked and
# lost); everything else that is not eligible is permanent for this job.
OUTSIDE_WINDOW = "outside_calling_window"
REGION_NOT_ALLOWED = "region_not_allowed"


@dataclass
class EligibilityResult:
    eligible: bool
    reason: str | None = None
    code: str | None = None

    @property
    def transient(self) -> bool:
        return self.code == OUTSIDE_WINDOW


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

    Calling window (CP14): read in the CAMPAIGN's timezone via the shared
    `calling_window` functions and clamped by the global hard bound. A campaign
    with no `retry_policy` row is judged by the DEFAULT policy -- a missing row
    never means "no window". The destination's region is re-checked against
    ALLOWED_DIAL_REGIONS here too, so a legacy row written before the rule existed
    cannot be dialed.
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

        # RetryScheduled: Checkpoint 05's RecoveryManager sets this
        # right before scheduling a retry -- it's a legitimate pre-dial
        # status, not just Pending/Dialing from a first attempt.
        if contact.status not in (
            ContactStatus.PENDING,
            ContactStatus.DIALING,
            ContactStatus.RETRY_SCHEDULED,
        ):
            return EligibilityResult(
                False, f"Contact is {contact.status.value}, not eligible to dial"
            )

        if self.suppressions.is_suppressed(contact.normalized_phone_number):
            return EligibilityResult(False, "Contact is suppressed")

        if not is_dialable_region(
            contact.normalized_phone_number, get_settings().allowed_dial_regions
        ):
            return EligibilityResult(
                False, "Destination region is not allowed", REGION_NOT_ALLOWED
            )

        policy = retry_policy if retry_policy is not None else build_default_policy(campaign.id)
        try:
            open_now = window_is_open(campaign, policy, now or datetime.now(UTC))
        except InvalidTimezoneError:
            # A corrupt zone must never be guessed at: hold the work, do not dial.
            return EligibilityResult(False, "Outside calling window", OUTSIDE_WINDOW)
        if not open_now:
            return EligibilityResult(False, "Outside calling window", OUTSIDE_WINDOW)

        return EligibilityResult(True)
