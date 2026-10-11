"""CP15A -- erasure, export, legal hold, reapply_erasures and the admin API."""

import json
import re

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models.audit_log import AuditLog
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.conversation import ConversationMessage, ConversationSession
from app.models.enums import CampaignStatus, ContactStatus, ConversationRole, SuppressionSource
from app.models.erasure_log import ErasureLog
from app.models.suppression import Suppression
from app.repositories.suppression_repository import SuppressionRepository
from app.services.data_lifecycle import export as export_mod
from app.services.data_lifecycle.erasure import erase, reapply_erasures
from app.services.data_lifecycle.fingerprint import erasure_fingerprint
from app.services.eligibility_service import DialEligibilityService
from tests.cp15_helpers import (
    NOW,
    add_analysis,
    add_snapshot,
    add_transcript,
    make_attempt,
    make_campaign,
    make_contact,
    next_number,
)


def _person(db, number, *, campaigns=2, status=ContactStatus.PENDING):
    """The same person (number) in several campaigns, each with a transcript + analysis."""
    contacts = []
    for _ in range(campaigns):
        campaign = make_campaign(db, status=CampaignStatus.ACTIVE)
        contact = make_contact(db, campaign, number, status=status)
        attempt = make_attempt(db, contact)
        add_transcript(db, attempt, age_days=1)
        add_snapshot(db, attempt, age_days=1)
        add_analysis(db, attempt, contact, age_days=1)
        contacts.append(contact)
    return contacts


def _erase(db, **kw):
    kw.setdefault("reason", "subject request")
    return erase(db, actor="test-admin", now=NOW, **kw)


def _remaining_content(db) -> int:
    return db.execute(select(func.count()).select_from(ConversationMessage)).scalar_one()


# -- erase ------------------------------------------------------------------------------------


def test_erasure_covers_every_contact_sharing_the_number_across_campaigns(db_session):
    number = next_number()
    target = _person(db_session, number, campaigns=3)
    bystander = _person(db_session, next_number(), campaigns=1)[0]
    result = _erase(db_session, phone_number=number)

    assert result.counts["contacts"] == 3
    assert result.counts["messages"] == 9
    assert result.counts["snapshots"] == 3 and result.counts["analyses"] == 3
    for contact in target:
        db_session.refresh(contact)
        assert contact.purged_at is not None
        assert contact.phone_number.startswith("purged:")
        assert contact.status == ContactStatus.CLOSED  # live contacts can never be dialed
    assert _remaining_content(db_session) == 3  # only the bystander's turns remain
    db_session.refresh(bystander)
    assert bystander.purged_at is None
    summaries = db_session.execute(select(CallAnalysis.summary)).scalars().all()
    assert summaries.count(None) == 3 and summaries.count("synthetic summary") == 1


def test_erasure_by_contact_id_resolves_the_number_and_covers_siblings(db_session):
    number = next_number()
    contacts = _person(db_session, number, campaigns=2)
    result = _erase(db_session, contact_id=contacts[0].id)
    assert result.counts["contacts"] == 2
    assert _remaining_content(db_session) == 0


def test_erasure_adds_to_the_do_not_call_list_by_default(db_session):
    number = next_number()
    _person(db_session, number, campaigns=1)
    result = _erase(db_session, phone_number=number)
    assert result.suppressed is True
    assert SuppressionRepository(db_session).is_suppressed(number)
    row = db_session.execute(select(Suppression)).scalar_one()
    assert row.reason == "erasure_request" and row.source == SuppressionSource.MANUAL_API


def test_erasure_can_opt_out_of_suppression_and_keeps_an_existing_row(db_session):
    number = next_number()
    _person(db_session, number, campaigns=1)
    assert _erase(db_session, phone_number=number, suppress=False).suppressed is False
    assert not SuppressionRepository(db_session).is_suppressed(number)
    SuppressionRepository(db_session).insert_if_absent(
        number, source=SuppressionSource.MANUAL_API, reason="earlier", created_by="x"
    )
    again = _erase(db_session, phone_number=number)
    assert again.suppressed is False  # already there: kept, not duplicated
    assert db_session.execute(select(func.count()).select_from(Suppression)).scalar_one() == 1


