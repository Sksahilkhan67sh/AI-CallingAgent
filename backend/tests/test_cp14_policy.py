# ruff: noqa: F811
"""CP14 (C5b) -- every campaign has a retry policy; a missing row can never mean
"no window, no retries"; the admin API edits it under the CP11 patterns."""

import io
from datetime import time

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.models.campaign import Campaign
from app.models.enums import CampaignStatus
from app.models.retry_policy import RetryPolicy
from app.services.retry_policy_service import (
    build_default_policy,
    ensure_policy,
    get_effective_policy,
)
from tests.cp14_helpers import production_window  # noqa: F401  (fixture)


def _rows(db, campaign_id) -> int:
    return db.execute(
        select(func.count()).select_from(RetryPolicy).where(RetryPolicy.campaign_id == campaign_id)
    ).scalar_one()


def _new_campaign(client, **kw):
    response = client.post("/api/v1/campaigns", json={"name": "cp14 policy", **kw})
    assert response.status_code == 201, response.text
    return response.json()


GOOD_BODY = {
    "max_retries": 2,
    "retry_spacing_seconds": [1800, 7200],
    "window_start": "10:00:00",
    "window_end": "18:00:00",
}


# -- born with the campaign -----------------------------------------------------------------------------


def test_creating_a_campaign_creates_its_policy_with_the_configured_defaults(
    client, db_session, production_window
):
    campaign = _new_campaign(client)
    assert _rows(db_session, campaign["id"]) == 1
    policy = db_session.execute(
        select(RetryPolicy).where(RetryPolicy.campaign_id == campaign["id"])
    ).scalar_one()
    s = get_settings()
    assert policy.max_retries == s.default_max_retries == 2
    assert policy.retry_spacing_seconds == s.default_retry_spacing_seconds == [3600, 14400]
    assert (policy.window_start, policy.window_end) == (time(9), time(21))
    assert (campaign["timezone"], campaign["default_region"]) == ("Asia/Kolkata", "IN")


def test_csv_import_also_creates_the_policy(client, db_session):
    response = client.post(
        "/api/v1/campaigns/import",
        data={"name": "import policy"},
        files={"file": ("c.csv", io.BytesIO(b"phone_number\n9876543210\n"), "text/csv")},
    )
    assert response.status_code == 201
    assert _rows(db_session, response.json()["campaign_id"]) == 1


def test_the_default_backoffs_are_production_safe_not_development_values():
    s = get_settings()
    assert min(s.default_retry_spacing_seconds) >= s.min_retry_backoff_seconds >= 300
    assert s.default_retry_spacing_seconds != [30, 600]  # the old dev values


# -- timezone / region validation on campaigns ---------------------------------------------------------------


@pytest.mark.parametrize("tz", ["Mars/Base", "IST", "", "../etc/passwd", "x" * 90])
def test_campaign_create_rejects_a_bad_timezone(client, tz):
    response = client.post("/api/v1/campaigns", json={"name": "x", "timezone": tz})
    assert response.status_code == 422


@pytest.mark.parametrize("region", ["XX", "INDIA", "", "1N"])
def test_campaign_create_rejects_a_bad_region(client, region):
    response = client.post("/api/v1/campaigns", json={"name": "x", "default_region": region})
    assert response.status_code == 422


def test_campaign_create_accepts_and_normalizes_overrides(client):
    campaign = _new_campaign(client, timezone="America/New_York", default_region="us")
    assert (campaign["timezone"], campaign["default_region"]) == ("America/New_York", "US")


def test_campaign_timezone_is_editable_but_region_is_not(client):
    campaign = _new_campaign(client)
    ok = client.patch(f"/api/v1/campaigns/{campaign['id']}", json={"timezone": "Asia/Dubai"})
    assert ok.status_code == 200 and ok.json()["timezone"] == "Asia/Dubai"
    bad = client.patch(f"/api/v1/campaigns/{campaign['id']}", json={"timezone": "Nope/Nope"})
    assert bad.status_code == 422
    ignored = client.patch(f"/api/v1/campaigns/{campaign['id']}", json={"default_region": "US"})
    assert ignored.json()["default_region"] == "IN"  # existing contacts were parsed with it


