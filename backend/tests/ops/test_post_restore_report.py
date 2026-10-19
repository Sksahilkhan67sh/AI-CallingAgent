"""post_restore_report is READ ONLY: enforced by the database transaction mode, not by trust."""

import json
import re
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.models.call_analysis import CallAnalysis
from app.models.call_attempt import CallAttempt
from app.models.contact import Contact
from app.models.enums import AnalysisStatus, CallAttemptState, ContactStatus
from app.scripts import post_restore_report as report_mod
from tests.cp15_helpers import (
    NOW,
    make_attempt,
    make_campaign,
    make_contact,
    next_number,
)

# The report JSON legitimately contains random UUIDs, hex fingerprints and real-time ISO
# timestamps (microseconds). A four-digit phone suffix can appear inside those by chance
# (~1.3% of runs: CI failed on '0020'), which says nothing about a leak. Remove those tokens
# before looking for the suffix; a masked number such as "***0020" is not one of them.
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ISO_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T[0-9:.]+(?:[+-]\d{2}:\d{2})?")
_LONG_HEX = re.compile(r"[0-9a-f]{8,}")


def _leaks_suffix(output: str, suffix: str) -> bool:
    for random_token in (_UUID, _ISO_TIMESTAMP, _LONG_HEX):
        output = random_token.sub("<random>", output)
    return suffix in output


def test_suffix_check_ignores_random_tokens_but_still_catches_a_leak():
    uuid_with_suffix = "5b1c0020-0020-4f4e-8a00-000200200020"
    assert not _leaks_suffix(f'{{"id": "{uuid_with_suffix}"}}', "0020")
    assert not _leaks_suffix('{"at": "2026-10-19T02:47:24.002012+00:00"}', "0020")
    assert not _leaks_suffix('{"fingerprint": "ab00200cdef0123456"}', "0020")
    # real leaks: masked, spaced and bare forms
    assert _leaks_suffix('{"phone": "******0020"}', "0020")
    assert _leaks_suffix('{"phone": "+91 98784 90020"}', "0020")
    assert _leaks_suffix("phone ends 0020", "0020")


def _age(db, contact, hours):
    db.execute(
        text("update contact set updated_at = :t where id = :i"),
        {"t": NOW - timedelta(hours=hours), "i": contact.id},
    )


def _world(db):
    campaign = make_campaign(db)
    stranded = {}
    for key, status, hours in (
        ("d_new", ContactStatus.DIALING, 0.2),
        ("d_day", ContactStatus.DIALING, 5),
        ("r_week", ContactStatus.RETRY_SCHEDULED, 72),
        ("r_old", ContactStatus.RETRY_SCHEDULED, 24 * 30),
    ):
        c = make_contact(db, campaign, next_number(), status=status)
        _age(db, c, hours)
        stranded[key] = c
    done = make_contact(db, campaign, next_number(), status=ContactStatus.COMPLETED)
    attempt = make_attempt(db, stranded["d_day"])
    attempt.state = CallAttemptState.INITIATED
    attempt.started_at = NOW - timedelta(hours=30)
    db.add(
        CallAnalysis(
            id=uuid.uuid4(),
            call_attempt_id=attempt.id,
            contact_id=stranded["d_day"].id,
            campaign_id=campaign.id,
            status=AnalysisStatus.PROCESSING,
            claim_token=uuid.uuid4(),
            lease_expires_at=NOW - timedelta(hours=1),
            processing_started_at=NOW - timedelta(hours=2),
        )
    )
    db.flush()
    return stranded, done, attempt