def test_erasure_log_and_audit_hold_a_fingerprint_never_the_number(db_session):
    number = next_number()
    _person(db_session, number, campaigns=1)
    _erase(db_session, phone_number=number)
    log = db_session.execute(select(ErasureLog)).scalar_one()
    assert re.fullmatch(r"[0-9a-f]{64}", log.fingerprint)
    assert log.fingerprint == erasure_fingerprint(number)
    audit = db_session.execute(
        select(AuditLog).where(AuditLog.action == "data.erasure")
    ).scalar_one()
    blob = json.dumps(
        [log.fingerprint, log.reason, log.counts, log.actor, audit.event_metadata, audit.actor]
    )
    assert number not in blob and number[3:] not in blob
    assert audit.event_metadata["last4"] == number[-4:]
    assert len(audit.event_metadata["fingerprint"]) == 16


def test_the_fingerprint_depends_on_the_pepper(monkeypatch):
    number = next_number()
    first = erasure_fingerprint(number)
    monkeypatch.setattr(get_settings(), "erasure_fingerprint_pepper", "another-pepper-value-123456")
    assert erasure_fingerprint(number) != first


@pytest.mark.parametrize("on", ["campaign", "contact"])
def test_a_legal_hold_refuses_the_whole_request_and_changes_nothing(db_session, on):
    number = next_number()
    contacts = _person(db_session, number, campaigns=2)
    if on == "campaign":
        db_session.get(Campaign, contacts[1].campaign_id).legal_hold = True
    else:
        contacts[1].legal_hold = True
    db_session.flush()
    with pytest.raises(ConflictError, match="legal hold"):
        _erase(db_session, phone_number=number)
    assert _remaining_content(db_session) == 6
    assert db_session.execute(select(func.count()).select_from(Suppression)).scalar_one() == 0
    assert db_session.execute(select(func.count()).select_from(ErasureLog)).scalar_one() == 0
    for contact in contacts:
        db_session.refresh(contact)
        assert contact.purged_at is None


def test_erasure_is_idempotent(db_session):
    number = next_number()
    contacts = _person(db_session, number, campaigns=2)
    first = _erase(db_session, phone_number=number)
    second = _erase(db_session, phone_number=number)
    assert first.counts["contacts"] == 2
    assert second.counts["contacts"] == 0 and second.counts["messages"] == 0
    assert second.suppressed is False
    assert _erase(db_session, contact_id=contacts[0].id).already_erased is True


def test_a_scrubbed_contact_is_not_dial_eligible(db_session):
    number = next_number()
    campaign = make_campaign(db_session, status=CampaignStatus.ACTIVE)
    contact = make_contact(db_session, campaign, number, status=ContactStatus.PENDING)
    _erase(db_session, phone_number=number)
    db_session.refresh(contact)
    verdict = DialEligibilityService(SuppressionRepository(db_session)).check(
        contact, campaign, None
    )
    assert verdict.eligible is False


def test_terminal_contact_status_is_kept_for_counts(db_session):
    number = next_number()
    contact = _person(db_session, number, campaigns=1, status=ContactStatus.COMPLETED)[0]
    _erase(db_session, phone_number=number)
    db_session.refresh(contact)
    assert contact.status == ContactStatus.COMPLETED and contact.purged_at is not None


def test_erasure_input_validation(db_session):
    with pytest.raises(ValidationError):
        _erase(db_session)
    with pytest.raises(ValidationError):
        _erase(db_session, phone_number=next_number(), contact_id=make_contact(
            db_session, make_campaign(db_session)).id)
    with pytest.raises(ValidationError, match="Invalid phone"):
        _erase(db_session, phone_number="not-a-number")
    import uuid

    with pytest.raises(NotFoundError):
        _erase(db_session, contact_id=uuid.uuid4())


def test_a_number_with_no_contacts_is_still_suppressed_and_logged(db_session):
    number = next_number()
    result = _erase(db_session, phone_number=number)
    assert result.counts["contacts"] == 0 and result.suppressed is True
    assert db_session.execute(select(func.count()).select_from(ErasureLog)).scalar_one() == 1


def test_erasure_log_is_append_only(db_session):
    _erase(db_session, phone_number=next_number())
    with pytest.raises(DBAPIError, match="append-only"):
        with db_session.begin_nested():
            db_session.execute(text("UPDATE erasure_log SET actor = 'x'"))
    with pytest.raises(DBAPIError, match="append-only"):
        with db_session.begin_nested():
            db_session.execute(text("DELETE FROM erasure_log"))
    assert db_session.execute(select(func.count()).select_from(ErasureLog)).scalar_one() == 1


# -- reapply after a restore ------------------------------------------------------------------


