"""CP15A -- the retention engine against real Postgres, with an injected clock.

Most tests run inside the rolled-back `db_session` transaction. The failure/repair test and the
lock test need real commits and use the committed `Session` from cp14_helpers."""

import ast
import itertools
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.models.audit_log import AuditLog
from app.models.call_analysis import CallAnalysis
from app.models.contact import Contact
from app.models.conversation import ConversationMessage
from app.models.enums import AnalysisStatus, CampaignStatus, SuppressionSource
from app.models.processed_event import ProcessedEvent
from app.models.retention_run import RetentionRun
from app.models.suppression import Suppression
from app.services.data_lifecycle import retention
from app.services.data_lifecycle.retention import (
    RetentionConfig,
    RetentionLockHeld,
    advisory_lock,
    backlog_counts,
    run_retention,
)
from tests.conftest import test_engine
from tests.cp15_helpers import (
    NOW,
    add_analysis,
    add_snapshot,
    add_transcript,
    days_ago,
    make_attempt,
    make_campaign,
    make_contact,
)

CFG_T_C = RetentionConfig(transcript_days=30, contact_days=30)
ALL_30 = RetentionConfig(
    transcript_days=30,
    analysis_days=30,
    contact_days=30,
    audit_log_days=30,
    processed_event_days=30,
    run_history_days=30,
)


def _run(db, config=ALL_30, *, apply=True, **kw):
    return run_retention(db, lock_engine=test_engine, config=config, now=NOW, apply=apply, **kw)


def _count(db, model) -> int:
    return db.execute(select(func.count()).select_from(model)).scalar_one()


# -- each class at the boundary ---------------------------------------------------------------


def test_transcript_messages_at_the_boundary(db_session):
    attempt = make_attempt(db_session, make_contact(db_session, make_campaign(db_session)))
    add_transcript(db_session, attempt, age_days=29.99, turns=2)  # just inside
    old = make_attempt(db_session, make_contact(db_session, make_campaign(db_session)))
    add_transcript(db_session, old, age_days=30.01, turns=3)  # just past
    result = _run(db_session)
    assert result.counts["transcript_messages"] == 3
    assert _count(db_session, ConversationMessage) == 2
    assert result.status == "completed"


def test_working_memory_content_is_cleared_but_the_row_stays(db_session):
    attempt = make_attempt(db_session, make_contact(db_session, make_campaign(db_session)))
    old = add_snapshot(db_session, attempt, age_days=31)
    fresh = add_snapshot(db_session, attempt, age_days=29)
    _run(db_session)
    db_session.refresh(old)
    db_session.refresh(fresh)
    cleared = (old.captured_entities, old.objections_raised, old.last_agent_utterance)
    assert cleared == ({}, [], None)
    assert old.script_progress == {} or isinstance(old.script_progress, dict)  # row kept
    assert fresh.captured_entities == {"k": "synthetic"}


def test_analysis_free_text_is_nulled_scores_stay(db_session):
    contact = make_contact(db_session, make_campaign(db_session))
    attempt = make_attempt(db_session, contact)
    old = add_analysis(db_session, attempt, contact, age_days=31)
    contact2 = make_contact(db_session, make_campaign(db_session))
    fresh = add_analysis(db_session, make_attempt(db_session, contact2), contact2, age_days=29)
    result = _run(db_session)
    db_session.refresh(old)
    db_session.refresh(fresh)
    assert result.counts["analysis_free_text"] == 1
    assert (old.summary, old.feedback, old.key_facts, old.objections, old.customer_needs) == (
        None, None, [], [], [],
    )
    assert old.lead_score == 70
    assert fresh.summary == "synthetic summary"


def test_in_flight_analysis_is_never_scrubbed_and_its_transcript_is_kept(db_session):
    contact = make_contact(db_session, make_campaign(db_session))
    attempt = make_attempt(db_session, contact)
    add_transcript(db_session, attempt, age_days=90)
    pending = add_analysis(db_session, attempt, contact, age_days=90, status=AnalysisStatus.PENDING)
    _run(db_session)
    db_session.refresh(pending)
    assert pending.summary == "synthetic summary"
    assert _count(db_session, ConversationMessage) == 3


def test_contacts_are_scrubbed_after_the_campaign_closed_not_after_they_were_created(db_session):
    old_campaign = make_campaign(db_session, closed_days_ago=31)
    new_campaign = make_campaign(db_session, closed_days_ago=29)
    a = make_contact(db_session, old_campaign)
    b = make_contact(db_session, old_campaign)
    c = make_contact(db_session, new_campaign)
    original = c.phone_number
    result = _run(db_session)
    for row in (a, b, c):
        db_session.refresh(row)
    assert result.counts["contacts"] == 2
    assert a.purged_at is not None and b.purged_at is not None and c.purged_at is None
    assert a.phone_number.startswith("purged:") and a.phone_number == a.normalized_phone_number
    assert a.phone_number != b.phone_number  # unique constraint still holds
    assert c.phone_number == original
    assert a.status.value == "Completed"  # status kept for counts
    assert a.updated_at == days_ago(1000)  # the scrub does not move age-based decisions