def test_buckets_counts_and_samples(db_session):
    stranded, done, attempt = _world(db_session)
    rep = report_mod.build_report(db_session, NOW, limit=1)
    contacts = rep["stranded_contacts"]
    assert contacts["total"] == 4
    assert {k: v["count"] for k, v in contacts["by_age"].items()} == {
        "under_1h": 1,
        "1h_to_24h": 1,
        "1d_to_7d": 1,
        "over_7d": 1,
    }
    assert str(stranded["d_new"].id) in {s["id"] for s in contacts["by_age"]["under_1h"]["sample"]}
    assert all(str(done.id) != s["id"] for b in contacts["by_age"].values() for s in b["sample"])
    assert rep["stranded_call_attempts"]["total"] == 1
    assert rep["stranded_call_attempts"]["by_age"]["1d_to_7d"]["sample"][0] == {
        "id": str(attempt.id),
        "status": "Initiated",
    }
    assert rep["stranded_analyses"]["total"] == 1
    assert rep["newest"]["contact.updated_at"] and rep["newest"]["erasure_log.requested_at"] is None


def test_the_sample_is_bounded_by_the_limit(db_session):
    campaign = make_campaign(db_session)
    for _ in range(5):
        make_contact(db_session, campaign, next_number(), status=ContactStatus.DIALING)
    rep = report_mod.build_report(db_session, NOW, limit=2)
    bucket = rep["stranded_contacts"]["by_age"]
    assert sum(b["count"] for b in bucket.values()) == 5
    assert max(len(b["sample"]) for b in bucket.values()) <= 2


def test_it_changes_nothing_and_the_transaction_is_database_enforced_read_only(db_session):
    _world(db_session)

    def snapshot():
        return (
            db_session.execute(select(func.count()).select_from(Contact)).scalar_one(),
            db_session.execute(select(func.max(Contact.updated_at))).scalar_one(),
            db_session.execute(select(func.count()).select_from(CallAttempt)).scalar_one(),
            db_session.execute(select(func.count()).select_from(AuditLog)).scalar_one(),
            db_session.execute(
                text("select md5(string_agg(t::text, '|' order by t::text)) from contact t")
            ).scalar_one(),
        )

    before = snapshot()
    report_mod.build_report(db_session, NOW)
    after = snapshot()
    assert before == after
    with pytest.raises(DBAPIError, match="read-only transaction"):
        with db_session.begin_nested():
            db_session.execute(
                text(
                    "insert into campaign (id, name, status) "
                    "values (gen_random_uuid(), 'x', 'active')"
                )
            )
    with pytest.raises(DBAPIError, match="read-only transaction"):
        with db_session.begin_nested():
            db_session.execute(text("update contact set attempt_count = 9"))


def test_it_never_flushes_the_session(db_session, monkeypatch):
    _world(db_session)
    db_session.flush()

    def boom(self, *a, **k):
        raise AssertionError("the report must not flush")

    monkeypatch.setattr(Session, "flush", boom)
    assert report_mod.build_report(db_session, NOW)["stranded_contacts"]["total"] == 4


def test_no_phone_number_in_text_or_json_output_and_secret_checks_are_stable(
    db_session, monkeypatch, capsys
):
    stranded, _done, _attempt = _world(db_session)
    rep = report_mod.build_report(db_session, NOW)
    text_out, json_out = report_mod._render(rep), json.dumps(rep)
    for c in stranded.values():
        assert c.phone_number not in text_out + json_out
        assert not _leaks_suffix(json_out, c.phone_number[-4:])
    again = report_mod.build_report(db_session, NOW)["secret_checks"]
    assert again == rep["secret_checks"] and set(again) == {"erasure_pepper", "jwt_signing_key"}
    monkeypatch.setattr(
        get_settings(), "erasure_fingerprint_pepper", "a-different-pepper-value-0123456789"
    )
    assert (
        report_mod.build_report(db_session, NOW)["secret_checks"]["erasure_pepper"]
        != again["erasure_pepper"]
    )
    monkeypatch.setattr(
        get_settings(), "jwt_signing_key", "a-different-signing-key-value-0123456789"
    )
    assert (
        report_mod.build_report(db_session, NOW)["secret_checks"]["jwt_signing_key"]
        != again["jwt_signing_key"]
    )
    assert "Nothing was changed" in text_out
