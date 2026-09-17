"""RetryPolicy defaults and the retry_spacing_seconds/max_retries CHECK
constraint (Database Design §2.2)."""

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.campaign import Campaign
from app.models.retry_policy import (
    DEFAULT_MID_CALL_RULES,
    DEFAULT_NEVER_CONNECTED_RULES,
    DEFAULT_RETRY_SPACING_SECONDS,
    RetryPolicy,
)


def _create_campaign(db_session) -> Campaign:
    campaign = Campaign(name="Retry policy test campaign")
    db_session.add(campaign)
    db_session.flush()
    return campaign


def test_retry_policy_defaults_match_spec(db_session):
    campaign = _create_campaign(db_session)

    policy = RetryPolicy(campaign_id=campaign.id)
    db_session.add(policy)
    db_session.flush()

    assert policy.max_retries == 2
    assert policy.retry_spacing_seconds == DEFAULT_RETRY_SPACING_SECONDS
    assert policy.never_connected_rules == DEFAULT_NEVER_CONNECTED_RULES
    assert policy.mid_call_rules == DEFAULT_MID_CALL_RULES
    # technical_issue must never appear in never_connected_rules (Call
    # State Machine §5)
    assert "technical_issue" not in policy.never_connected_rules


def test_retry_spacing_length_must_match_max_retries(db_session):
    campaign = _create_campaign(db_session)

    policy = RetryPolicy(
        campaign_id=campaign.id,
        max_retries=2,
        retry_spacing_seconds=[30],  # only 1 value for 2 retries
    )
    db_session.add(policy)

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_one_retry_policy_per_campaign(db_session):
    campaign = _create_campaign(db_session)
    db_session.add(RetryPolicy(campaign_id=campaign.id))
    db_session.flush()

    db_session.add(RetryPolicy(campaign_id=campaign.id))
    with pytest.raises(IntegrityError):
        db_session.flush()
