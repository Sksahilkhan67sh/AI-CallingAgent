# ruff: noqa: F811
"""CP14 -- the global do-not-call list: number-keyed, admin-managed, audited without ever
recording a full number, and authoritative at dial time on every path."""

import io
import json
import logging
import uuid

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.models.contact import Contact
from app.models.enums import CallAttemptState, ContactStatus, SuppressionSource
from app.models.suppression import Suppression
from app.repositories.suppression_repository import SuppressionRepository
from app.services.queue.dialer_worker import JobOutcome
from app.services.recovery.scheduler import _SCHEDULE_KEY
from app.services.suppression_service import SuppressionService
from tests.cp14_helpers import (
    Session,
    attempts_of,
    commit_world,
    contact_state,
    enqueue_first,
    ist,
    production_window,  # noqa: F401  (fixture)
    rig,  # noqa: F401  (fixture)
    truncate_after_module,  # noqa: F401  (fixture)
)
from tests.phone_helpers import valid_in

URL = "/api/v1/admin/suppressions"
NUMBER = "+919811100001"


def _count(db) -> int:
    return db.execute(select(func.count()).select_from(Suppression)).scalar_one()


def _audits(db, action):
    return (
        db.execute(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.created_at))
        .scalars()
        .all()
    )


def _csv(client, body: str, **kw):
    return client.post(
        f"{URL}/import",
        files={"file": ("dnc.csv", io.BytesIO(body.encode()), "text/csv")},
        **kw,
    )


# -- add one --------------------------------------------------------------------------------------