# -- legacy campaigns -----------------------------------------------------------------------------------------


def test_a_legacy_campaign_without_a_row_gets_the_default_policy(db_session, production_window):
    legacy = Campaign(name="legacy", status=CampaignStatus.ACTIVE)
    db_session.add(legacy)
    db_session.flush()
    assert _rows(db_session, legacy.id) == 0
    policy = get_effective_policy(db_session, legacy)
    assert policy.max_retries == 2 and policy.retry_spacing_seconds == [3600, 14400]
    assert (policy.window_start, policy.window_end) == (time(9), time(21))
    assert _rows(db_session, legacy.id) == 0  # reading never writes


def test_a_stored_row_wins_over_the_default(db_session):
    campaign = Campaign(name="custom", status=CampaignStatus.ACTIVE)
    db_session.add(campaign)
    db_session.flush()
    db_session.add(RetryPolicy(campaign_id=campaign.id, max_retries=0, retry_spacing_seconds=[]))
    db_session.flush()
    assert get_effective_policy(db_session, campaign).max_retries == 0


def test_ensure_policy_is_idempotent(db_session):
    campaign = Campaign(name="idem", status=CampaignStatus.ACTIVE)
    db_session.add(campaign)
    db_session.flush()
    assert ensure_policy(db_session, campaign.id) is True
    assert ensure_policy(db_session, campaign.id) is False
    assert _rows(db_session, campaign.id) == 1


def test_default_policy_object_is_never_attached_to_the_session(db_session):
    policy = build_default_policy(None)
    assert policy not in db_session


def test_the_backfill_script_creates_only_missing_rows(db_session):
    from app.scripts import backfill_retry_policies as script

    with_row = Campaign(name="has", status=CampaignStatus.ACTIVE)
    without = Campaign(name="lacks", status=CampaignStatus.ACTIVE)
    db_session.add_all([with_row, without])
    db_session.flush()
    db_session.add(RetryPolicy(campaign_id=with_row.id, max_retries=1, retry_spacing_seconds=[900]))
    db_session.flush()

    dry = script.run(db_session, apply=False)
    assert dry["created"] == 0 and _rows(db_session, without.id) == 0
    applied = script.run(db_session, apply=True)
    assert applied["created"] >= 1 and _rows(db_session, without.id) == 1
    assert script.run(db_session, apply=True)["created"] == 0  # idempotent
    policy = db_session.execute(
        select(RetryPolicy).where(RetryPolicy.campaign_id == with_row.id)
    ).scalar_one()
    assert policy.max_retries == 1  # an existing row is never touched


# -- the admin API --------------------------------------------------------------------------------------------------


def _url(campaign_id) -> str:
    return f"/api/v1/admin/campaigns/{campaign_id}/retry-policy"


def test_authz_anon_401_operator_reads_but_cannot_write(client, anon_client, operator_client):
    campaign = _new_campaign(client)
    assert anon_client.get(_url(campaign["id"])).status_code == 401
    assert anon_client.put(_url(campaign["id"]), json=GOOD_BODY).status_code == 401
    assert operator_client.get(_url(campaign["id"])).status_code == 200
    assert operator_client.put(_url(campaign["id"]), json=GOOD_BODY).status_code == 403
    assert client.put(_url(campaign["id"]), json=GOOD_BODY).status_code == 200


def test_get_reports_whether_the_row_is_stored(client, db_session):
    legacy = Campaign(name="legacy api", status=CampaignStatus.ACTIVE)
    db_session.add(legacy)
    db_session.commit()
    body = client.get(_url(legacy.id)).json()
    assert body["persisted"] is False and body["max_retries"] == 2
    assert body["timezone"] == "Asia/Kolkata"
    assert body["hard_window_start"] and body["hard_window_end"]
    assert client.get(_url(_new_campaign(client)["id"])).json()["persisted"] is True


