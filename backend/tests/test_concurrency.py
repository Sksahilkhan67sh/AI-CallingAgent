"""Checkpoint 01A Step 25: two concurrent attempts to create the same
(contact_id, attempt_number) call attempt must not both succeed. This
uses two real, independent DB connections/sessions and real threads --
not the single shared transactional `db_session` fixture -- because the
whole point is to exercise the database's own protection under
concurrent access, not just sequential application logic.
"""

import os
import threading

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.call_attempt import CallAttempt
from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CallAttemptState
from app.services.phone import normalize_phone_number

_engine = create_engine(os.environ["PRIMARY_DB_URL"])
_Session = sessionmaker(bind=_engine)


def test_concurrent_duplicate_attempt_creation_only_one_succeeds():
    setup_session = _Session()
    campaign = Campaign(name="Concurrency test campaign")
    setup_session.add(campaign)
    setup_session.flush()
    contact = Contact(
        campaign_id=campaign.id,
        phone_number="555-909-0101",
        normalized_phone_number=normalize_phone_number("555-909-0101"),
    )
    setup_session.add(contact)
    setup_session.commit()
    contact_id = contact.id
    campaign_id = campaign.id
    setup_session.close()

    results: list[bool] = []
    barrier = threading.Barrier(2)

    def attempt_insert() -> None:
        session = _Session()
        try:
            barrier.wait(timeout=5)
            session.add(
                CallAttempt(
                    contact_id=contact_id,
                    attempt_number=1,
                    state=CallAttemptState.INITIATED,
                )
            )
            session.commit()
            results.append(True)
        except Exception:
            session.rollback()
            results.append(False)
        finally:
            session.close()

    threads = [threading.Thread(target=attempt_insert) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [False, True]

    # cleanup -- this test commits for real, unlike the isolated fixture tests
    cleanup_session = _Session()
    cleanup_session.query(CallAttempt).filter_by(contact_id=contact_id).delete()
    cleanup_session.query(Contact).filter_by(id=contact_id).delete()
    cleanup_session.query(Campaign).filter_by(id=campaign_id).delete()
    cleanup_session.commit()
    cleanup_session.close()