@pytest.mark.parametrize(
    "status,closed", [(CampaignStatus.ACTIVE, 400), (CampaignStatus.COMPLETED, None)]
)
def test_open_or_undated_campaigns_are_never_purged(db_session, status, closed):
    campaign = make_campaign(db_session, status=status, closed_days_ago=closed)
    contact = make_contact(db_session, campaign)
    _run(db_session)
    db_session.refresh(contact)
    assert contact.purged_at is None


def test_audit_log_processed_events_and_run_history_by_age(db_session):
    db_session.add_all(
        [
            AuditLog(actor="a", action="x", entity_type="t", created_at=days_ago(31)),
            AuditLog(actor="a", action="x", entity_type="t", created_at=days_ago(29)),
            ProcessedEvent(event_id="e-old", event_type="t", received_at=days_ago(31)),
            ProcessedEvent(event_id="e-new", event_type="t", received_at=days_ago(29)),
            RetentionRun(mode="apply", status="completed", counts={}, started_at=days_ago(31)),
            RetentionRun(mode="apply", status="completed", counts={}, started_at=days_ago(29)),
        ]
    )
    db_session.flush()
    before_audit = _count(db_session, AuditLog)
    result = _run(db_session)
    assert result.counts["processed_events"] == 1
    assert result.counts["retention_runs"] == 1
    assert result.counts["audit_log"] == 1
    # one 29-day-old audit row + this run's own audit event remain
    assert _count(db_session, AuditLog) == before_audit - 1 + 1
    assert db_session.execute(select(ProcessedEvent.event_id)).scalars().all() == ["e-new"]


def test_zero_or_unset_keeps_forever(db_session):
    contact = make_contact(db_session, make_campaign(db_session, closed_days_ago=999))
    add_transcript(db_session, make_attempt(db_session, contact), age_days=999)
    result = _run(db_session, RetentionConfig(transcript_days=0))
    assert result.counts == {}
    assert set(result.disabled) == set(retention.STEP_KEYS)
    assert _count(db_session, ConversationMessage) == 3


# -- legal hold -------------------------------------------------------------------------------


def _hold_world(db, *, campaign_hold=False, contact_hold=False):
    campaign = make_campaign(db, closed_days_ago=400, hold=campaign_hold)
    contact = make_contact(db, campaign, hold=contact_hold)
    attempt = make_attempt(db, contact)
    add_transcript(db, attempt, age_days=400)
    add_snapshot(db, attempt, age_days=400)
    add_analysis(db, attempt, contact, age_days=400)
    db.add(
        AuditLog(
            actor="a", action="held", entity_type="campaign",
            entity_id=campaign.id, created_at=days_ago(400),
        )
    )
    db.add(
        AuditLog(
            actor="a", action="held-c", entity_type="contact",
            entity_id=contact.id, created_at=days_ago(400),
        )
    )
    db.flush()
    return campaign, contact


@pytest.mark.parametrize("campaign_hold,contact_hold", [(True, False), (False, True)])
def test_legal_hold_blocks_every_retention_action(db_session, campaign_hold, contact_hold):
    campaign, contact = _hold_world(
        db_session, campaign_hold=campaign_hold, contact_hold=contact_hold
    )
    baseline_audit = db_session.execute(
        select(func.count()).select_from(AuditLog).where(AuditLog.action.in_(["held", "held-c"]))
    ).scalar_one()
    result = _run(db_session)
    assert result.counts["transcript_messages"] == 0
    assert result.counts["working_memory_snapshots"] == 0
    assert result.counts["analysis_free_text"] == 0
    assert result.counts["contacts"] == 0
    db_session.refresh(contact)
    assert contact.purged_at is None
    assert _count(db_session, ConversationMessage) == 3
    assert db_session.execute(select(CallAnalysis.summary)).scalar_one() == "synthetic summary"
    kept = db_session.execute(
        select(func.count()).select_from(AuditLog).where(AuditLog.action.in_(["held", "held-c"]))
    ).scalar_one()
    # audit rows about the held campaign/contact survive; only the matching entity is protected
    assert kept >= 1 and kept <= baseline_audit


def test_releasing_the_hold_lets_the_next_run_purge(db_session):
    campaign, contact = _hold_world(db_session, campaign_hold=True)
    _run(db_session)
    campaign.legal_hold = False
    db_session.flush()
    result = _run(db_session)
    assert result.counts["transcript_messages"] == 3
    assert result.counts["contacts"] == 1


