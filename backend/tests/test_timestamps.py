"""Timestamps must be UTC-aware, not naive (Checkpoint 01A Step 17)."""

from app.models.campaign import Campaign


def test_created_at_is_timezone_aware(db_session):
    campaign = Campaign(name="Timestamp test campaign")
    db_session.add(campaign)
    db_session.flush()
    db_session.refresh(campaign)

    assert campaign.created_at.tzinfo is not None