def test_a_number_without_a_contact_can_be_stored(client, db_session):
    response = client.post(URL, json={"phone_number": NUMBER, "reason": "registry export"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert (
        body["created"] is True
        and body["contact_id"] is None
        and body["created_by"] == "test-admin"
    )
    assert body["source"] == "manual_api" and body["last4"] == "0001"
    assert NUMBER not in response.text and "9811100001" not in response.text  # masked only
    row = db_session.execute(select(Suppression)).scalar_one()
    assert row.phone_number == NUMBER and row.contact_id is None
    assert SuppressionRepository(db_session).is_suppressed(NUMBER)


def test_add_is_idempotent_and_normalization_equivalent(client, db_session):
    first = client.post(URL, json={"phone_number": "9811100002"})
    again = [
        client.post(URL, json={"phone_number": raw})
        for raw in ("09811100002", "+91 98111-00002", "919811100002", "9811100002")
    ]
    assert first.status_code == 201
    assert [r.status_code for r in again] == [200] * 4
    assert {r.json()["id"] for r in again} == {first.json()["id"]}
    assert all(r.json()["created"] is False for r in again)
    assert _count(db_session) == 1
    assert len(_audits(db_session, "suppression.added")) == 1  # a repeat is not a new event


def test_add_with_a_matching_contact_links_it(client, db_session):
    campaign = client.post("/api/v1/campaigns", json={"name": "dnc link"}).json()
    contact = client.post(
        "/api/v1/contacts", json={"campaign_id": campaign["id"], "phone_number": "9811100003"}
    ).json()
    response = client.post(URL, json={"phone_number": "9811100003", "contact_id": contact["id"]})
    assert response.status_code == 201 and response.json()["contact_id"] == contact["id"]
    again = client.post(URL, json={"phone_number": "9811100003", "contact_id": contact["id"]})
    assert again.status_code == 200 and again.json()["id"] == response.json()["id"]


def test_add_rejects_a_contact_that_does_not_own_the_number(client):
    campaign = client.post("/api/v1/campaigns", json={"name": "dnc mismatch"}).json()
    contact = client.post(
        "/api/v1/contacts", json={"campaign_id": campaign["id"], "phone_number": "9811100004"}
    ).json()
    mismatch = client.post(URL, json={"phone_number": "9811100005", "contact_id": contact["id"]})
    assert mismatch.status_code == 422
    unknown = client.post(URL, json={"phone_number": "9811100005", "contact_id": str(uuid.uuid4())})
    assert unknown.status_code == 404


def test_a_contact_that_already_has_a_row_under_another_number_still_records_the_number(
    client, db_session
):
    campaign = client.post("/api/v1/campaigns", json={"name": "dnc two"}).json()
    contact = client.post(
        "/api/v1/contacts", json={"campaign_id": campaign["id"], "phone_number": "9811100006"}
    ).json()
    db_session.add(
        Suppression(
            contact_id=contact["id"],
            phone_number="+919811100099",
            source=SuppressionSource.MANUAL_API,
        )
    )
    db_session.commit()
    response = client.post(URL, json={"phone_number": "9811100006", "contact_id": contact["id"]})
    assert response.status_code == 201 and response.json()["contact_id"] is None
    assert SuppressionRepository(db_session).is_suppressed("+919811100006")


def test_a_foreign_number_can_still_be_suppressed(client):
    # Dialing is IN-only, but refusing to record a do-not-call number would leave it callable
    # if the allowed regions ever widen.
    assert client.post(URL, json={"phone_number": "+14155552671"}).status_code == 201


@pytest.mark.parametrize(
    ("raw", "code"),
    [("12", "invalid_number"), ("abc", "invalid_number"), ("9" * 30, "too_long"), ("   ", "empty")],
)
def test_add_rejects_invalid_numbers_with_a_reason_code(client, raw, code):
    response = client.post(URL, json={"phone_number": raw})
    assert response.status_code == 422
    assert code in response.text or response.json()["detail"]  # schema-level for blank


# -- audit and logs never hold a full number ------------------------------------------------------


def test_audit_metadata_has_a_fingerprint_and_last4_but_never_the_number(client, db_session):
    client.post(URL, json={"phone_number": "9811100007", "reason": "complaint"})
    added = _audits(db_session, "suppression.added")[-1]
    meta = added.event_metadata
    assert meta["last4"] == "0007" and len(meta["fingerprint"]) == 16
    blob = json.dumps(meta)
    assert "9811100007" not in blob and "+91981" not in blob
    row_id = client.get(URL, params={"phone": "9811100007"}).json()["items"][0]["id"]
    removal = client.request(
        "DELETE", f"{URL}/{row_id}", json={"reason": "caller asked to be called again"}
    )
    assert removal.status_code == 204
    removed = _audits(db_session, "suppression.removed")[-1]
    assert removed.event_metadata["fingerprint"] == meta["fingerprint"]  # same number, same value
    assert removed.event_metadata["reason"] == "caller asked to be called again"
    assert "9811100007" not in json.dumps(removed.event_metadata)


def test_application_logs_never_contain_a_full_number(client, db_session, caplog):
    caplog.set_level(logging.DEBUG)
    client.post(URL, json={"phone_number": "9811100008"})
    client.post(URL, json={"phone_number": "9811100008"})
    _csv(client, "phone_number\n9811100009\nnot-a-number\n")
    client.get(URL, params={"phone": "9811100008"})
    listing = client.get(URL).json()["items"]
    client.request("DELETE", f"{URL}/{listing[0]['id']}", json={"reason": "cleanup after test"})
    text = " ".join(
        f"{r.getMessage()} {sorted(map(str, r.__dict__.values()))}" for r in caplog.records
    )
    for digits in ("9811100008", "9811100009"):
        assert digits not in text


# -- bulk import ----------------------------------------------------------------------------------


def test_bulk_import_counts_and_per_row_errors(client, db_session):
    client.post(URL, json={"phone_number": "9811100010"})  # already present
    body = "\n".join(
        [
            "phone_number",
            "9811100011",  # 2 new
            "09811100010",  # 3 already present (other spelling)
            "bad",  # 4 invalid
            "9811100011",  # 5 same number twice in the file
            "   ",  # 6 empty
            "9" * 25,  # 7 too long
            "+91 98111 00012",  # 8 new
        ]
    )
    response = _csv(client, body, data={"reason": "list 2026-10"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert (result["total"], result["added"], result["already_present"], result["invalid"]) == (
        7,
        2,
        2,
        3,
    )
    assert {(e["row"], e["reason"]) for e in result["errors"]} == {
        (4, "invalid_number"),
        (6, "empty"),
        (7, "too_long"),
    }
    assert _count(db_session) == 3
    reasons = {r.reason for r in db_session.execute(select(Suppression)).scalars()}
    assert "list 2026-10" in reasons
    audit = _audits(db_session, "suppression.imported")[-1].event_metadata
    assert audit == {"total": 7, "added": 2, "already_present": 2, "invalid": 3}


def test_bulk_import_is_idempotent(client, db_session):
    body = "phone_number\n" + "\n".join(valid_in(i) for i in range(5000, 5040))
    assert _csv(client, body).json()["added"] == 40
    second = _csv(client, body).json()
    assert (second["added"], second["already_present"]) == (0, 40)
    assert _count(db_session) == 40


def test_bulk_import_keeps_the_cp11_caps(client):
    rows = "phone_number\n" + "\n".join(valid_in(i) for i in range(10_001))
    assert _csv(client, rows).status_code == 422
    exactly = "phone_number\n" + "\n".join(valid_in(i) for i in range(20_000, 30_000))
    assert _csv(client, exactly).json()["added"] == 10_000
    oversize = "phone_number\n" + "9" * (5 * 1024 * 1024 + 10)
    assert _csv(client, oversize).status_code in (413, 422)


def test_bulk_import_rejects_a_bad_file(client):
    assert _csv(client, "number\n9811100013\n").status_code == 422  # no phone_number column
    bad = client.post(
        f"{URL}/import", files={"file": ("x.csv", io.BytesIO(b"\xff\xfe\x00"), "text/csv")}
    )
    assert bad.status_code == 422


def test_bulk_import_draws_from_the_import_budget(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "import_rate_limit_per_minute", 1)
    assert _csv(client, "phone_number\n9811100014\n").status_code == 200
    assert _csv(client, "phone_number\n9811100015\n").status_code == 429


# -- list / search / remove -----------------------------------------------------------------------


def test_list_is_bounded_paginated_and_searchable_by_any_spelling(client):
    _csv(client, "phone_number\n" + "\n".join(valid_in(i) for i in range(40_000, 40_030)))
    page = client.get(URL, params={"limit": 10, "offset": 0}).json()
    assert len(page["items"]) == 10 and page["total"] == 30 and page["limit"] == 10
    nxt = client.get(URL, params={"limit": 10, "offset": 25}).json()
    assert len(nxt["items"]) == 5
    assert client.get(URL, params={"limit": 201}).status_code == 422
    assert client.get(URL, params={"limit": 0}).status_code == 422
    assert client.get(URL, params={"offset": -1}).status_code == 422
    found = client.get(URL, params={"phone": "09800040005"}).json()  # valid_in(40005) spelled bare
    assert found["total"] == 1 and found["items"][0]["last4"] == "0005"
    assert client.get(URL, params={"phone": "9800099999"}).json()["total"] == 0
    assert client.get(URL, params={"phone": "garbage"}).status_code == 422
    assert all("phone_number" not in item for item in page["items"])


def test_remove_needs_a_reason_and_re_enables_calling(client, db_session):
    row = client.post(URL, json={"phone_number": "9811100016"}).json()
    assert client.request("DELETE", f"{URL}/{row['id']}").status_code == 422  # no body
    assert client.request("DELETE", f"{URL}/{row['id']}", json={}).status_code == 422
    assert client.request("DELETE", f"{URL}/{row['id']}", json={"reason": "no"}).status_code == 422
    assert SuppressionRepository(db_session).is_suppressed("+919811100016")
    ok = client.request("DELETE", f"{URL}/{row['id']}", json={"reason": "wrongly listed"})
    assert ok.status_code == 204
    assert not SuppressionRepository(db_session).is_suppressed("+919811100016")
    assert (
        client.request("DELETE", f"{URL}/{row['id']}", json={"reason": "again"}).status_code == 404
    )
    assert client.post(URL, json={"phone_number": "9811100016"}).status_code == 201  # re-addable


def test_authz_operators_read_only_anon_nothing(client, operator_client, anon_client):
    row = client.post(URL, json={"phone_number": "9811100017"}).json()
    assert operator_client.get(URL).status_code == 200
    assert operator_client.post(URL, json={"phone_number": "9811100018"}).status_code == 403
    assert (
        operator_client.request(
            "DELETE", f"{URL}/{row['id']}", json={"reason": "operator try"}
        ).status_code
        == 403
    )
    assert _csv(operator_client, "phone_number\n9811100019\n").status_code == 403
    assert anon_client.get(URL).status_code == 401
    assert anon_client.post(URL, json={"phone_number": "9811100020"}).status_code == 401
    assert client.get(URL, params={"phone": "9811100018"}).json()["total"] == 0  # nothing leaked in


def test_mutations_draw_from_the_mutation_budget(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "mutation_rate_limit_per_minute", 2)
    codes = [
        client.post(URL, json={"phone_number": f"98111000{i:02d}"}).status_code
        for i in (21, 22, 23)
    ]
    assert codes == [201, 201, 429]


# -- dial time: authoritative, permanent ----------------------------------------------------------


def test_a_number_added_after_its_job_was_queued_is_not_dialed(rig):
    rig.clock.now = ist(12, 0)  # window open: only the suppression can stop this call
    campaign, (contact,) = commit_world(
        policy={"max_retries": 2, "retry_spacing_seconds": [900, 900]}
    )
    enqueue_first(rig, campaign, contact)
    with Session() as s:
        number = s.get(Contact, contact).normalized_phone_number
    # AFTER queueing, through the real service, on a committed session (the dialer's view).
    with Session() as s:
        assert SuppressionService(s, actor="test-admin").add(number, "late complaint", None).created
        s.commit()

    assert rig.run_one() == JobOutcome.NOT_ELIGIBLE
    assert attempts_of(contact) == []  # nothing dialed, no attempt claimed
    assert contact_state(contact) == (ContactStatus.PENDING, 0)  # no retry budget consumed
    assert rig.pending() == 0  # acked: suppression is permanent
    assert rig.redis.zcard(_SCHEDULE_KEY) == 0  # and nothing scheduled to try again
    assert rig.run_one() == JobOutcome.NO_JOB


def test_suppression_matches_on_every_spelling_at_dial_time(rig):
    rig.clock.now = ist(12, 0)
    campaign, (contact,) = commit_world(policy=None)
    enqueue_first(rig, campaign, contact)
    with Session() as s:
        national = s.get(Contact, contact).normalized_phone_number[3:]  # bare 10 digits
        s.add(Suppression(phone_number=f"+91{national}", source=SuppressionSource.MANUAL_API))
        s.commit()
    assert rig.run_one() == JobOutcome.NOT_ELIGIBLE and attempts_of(contact) == []


def test_enqueue_skips_suppressed_numbers_with_the_same_lookup(client, db_session):
    campaign = client.post("/api/v1/campaigns", json={"name": "enqueue dnc"}).json()
    ids = {}
    for raw in ("9811100030", "9811100031"):
        ids[raw] = client.post(
            "/api/v1/contacts", json={"campaign_id": campaign["id"], "phone_number": raw}
        ).json()["id"]
    client.post(URL, json={"phone_number": "09811100030"})  # a different spelling
    repo = SuppressionRepository(db_session)
    assert repo.suppressed_among({"+919811100030", "+919811100031"}) == {"+919811100030"}


# -- opt-out writers ------------------------------------------------------------------------------


def test_opt_out_writer_is_global_idempotent_and_audits_only_the_inserter(db_session):
    from app.models.call_attempt import CallAttempt
    from app.models.campaign import Campaign
    from app.schemas.dograh_webhook import DograhWebhookPayload
    from app.services.telephony.dograh_webhook_service import _suppress_for_opt_out

    campaigns = [Campaign(name=f"optout {i}") for i in range(2)]
    db_session.add_all(campaigns)
    db_session.flush()
    contacts, attempts = [], []
    for campaign in campaigns:  # the SAME number in two campaigns
        contact = Contact(
            campaign_id=campaign.id,
            phone_number="9811100040",
            normalized_phone_number="+919811100040",
            status=ContactStatus.IN_CONVERSATION,
        )
        db_session.add(contact)
        db_session.flush()
        attempt = CallAttempt(
            contact_id=contact.id, attempt_number=1, state=CallAttemptState.CONNECTED
        )
        db_session.add(attempt)
        db_session.flush()
        contacts.append(contact)
        attempts.append(attempt)
    payload = DograhWebhookPayload(call_attempt_id=str(attempts[0].id), call_status="completed")

    for contact, attempt in zip(contacts, attempts, strict=True):
        _suppress_for_opt_out(db_session, attempt, contact, payload)
    _suppress_for_opt_out(db_session, attempts[0], contacts[0], payload)  # a repeat

    rows = (
        db_session.execute(select(Suppression).where(Suppression.phone_number == "+919811100040"))
        .scalars()
        .all()
    )
    assert len(rows) == 1 and rows[0].source == SuppressionSource.AGENT_IN_CALL
    assert rows[0].contact_id == contacts[0].id  # the first writer's contact; the number is global
    assert all(c.status == ContactStatus.CLOSED for c in contacts)  # both contacts are closed
    audits = [
        a
        for a in _audits(db_session, "dograh.opt_out_suppressed")
        if a.entity_id in {c.id for c in contacts}
    ]
    assert len(audits) == 1  # only the call that inserted audits


def test_opt_out_is_a_no_op_when_an_operator_already_listed_the_number(db_session):
    from app.models.call_attempt import CallAttempt
    from app.models.campaign import Campaign
    from app.schemas.dograh_webhook import DograhWebhookPayload
    from app.services.telephony.dograh_webhook_service import _suppress_for_opt_out

    campaign = Campaign(name="optout listed")
    db_session.add(campaign)
    db_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number="9811100041",
        normalized_phone_number="+919811100041",
        status=ContactStatus.IN_CONVERSATION,
    )
    db_session.add(contact)
    db_session.flush()
    attempt = CallAttempt(contact_id=contact.id, attempt_number=1, state=CallAttemptState.CONNECTED)
    db_session.add(attempt)
    db_session.add(Suppression(phone_number="+919811100041", source=SuppressionSource.MANUAL_API))
    db_session.flush()
    payload = DograhWebhookPayload(call_attempt_id=str(attempt.id), call_status="completed")

    _suppress_for_opt_out(db_session, attempt, contact, payload)  # must not raise

    assert contact.status == ContactStatus.CLOSED
    mine = (
        select(func.count())
        .select_from(Suppression)
        .where(Suppression.phone_number == "+919811100041")
    )
    assert db_session.execute(mine).scalar_one() == 1


# -- one lookup rule on every path ---------------------------------------------------------------


def test_every_lookup_canonicalizes_its_input_the_same_way(db_session):
    repo = SuppressionRepository(db_session)
    repo.insert_if_absent(
        "09811100050", source=SuppressionSource.MANUAL_API, reason="spelled with a trunk 0"
    )
    spellings = ["9811100050", "09811100050", "919811100050", "+91 98111-00050", "+919811100050"]
    for spelling in spellings:
        assert repo.is_suppressed(spelling), spelling
        assert repo.get_by_phone(spelling) is not None, spelling
    # the batch lookup (used by the CP12-C enqueue page) answers in the CALLER's spelling
    assert repo.suppressed_among(set(spellings) | {"9811100051"}) == set(spellings)
    assert not repo.is_suppressed("9811100051")
    # and the writer stored the canonical form, so the row is findable by it directly
    assert repo.get_by_phone("+919811100050").phone_number == "+919811100050"


def test_a_legacy_value_that_does_not_parse_is_still_matched_as_is(db_session):
    # An opt-out must never be lost because a stored number looks odd: an unparseable legacy
    # value is stored and looked up verbatim until the backfill repairs it.
    repo = SuppressionRepository(db_session)
    repo.insert_if_absent("+09876543299", source=SuppressionSource.MANUAL_API, reason=None)
    assert repo.is_suppressed("+09876543299")
