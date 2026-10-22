"""Invariant checker for the P2-7 database-resilience gate.

Run it after a failure scenario; it returns a list of violations (empty = healthy). It states
the two promises Phase 1 and Phase 2 make about the dial path and checks them against the real
database and queue, so a scenario cannot pass by merely not crashing:

* no duplicate dial: a phone number is never placed more than once;
* no lost job: every contact either has a durable attempt, or its job is still queued.
"""

from collections import Counter

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.call_attempt import CallAttempt


def kill_application_connections(admin_session: Session, application_name: str) -> int:
    """Terminate every backend the application's engine holds (idle and in use). Matches on the
    `application_name` the engine sets, so other engines in the process are left alone."""
    killed = admin_session.execute(
        text(
            "select count(pg_terminate_backend(pid)) from pg_stat_activity "
            "where application_name = :n and pid <> pg_backend_pid()"
        ),
        {"n": application_name},
    ).scalar_one()
    return int(killed)


def check_dial_invariants(
    *, session: Session, contact_ids: list[str], phones_dialed: list[str], pending: int, unread: int
) -> list[str]:
    violations: list[str] = []
    for phone, n in Counter(phones_dialed).items():
        if n > 1:
            violations.append(f"duplicate dial: {phone} was dialled {n} times")
    # Ids arrive as strings from some helpers and as UUIDs from the database: compare as text.
    wanted = [str(c) for c in contact_ids]
    with_attempt = {
        str(c)
        for c in session.execute(
            select(CallAttempt.contact_id).where(CallAttempt.contact_id.in_(wanted))
        ).scalars()
    }
    without = [c for c in wanted if c not in with_attempt]
    if len(without) > pending + unread:
        violations.append(
            f"lost job: {len(without)} contacts have no attempt but only "
            f"{pending + unread} jobs are still queued"
        )
    repeated = session.execute(
        select(CallAttempt.contact_id, CallAttempt.attempt_number, func.count())
        .where(CallAttempt.contact_id.in_(wanted))
        .group_by(CallAttempt.contact_id, CallAttempt.attempt_number)
        .having(func.count() > 1)
    ).all()
    for contact_id, number, n in repeated:
        violations.append(f"duplicate attempt row: contact {contact_id} attempt {number} x{n}")
    return violations


def forget_campaign(session: Session, campaign_id: str) -> None:
    """Delete a scenario's campaign and everything under it, children first (every foreign key
    is NO ACTION). A resilience scenario must leave the shared database as it found it: leftover
    contacts in `Dialing` / `RetryScheduled` change what a real worker does in later tests."""
    params = {"c": campaign_id}
    attempts = (
        "select a.id from call_attempt a join contact c on c.id = a.contact_id "
        "where c.campaign_id = :c"
    )
    for sql in (
        f"delete from call_analysis where call_attempt_id in ({attempts})",
        f"delete from call_event where call_attempt_id in ({attempts})",
        f"delete from conversation_session where call_attempt_id in ({attempts})",
        f"delete from working_memory_snapshot where attempt_id in ({attempts})",
        "delete from call_analysis where campaign_id = :c",
        f"delete from call_attempt where id in ({attempts})",
        "delete from suppression where contact_id in "
        "(select id from contact where campaign_id = :c)",
        "delete from contact where campaign_id = :c",
        "delete from retry_policy where campaign_id = :c",
        "delete from agent_config where campaign_id = :c",
        "delete from campaign where id = :c",
    ):
        session.execute(text(sql), params)
    session.commit()