def _simulate_restore_of(db, contacts, number):
    """Put the erased person back exactly as an older backup would hold them: the number,
    a live status, and transcript text."""
    for contact in contacts:
        db.execute(
            update(Contact)
            .where(Contact.id == contact.id)
            .values(
                phone_number=number, normalized_phone_number=number,
                purged_at=None, status=ContactStatus.PENDING,
            )
        )
        attempt_session = db.execute(
            select(ConversationSession.id)
            .join(CallAttempt, CallAttempt.id == ConversationSession.call_attempt_id)
            .where(CallAttempt.contact_id == contact.id)
        ).scalar_one()
        db.add(
            ConversationMessage(
                session_id=attempt_session, sequence=99,
                role=ConversationRole.CONTACT, content="restored synthetic turn",
            )
        )
        db.expire(contact)
    db.flush()


def test_reapply_erasures_scrubs_people_who_came_back_from_an_older_backup(db_session):
    number = next_number()
    contacts = _person(db_session, number, campaigns=2)
    other = _person(db_session, next_number(), campaigns=1)[0]
    _erase(db_session, phone_number=number)
    _simulate_restore_of(db_session, contacts, number)
    assert db_session.get(Contact, contacts[0].id).purged_at is None

    dry = reapply_erasures(db_session, now=NOW)
    assert (dry.fingerprints, dry.contacts_matched, dry.applied) == (1, 2, False)
    assert db_session.get(Contact, contacts[0].id).purged_at is None  # dry run: no change

    applied = reapply_erasures(db_session, now=NOW, apply=True)
    assert applied.contacts_scrubbed == 2 and applied.skipped_legal_hold == 0
    for contact in contacts:
        db_session.refresh(contact)
        assert contact.purged_at is not None and contact.phone_number.startswith("purged:")
        assert contact.status == ContactStatus.CLOSED
    db_session.refresh(other)
    assert other.purged_at is None
    # idempotent
    assert reapply_erasures(db_session, now=NOW, apply=True).contacts_matched == 0


def test_reapply_skips_held_records_and_reports_a_dnc_gap(db_session):
    number = next_number()
    contacts = _person(db_session, number, campaigns=2)
    _erase(db_session, phone_number=number)
    _simulate_restore_of(db_session, contacts, number)
    contacts[0].legal_hold = True
    db_session.execute(text("DELETE FROM suppression"))  # the restore lost the DNC entry
    db_session.flush()
    report = reapply_erasures(db_session, now=NOW, apply=True)
    assert report.contacts_matched == 2 and report.skipped_legal_hold == 1
    assert report.contacts_scrubbed == 1
    assert report.erased_missing_from_dnc == 1
    db_session.refresh(contacts[0])
    assert contacts[0].purged_at is None


def test_reapply_with_an_empty_erasure_log_changes_nothing(db_session):
    contact = make_contact(db_session, make_campaign(db_session))
    report = reapply_erasures(db_session, now=NOW, apply=True)
    assert report.fingerprints == 0 and report.contacts_scanned == 0
    db_session.refresh(contact)
    assert contact.purged_at is None


# -- export -----------------------------------------------------------------------------------


def test_export_is_complete_bounded_and_audited_with_counts_only(db_session, monkeypatch):
    number = next_number()
    contact = _person(db_session, number, campaigns=1)[0]
    data = export_mod.export_contact(db_session, contact_id=contact.id, actor="test-admin")
    assert data["contact"]["phone_number"] == number
    assert len(data["attempts"][0]["transcript"]) == 3
    assert data["attempts"][0]["analysis"]["summary"] == "synthetic summary"
    assert data["truncated"] is False
    audit = db_session.execute(
        select(AuditLog).where(AuditLog.action == "data.export")
    ).scalar_one()
    assert audit.event_metadata == {"attempts": 1, "messages": 3, "truncated": False}
    assert number not in json.dumps(audit.event_metadata)

    monkeypatch.setattr(export_mod, "MAX_EXPORT_MESSAGES", 2)
    assert export_mod.export_contact(
        db_session, contact_id=contact.id, actor="test-admin"
    )["truncated"] is True


def test_export_of_a_purged_contact_has_no_number(db_session):
    number = next_number()
    contact = _person(db_session, number, campaigns=1)[0]
    _erase(db_session, phone_number=number)
    data = export_mod.export_contact(db_session, contact_id=contact.id, actor="a")
    assert data["contact"]["phone_number"] is None and data["contact"]["purged_at"]
    assert data["attempts"][0]["transcript"] == []