# -- suppression is never touched -------------------------------------------------------------


def test_suppression_rows_are_untouched_by_every_class(db_session):
    contact = make_contact(db_session, make_campaign(db_session, closed_days_ago=999))
    db_session.add(
        Suppression(
            phone_number=contact.normalized_phone_number,
            contact_id=contact.id,
            source=SuppressionSource.MANUAL_API,
            reason="r",
            requested_at=days_ago(9999),
        )
    )
    db_session.flush()
    snapshot = db_session.execute(
        select(Suppression.id, Suppression.phone_number, Suppression.contact_id,
               Suppression.reason, Suppression.requested_at)
    ).all()
    _run(db_session, RetentionConfig(
        transcript_days=1, analysis_days=1, contact_days=1,
        audit_log_days=1, processed_event_days=1, run_history_days=1,
    ))
    assert db_session.execute(
        select(Suppression.id, Suppression.phone_number, Suppression.contact_id,
               Suppression.reason, Suppression.requested_at)
    ).all() == snapshot


LIFECYCLE_DIR = Path(retention.__file__).parent


def test_retention_module_never_references_suppression():
    """GUARD: the purge path must not import, name or query the do-not-call table."""
    tree = ast.parse((LIFECYCLE_DIR / "retention.py").read_text())
    mentioned = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            mentioned.add(node.id)
        elif isinstance(node, ast.Attribute):
            mentioned.add(node.attr)
        elif isinstance(node, ast.ImportFrom):
            mentioned.add(node.module or "")
            mentioned.update(alias.name for alias in node.names)
        elif (
            isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) < 60
        ):
            mentioned.add(node.value)  # table-name strings in raw SQL (docstrings are longer)
    assert not [m for m in mentioned if "suppression" in m.lower()]


def test_no_lifecycle_module_deletes_or_updates_suppression_rows():
    for path in LIFECYCLE_DIR.glob("*.py"):
        source = path.read_text()
        for forbidden in ("delete(Suppression", "update(Suppression", "DELETE FROM suppression",
                          "UPDATE suppression", "TRUNCATE"):
            assert forbidden not in source, (path.name, forbidden)


# -- dry run, idempotence, batching -----------------------------------------------------------


def test_dry_run_changes_no_data_and_reports_what_apply_would_do(db_session):
    contact = make_contact(db_session, make_campaign(db_session, closed_days_ago=60))
    attempt = make_attempt(db_session, contact)
    add_transcript(db_session, attempt, age_days=60)
    add_analysis(db_session, attempt, contact, age_days=60)
    before = (
        _count(db_session, ConversationMessage),
        db_session.execute(select(CallAnalysis.summary)).scalar_one(),
        db_session.execute(select(Contact.purged_at)).scalar_one(),
    )
    dry = _run(db_session, apply=False)
    after = (
        _count(db_session, ConversationMessage),
        db_session.execute(select(CallAnalysis.summary)).scalar_one(),
        db_session.execute(select(Contact.purged_at)).scalar_one(),
    )
    assert before == after
    assert dry.mode == "dry_run" and dry.counts["transcript_messages"] == 3
    applied = _run(db_session, apply=True)
    assert applied.counts["transcript_messages"] == dry.counts["transcript_messages"]
    assert applied.counts["contacts"] == dry.counts["contacts"] == 1


def test_apply_is_idempotent(db_session):
    contact = make_contact(db_session, make_campaign(db_session, closed_days_ago=60))
    add_transcript(db_session, make_attempt(db_session, contact), age_days=60)
    first = _run(db_session)
    second = _run(db_session)
    assert first.counts["transcript_messages"] == 3
    assert all(v == 0 for k, v in second.counts.items() if k != "audit_log")


def test_batch_limit_and_runtime_budget_then_resume(db_session):
    contact = make_contact(db_session, make_campaign(db_session))
    add_transcript(db_session, make_attempt(db_session, contact), age_days=60, turns=5)
    ticks = itertools.count()
    config = RetentionConfig(transcript_days=30, batch_size=2, max_runtime_seconds=2)
    partial = _run(db_session, config, monotonic=lambda: float(next(ticks)))
    assert partial.status == "partial"
    assert partial.counts["transcript_messages"] == 2  # one batch of two, then the budget hit
    assert _count(db_session, ConversationMessage) == 3
    done = _run(db_session, RetentionConfig(transcript_days=30, batch_size=2))
    assert done.status == "completed"
    assert done.counts["transcript_messages"] == 3
    assert _count(db_session, ConversationMessage) == 0