def test_put_updates_and_audits_before_and_after(client, db_session, production_window):
    campaign = _new_campaign(client)
    response = client.put(_url(campaign["id"]), json={**GOOD_BODY, "timezone": "Asia/Dubai"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["retry_spacing_seconds"] == [1800, 7200] and body["timezone"] == "Asia/Dubai"
    assert _rows(db_session, campaign["id"]) == 1

    audit = db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "retry_policy.updated", AuditLog.entity_id == campaign["id"]
        )
    ).scalar_one()
    assert audit.actor == "test-admin"
    before, after = audit.event_metadata["before"], audit.event_metadata["after"]
    assert before["retry_spacing_seconds"] == [3600, 14400]
    assert before["window_start"] == "09:00:00" and before["timezone"] == "Asia/Kolkata"
    assert after["retry_spacing_seconds"] == [1800, 7200]
    assert after["window_start"] == "10:00:00" and after["timezone"] == "Asia/Dubai"


def test_put_on_a_legacy_campaign_creates_the_row(client, db_session):
    legacy = Campaign(name="legacy put", status=CampaignStatus.ACTIVE)
    db_session.add(legacy)
    db_session.commit()
    assert client.put(_url(legacy.id), json=GOOD_BODY).status_code == 200
    assert _rows(db_session, legacy.id) == 1


def test_partial_rule_updates_merge_onto_the_current_rules(client):
    campaign = _new_campaign(client)
    first = client.put(
        _url(campaign["id"]), json={**GOOD_BODY, "never_connected_rules": {"no_answer": False}}
    ).json()
    again = client.put(_url(campaign["id"]), json=GOOD_BODY).json()  # rules omitted
    assert first["never_connected_rules"]["no_answer"] is False
    assert again["never_connected_rules"]["no_answer"] is False  # unchanged, not reset


@pytest.mark.parametrize(
    ("patch", "why"),
    [
        ({"max_retries": 6, "retry_spacing_seconds": [3600] * 6}, "above the ceiling"),
        ({"max_retries": -1, "retry_spacing_seconds": []}, "negative"),
        ({"retry_spacing_seconds": [3600]}, "spacing length != max_retries"),
        ({"retry_spacing_seconds": [60, 3600]}, "below the backoff floor"),
        ({"retry_spacing_seconds": [3600, 10**9]}, "above the backoff ceiling"),
        ({"window_start": "08:00:00"}, "starts before the hard bound"),
        ({"window_end": "22:00:00"}, "ends after the hard bound"),
        ({"window_start": "22:00:00", "window_end": "06:00:00"}, "overnight can't fit 09-21"),
        ({"window_start": "12:00:00", "window_end": "12:00:00"}, "start == end"),
        ({"timezone": "Mars/Base"}, "bad timezone"),
        ({"never_connected_rules": {"made_up": True}}, "unknown rule"),
        ({"mid_call_rules": {"nope": True}}, "unknown rule"),
        ({"window_start": "25:00:00"}, "unparseable time"),
    ],
)
def test_put_validation(client, db_session, production_window, patch, why):
    campaign = _new_campaign(client)
    response = client.put(_url(campaign["id"]), json={**GOOD_BODY, **patch})
    assert response.status_code == 422, (why, response.text)
    stored = db_session.execute(
        select(RetryPolicy).where(RetryPolicy.campaign_id == campaign["id"])
    ).scalar_one()
    assert stored.retry_spacing_seconds == [3600, 14400]  # a rejected edit changes nothing


def test_put_unknown_campaign_is_404(client):
    response = client.put(_url("00000000-0000-0000-0000-000000000000"), json=GOOD_BODY)
    assert response.status_code == 404


def test_put_draws_from_the_mutation_budget(client, monkeypatch):
    campaign = _new_campaign(client)
    monkeypatch.setattr(get_settings(), "mutation_rate_limit_per_minute", 3)  # +1: creating the campaign above
    codes = [client.put(_url(campaign["id"]), json=GOOD_BODY).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