# -- the admin API ----------------------------------------------------------------------------

ERASE = "/api/v1/admin/data/erasure"


def test_erasure_api_authz_matrix_and_response_shape(
    client, operator_client, anon_client, db_session
):
    number = next_number()
    _person(db_session, number, campaigns=1)
    body = {"phone_number": number, "reason": "subject request"}
    assert anon_client.post(ERASE, json=body).status_code == 401
    assert operator_client.post(ERASE, json=body).status_code == 403
    ok = client.post(ERASE, json=body)
    assert ok.status_code == 200
    assert number not in ok.text
    assert ok.json()["last4"] == number[-4:] and ok.json()["counts"]["contacts"] == 1
    audit = db_session.execute(
        select(AuditLog).where(AuditLog.action == "data.erasure")
    ).scalar_one()
    assert audit.actor == "test-admin"


def test_erasure_api_validation_and_hold_conflict(client, db_session):
    number = next_number()
    contact = _person(db_session, number, campaigns=1)[0]
    contact.legal_hold = True
    db_session.flush()
    assert client.post(ERASE, json={"reason": "abc"}).status_code == 422  # no target
    assert client.post(
        ERASE, json={"phone_number": number, "contact_id": str(contact.id), "reason": "abc"}
    ).status_code == 422
    assert client.post(ERASE, json={"phone_number": number, "reason": "x"}).status_code == 422
    conflict = client.post(ERASE, json={"phone_number": number, "reason": "subject request"})
    assert conflict.status_code == 409 and number not in conflict.text


def test_export_api_authz(client, operator_client, anon_client, db_session):
    contact = _person(db_session, next_number(), campaigns=1)[0]
    url = f"/api/v1/admin/data/contacts/{contact.id}/export"
    assert anon_client.get(url).status_code == 401
    assert operator_client.get(url).status_code == 403
    ok = client.get(url)
    assert ok.status_code == 200 and ok.json()["attempts"][0]["transcript"]
    import uuid

    assert client.get(f"/api/v1/admin/data/contacts/{uuid.uuid4()}/export").status_code == 404


@pytest.mark.parametrize("kind", ["campaigns", "contacts"])
def test_legal_hold_api_is_admin_only_needs_a_reason_and_is_audited(
    client, operator_client, anon_client, db_session, kind
):
    contact = _person(db_session, next_number(), campaigns=1)[0]
    target = contact.campaign_id if kind == "campaigns" else contact.id
    url = f"/api/v1/admin/data/legal-hold/{kind}/{target}"
    body = {"active": True, "reason": "litigation 2026-11"}
    assert anon_client.put(url, json=body).status_code == 401
    assert operator_client.put(url, json=body).status_code == 403
    assert client.put(url, json={"active": True}).status_code == 422
    assert client.put(url, json={"active": True, "reason": "x"}).status_code == 422
    first = client.put(url, json=body)
    assert first.status_code == 200 and first.json() == {"active": True, "changed": True}
    assert client.put(url, json=body).json()["changed"] is False
    cleared = client.put(url, json={"active": False, "reason": "matter closed"})
    assert cleared.json() == {"active": False, "changed": True}
    actions = db_session.execute(
        select(AuditLog.action, AuditLog.event_metadata).where(
            AuditLog.action.in_(["legal_hold.set", "legal_hold.cleared"])
        )
    ).all()
    assert {a for a, _ in actions} == {"legal_hold.set", "legal_hold.cleared"}
    assert any(m["reason"] == "litigation 2026-11" for _, m in actions)
    import uuid

    missing = f"/api/v1/admin/data/legal-hold/{kind}/{uuid.uuid4()}"
    assert client.put(missing, json=body).status_code == 404


def test_retention_status_is_readable_by_any_authenticated_role(
    client, operator_client, anon_client, db_session, monkeypatch
):
    url = "/api/v1/admin/data/retention/status"
    assert anon_client.get(url).status_code == 401
    empty = operator_client.get(url).json()
    assert empty["last_run_id"] is None
    assert set(empty["backlog"].values()) == {None}  # nothing configured => keep forever

    monkeypatch.setattr(get_settings(), "retention_transcript_days", 1)
    add_transcript(
        db_session,
        make_attempt(db_session, make_contact(db_session, make_campaign(db_session))),
        age_days=60,
    )
    body = client.get(url).json()
    assert body["backlog"]["transcript_messages"] == 3
    assert body["backlog"]["contacts"] is None