def test_backlog_counts_match_a_dry_run(db_session):
    contact = make_contact(db_session, make_campaign(db_session, closed_days_ago=60))
    add_transcript(db_session, make_attempt(db_session, contact), age_days=60)
    backlog = backlog_counts(db_session, ALL_30, NOW)
    dry = _run(db_session, apply=False)
    assert backlog["transcript_messages"] == dry.counts["transcript_messages"] == 3
    assert backlog_counts(db_session, RetentionConfig(), NOW)["contacts"] is None


# -- run record and audit contain counts only -------------------------------------------------


def test_run_row_and_audit_event_carry_counts_only(db_session):
    contact = make_contact(db_session, make_campaign(db_session, closed_days_ago=60))
    add_transcript(db_session, make_attempt(db_session, contact), age_days=60)
    number = contact.normalized_phone_number
    result = _run(db_session)
    run = db_session.get(RetentionRun, result.run_id)
    event = db_session.execute(
        select(AuditLog).where(AuditLog.action == "retention.run")
    ).scalar_one()
    assert run.status == "completed" and run.finished_at is not None
    assert set(event.event_metadata) == {"mode", "status", "counts", "disabled_classes"}
    blob = f"{run.counts} {event.event_metadata} {event.actor}"
    assert number not in blob and "synthetic" not in blob and str(contact.id) not in blob
    assert all(isinstance(v, int) for v in event.event_metadata["counts"].values())


# -- locking, failure and repair (committed data) ----------------------------------------------


def test_the_advisory_lock_prevents_overlapping_runs():
    from tests.cp14_helpers import Session

    with advisory_lock(test_engine) as first, advisory_lock(test_engine) as second:
        assert first is True and second is False
    with Session() as db, advisory_lock(test_engine) as held:
        assert held
        with Session() as other:
            with pytest.raises(RetentionLockHeld):
                run_retention(
                    other, lock_engine=test_engine, config=ALL_30, now=NOW, apply=True
                )
            row = other.execute(
                select(RetentionRun).order_by(RetentionRun.started_at.desc())
            ).scalars().first()
            assert (row.status, row.error_category) == ("failed", "lock_held")
        db.rollback()
    # the lock is released with the context: a later run works
    with Session() as db:
        assert run_retention(db, lock_engine=test_engine, config=ALL_30, now=NOW).status == (
            "completed"
        )


def test_a_failure_mid_run_is_sanitized_and_a_rerun_repairs(monkeypatch):
    from tests.cp14_helpers import Session

    with Session() as db:
        campaign = make_campaign(db, closed_days_ago=60)
        contact = make_contact(db, campaign)
        add_transcript(db, make_attempt(db, contact), age_days=60, turns=4)
        db.commit()
    real = retention._STEPS
    calls = {"n": 0}

    def boom(db, ids, now):
        calls["n"] += 1
        raise SQLAlchemyError("secret-detail +919800000000 must not leak")

    patched = tuple(
        retention._Step(s.key, s.days_attr, s.candidates, boom) if s.key == "contacts" else s
        for s in real
    )
    monkeypatch.setattr(retention, "_STEPS", patched)
    with Session() as db:
        failed = run_retention(
            db, lock_engine=test_engine, config=CFG_T_C, now=NOW, apply=True,
        )
        assert failed.status == "failed" and failed.error_category == "db_error"
        row = db.get(RetentionRun, failed.run_id)
        assert "secret-detail" not in str(row.counts) + str(row.error_category)
        # transcript step committed before the failure; the contact step did not
        assert db.execute(select(func.count()).select_from(ConversationMessage)).scalar_one() == 0
        assert db.execute(select(Contact.purged_at)).scalar_one() is None
    monkeypatch.setattr(retention, "_STEPS", real)
    with Session() as db:
        fixed = run_retention(
            db, lock_engine=test_engine, config=CFG_T_C, now=NOW, apply=True,
        )
        assert fixed.status == "completed" and fixed.counts["contacts"] == 1
        assert db.execute(select(Contact.purged_at)).scalar_one() is not None
    from app.models.base import Base

    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    with test_engine.begin() as conn:
        conn.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
    assert calls["n"] == 1


# -- campaign closure starts the contact-PII clock ----------------------------------------------


def test_completing_a_campaign_records_closed_at_and_nothing_else_does(client, db_session):
    from app.models.campaign import Campaign

    created = client.post("/api/v1/campaigns", json={"name": "closure"}).json()
    cid = created["id"]
    row = lambda: db_session.execute(  # noqa: E731
        select(Campaign.closed_at).where(Campaign.id == cid)
    ).scalar_one()
    assert row() is None
    client.patch(f"/api/v1/campaigns/{cid}", json={"status": "active"})
    client.patch(f"/api/v1/campaigns/{cid}", json={"status": "paused"})
    assert row() is None  # pausing is not closing
    db_session.expire_all()
    assert client.patch(f"/api/v1/campaigns/{cid}", json={"status": "completed"}).status_code == 200
    db_session.expire_all()
    assert row() is not None
