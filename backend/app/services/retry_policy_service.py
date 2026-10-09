"""Retry policy: defaults, the effective policy, validation and the audited update (CP14, C5b).

Before CP14 a campaign without a `retry_policy` row had NO retries AND NO time restriction
(24x7 calling), and nothing ever created the row. Now:

* creating a campaign creates its policy in the same transaction (`ensure_policy`);
* `get_effective_policy` returns the DEFAULT policy (from settings) when a legacy campaign
  has no row, and every reader (dial eligibility, recovery dispatch, RecoveryManager) goes
  through it -- a missing row can never again mean "no window, no retries";
* the window is read in the campaign's timezone and clamped by the global hard bound.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import NotFoundError, ValidationError
from app.models.campaign import Campaign
from app.models.enums import MidCallDisconnectReason, NeverConnectedFailureReason
from app.models.retry_policy import (
    DEFAULT_MID_CALL_RULES,
    DEFAULT_NEVER_CONNECTED_RULES,
    RetryPolicy,
)
from app.repositories.campaign_repository import CampaignRepository
from app.schemas.retry_policy import RetryPolicyResponse, RetryPolicyUpdate
from app.services.audit_service import record_audit_event
from app.services.calling_window import (
    InvalidTimezoneError,
    is_dialable_now,
    load_timezone,
    next_window_open,
    window_within_bound,
)

_NEVER_CONNECTED_KEYS = frozenset(r.value for r in NeverConnectedFailureReason)
_MID_CALL_KEYS = frozenset(r.value for r in MidCallDisconnectReason)


def _default_values() -> dict:
    s = get_settings()
    return {
        "max_retries": s.default_max_retries,
        "retry_spacing_seconds": list(s.default_retry_spacing_seconds),
        "never_connected_rules": dict(DEFAULT_NEVER_CONNECTED_RULES),
        "mid_call_rules": dict(DEFAULT_MID_CALL_RULES),
        "window_start": s.default_calling_window_start,
        "window_end": s.default_calling_window_end,
    }


def build_default_policy(campaign_id: uuid.UUID | None = None) -> RetryPolicy:
    """An UNSAVED policy carrying the configured defaults (never added to a session)."""
    return RetryPolicy(campaign_id=campaign_id, **_default_values())


def get_effective_policy(db: Session, campaign: Campaign) -> RetryPolicy:
    stored = db.execute(
        select(RetryPolicy).where(RetryPolicy.campaign_id == campaign.id)
    ).scalar_one_or_none()
    return stored if stored is not None else build_default_policy(campaign.id)


def ensure_policy(db: Session, campaign_id: uuid.UUID) -> bool:
    """Create the campaign's policy row if missing. Idempotent and race-safe. True if this
    call created it."""
    created = db.execute(
        pg_insert(RetryPolicy)
        .values(id=uuid.uuid4(), campaign_id=campaign_id, **_default_values())
        .on_conflict_do_nothing(index_elements=[RetryPolicy.campaign_id])
        .returning(RetryPolicy.id)
    ).first()
    db.flush()
    return created is not None


# -- window helpers used by every call path ---------------------------------------------------


def window_is_open(campaign: Campaign, policy: RetryPolicy, now: datetime) -> bool:
    """May a call be placed at `now`? Raises InvalidTimezoneError on a corrupt zone --
    callers fail closed (hold the work), they never guess."""
    s = get_settings()
    return is_dialable_now(
        now,
        load_timezone(campaign.timezone),
        policy.window_start,
        policy.window_end,
        s.hard_calling_window_start,
        s.hard_calling_window_end,
    )


def next_open_time(campaign: Campaign, policy: RetryPolicy, now: datetime) -> datetime:
    s = get_settings()
    return next_window_open(
        now,
        load_timezone(campaign.timezone),
        policy.window_start,
        policy.window_end,
        s.hard_calling_window_start,
        s.hard_calling_window_end,
    )


# -- validation / admin update ------------------------------------------------------------------


def _validate_rules(rules: dict[str, bool], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(rules) - allowed)
    if unknown:
        raise ValidationError(f"{label} contains unknown reason(s): {', '.join(unknown)}")


def validate_policy_update(data: RetryPolicyUpdate) -> None:
    s = get_settings()
    if data.max_retries > s.max_retries_ceiling:
        raise ValidationError(f"max_retries must be between 0 and {s.max_retries_ceiling}")
    if len(data.retry_spacing_seconds) != data.max_retries:
        raise ValidationError("retry_spacing_seconds must have exactly max_retries entries")
    for spacing in data.retry_spacing_seconds:
        if not s.min_retry_backoff_seconds <= spacing <= s.max_retry_backoff_seconds:
            raise ValidationError(
                f"each retry spacing must be between {s.min_retry_backoff_seconds} and "
                f"{s.max_retry_backoff_seconds} seconds"
            )
    if data.window_start == data.window_end:
        raise ValidationError("window_start and window_end must differ")
    if not window_within_bound(
        data.window_start, data.window_end, s.hard_calling_window_start, s.hard_calling_window_end
    ):
        raise ValidationError(
            "the calling window must lie inside the allowed "
            f"{s.hard_calling_window_start:%H:%M}-{s.hard_calling_window_end:%H:%M} window"
        )
    if data.never_connected_rules is not None:
        _validate_rules(data.never_connected_rules, _NEVER_CONNECTED_KEYS, "never_connected_rules")
    if data.mid_call_rules is not None:
        _validate_rules(data.mid_call_rules, _MID_CALL_KEYS, "mid_call_rules")


def _snapshot(policy: RetryPolicy, timezone: str) -> dict:
    return {
        "max_retries": policy.max_retries,
        "retry_spacing_seconds": list(policy.retry_spacing_seconds),
        "window_start": policy.window_start.isoformat(),
        "window_end": policy.window_end.isoformat(),
        "timezone": timezone,
        "never_connected_rules": dict(policy.never_connected_rules),
        "mid_call_rules": dict(policy.mid_call_rules),
    }


def _response(campaign: Campaign, policy: RetryPolicy, persisted: bool) -> RetryPolicyResponse:
    s = get_settings()
    return RetryPolicyResponse(
        campaign_id=campaign.id,
        persisted=persisted,
        max_retries=policy.max_retries,
        retry_spacing_seconds=list(policy.retry_spacing_seconds),
        window_start=policy.window_start,
        window_end=policy.window_end,
        timezone=campaign.timezone,
        hard_window_start=s.hard_calling_window_start,
        hard_window_end=s.hard_calling_window_end,
        never_connected_rules=dict(policy.never_connected_rules),
        mid_call_rules=dict(policy.mid_call_rules),
    )


class RetryPolicyService:
    def __init__(self, db: Session, actor: str) -> None:
        self.db = db
        self.actor = actor
        self.campaigns = CampaignRepository(db)

    def _campaign(self, campaign_id: uuid.UUID, *, for_update: bool = False) -> Campaign:
        campaign = self.campaigns.get_by_id(campaign_id, for_update=for_update)
        if campaign is None:
            raise NotFoundError(f"Campaign {campaign_id} not found")
        return campaign

    def get(self, campaign_id: uuid.UUID) -> RetryPolicyResponse:
        campaign = self._campaign(campaign_id)
        stored = self.db.execute(
            select(RetryPolicy).where(RetryPolicy.campaign_id == campaign_id)
        ).scalar_one_or_none()
        return _response(campaign, stored or build_default_policy(campaign_id), stored is not None)

    def put(self, campaign_id: uuid.UUID, data: RetryPolicyUpdate) -> RetryPolicyResponse:
        validate_policy_update(data)
        if data.timezone is not None:
            try:
                load_timezone(data.timezone)
            except InvalidTimezoneError as exc:
                raise ValidationError("timezone must be a valid IANA timezone name") from exc

        # Row lock: two concurrent edits serialize and each audits a true before -> after.
        campaign = self._campaign(campaign_id, for_update=True)
        ensure_policy(self.db, campaign_id)
        policy = self.db.execute(
            select(RetryPolicy).where(RetryPolicy.campaign_id == campaign_id).with_for_update()
        ).scalar_one()

        before = _snapshot(policy, campaign.timezone)
        policy.max_retries = data.max_retries
        policy.retry_spacing_seconds = list(data.retry_spacing_seconds)
        policy.window_start = data.window_start
        policy.window_end = data.window_end
        if data.never_connected_rules is not None:
            policy.never_connected_rules = {
                **policy.never_connected_rules,
                **data.never_connected_rules,
            }
        if data.mid_call_rules is not None:
            policy.mid_call_rules = {**policy.mid_call_rules, **data.mid_call_rules}
        if data.timezone is not None:
            campaign.timezone = data.timezone
        self.db.flush()

        record_audit_event(
            self.db,
            actor=self.actor,
            action="retry_policy.updated",
            entity_type="campaign",
            entity_id=campaign_id,
            metadata={"before": before, "after": _snapshot(policy, campaign.timezone)},
        )
        return _response(campaign, policy, True)


