"""CP14 backfill: app/scripts/renormalize_phones.py -- dry run by default, collisions are
reported and skipped (never merged or deleted), idempotent, and prints no full numbers."""

import pytest
from sqlalchemy import select

from app.models.campaign import Campaign
from app.models.contact import Contact
from app.models.enums import CampaignStatus, ContactStatus, SuppressionSource
from app.models.suppression import Suppression
from app.scripts import renormalize_phones as script


def _campaign(db, name="renorm", region="IN"):
    c = Campaign(name=name, status=CampaignStatus.ACTIVE, default_region=region)
    db.add(c)
    db.flush()
    return c


def _contact(db, campaign, raw, stored):
    c = Contact(
        campaign_id=campaign.id,
        phone_number=raw,
        normalized_phone_number=stored,
        status=ContactStatus.PENDING,
    )
    db.add(c)
    db.flush()
    return c


def _suppression(db, number, contact=None):
    s = Suppression(
        phone_number=number,
        contact_id=contact.id if contact else None,
        source=SuppressionSource.MANUAL_API,
    )
    db.add(s)
    db.flush()
    return s


def _stored(db, row):
    db.expire(row)
    return row.normalized_phone_number if isinstance(row, Contact) else row.phone_number


def test_the_documented_c4_examples_are_repaired(db_session):
    campaign = _campaign(db_session)
    bare = _contact(db_session, campaign, "9876543210", "+19876543210")  # read as US
    trunk = _contact(db_session, campaign, "098765 43211", "+09876543211")  # trunk 0 kept
    good = _contact(db_session, campaign, "+91 98765 43212", "+919876543212")

    summary = script.run(db_session, apply=True)

    assert (summary.contacts.changed, summary.contacts.collisions) >= (2, 0)
    assert _stored(db_session, bare) == "+919876543210"
    assert _stored(db_session, trunk) == "+919876543211"
    assert _stored(db_session, good) == "+919876543212"  # already right: untouched


def test_dry_run_reports_but_writes_nothing(db_session):
    campaign = _campaign(db_session)
    bare = _contact(db_session, campaign, "9876543220", "+19876543220")
    linked = _suppression(db_session, "+19876543220", bare)

    summary = script.run(db_session, apply=False)

    assert summary.applied is False and summary.contacts.changed == 1
    assert summary.suppressions.changed == 1
    assert _stored(db_session, bare) == "+19876543220"
    assert _stored(db_session, linked) == "+19876543220"


def test_apply_is_idempotent(db_session):
    campaign = _campaign(db_session)
    _contact(db_session, campaign, "9876543230", "+19876543230")
    first = script.run(db_session, apply=True)
    second = script.run(db_session, apply=True)
    assert first.contacts.changed == 1
    assert (second.contacts.changed, second.suppressions.changed) == (0, 0)
    assert (second.contacts.collisions, second.contacts.invalid) == (0, 0)


def test_a_collision_with_an_existing_contact_is_listed_and_skipped(db_session):
    campaign = _campaign(db_session)
    legacy = _contact(db_session, campaign, "9876543240", "+19876543240")
    holder = _contact(db_session, campaign, "+91 98765 43240", "+919876543240")

    summary = script.run(db_session, apply=True)

    assert summary.contacts.collisions == 1 and summary.contacts.changed == 0
    assert _stored(db_session, legacy) == "+19876543240"  # NOT overwritten, NOT merged
    assert _stored(db_session, holder) == "+919876543240"
    assert db_session.get(Contact, legacy.id) is not None  # nothing deleted
    assert any("collision" in line for line in summary.contacts.notes)


def test_two_legacy_rows_that_become_the_same_number_one_moves_one_is_a_collision(db_session):
    campaign = _campaign(db_session)
    a = _contact(db_session, campaign, "9876543250", "+19876543250")
    b = _contact(db_session, campaign, "09876543250", "+09876543250")

    summary = script.run(db_session, apply=True)

    assert (summary.contacts.changed, summary.contacts.collisions) == (1, 1)
    assert {_stored(db_session, a), _stored(db_session, b)} == {
        "+919876543250",
        "+09876543250",
    } or {
        _stored(db_session, a),
        _stored(db_session, b),
    } == {"+919876543250", "+19876543250"}
    again = script.run(db_session, apply=True)  # re-running does not "resolve" it silently
    assert again.contacts.collisions == 1 and again.contacts.changed == 0


def test_the_same_number_in_different_campaigns_is_not_a_collision(db_session):
    one, two = _campaign(db_session, "one"), _campaign(db_session, "two")
    a = _contact(db_session, one, "9876543260", "+19876543260")
    b = _contact(db_session, two, "9876543260", "+19876543260")
    summary = script.run(db_session, apply=True)
    assert summary.contacts.collisions == 0
    assert _stored(db_session, a) == _stored(db_session, b) == "+919876543260"


def test_unparseable_rows_are_reported_and_left_alone(db_session):
    campaign = _campaign(db_session)
    junk = _contact(db_session, campaign, "12345", "+112345")
    summary = script.run(db_session, apply=True)
    assert summary.contacts.invalid >= 1 and _stored(db_session, junk) == "+112345"
    assert any("invalid_number" in line for line in summary.contacts.notes)


def test_a_linked_suppression_moves_together_with_its_contact(db_session):
    campaign = _campaign(db_session)
    contact = _contact(db_session, campaign, "9876543270", "+19876543270")
    suppression = _suppression(db_session, "+19876543270", contact)

    script.run(db_session, apply=True)

    assert _stored(db_session, contact) == _stored(db_session, suppression) == "+919876543270"


def test_a_suppression_is_not_moved_if_its_contact_was_skipped(db_session):
    campaign = _campaign(db_session)
    legacy = _contact(db_session, campaign, "9876543280", "+19876543280")
    _contact(db_session, campaign, "+91 98765 43280", "+919876543280")  # blocks the move
    suppression = _suppression(db_session, "+19876543280", legacy)

    summary = script.run(db_session, apply=True)

    assert _stored(db_session, legacy) == "+19876543280"
    assert _stored(db_session, suppression) == "+19876543280"  # still matches its contact
    assert summary.suppressions.collisions == 1  # reported (as skipped), not silently changed


def test_a_contactless_suppression_is_renormalized_from_the_stored_value(db_session):
    stored = _suppression(db_session, "919876543290")  # a legacy list entry, no '+'
    summary = script.run(db_session, apply=True)
    assert summary.suppressions.changed >= 1 and _stored(db_session, stored) == "+919876543290"


def test_two_suppressions_that_would_collide_are_reported_not_merged(db_session):
    first = _suppression(db_session, "919876543300")
    second = _suppression(db_session, "09876543300")
    summary = script.run(db_session, apply=True)
    assert summary.suppressions.collisions == 1 and summary.suppressions.changed == 1
    assert db_session.get(Suppression, first.id) and db_session.get(Suppression, second.id)
    numbers = {_stored(db_session, first), _stored(db_session, second)}
    assert "+919876543300" in numbers and len(numbers) == 2  # still two distinct rows


def test_output_never_prints_a_full_number(db_session, capsys):
    campaign = _campaign(db_session)
    _contact(db_session, campaign, "9876543310", "+19876543310")
    holder = _contact(db_session, campaign, "+91 98765 43310", "+919876543310")
    assert holder
    _contact(db_session, campaign, "12345", "+112345")
    summary = script.run(db_session, apply=False)
    script._print(summary)
    out = capsys.readouterr().out
    assert "changed=" in out and "collisions=" in out and "DRY RUN" in out
    for digits in ("9876543310", "876543310", "12345"):
        assert digits not in out.replace("contact=", "")


@pytest.mark.parametrize("flag", [[], ["--apply"]])
def test_cli_defaults_to_a_dry_run(flag, monkeypatch):
    parsed = {}

    def fake_run(db, *, apply):
        parsed["apply"] = apply
        return script.Summary(applied=apply)

    monkeypatch.setattr(script, "run", fake_run)
    monkeypatch.setattr(script, "SessionLocal", lambda: __import__("contextlib").nullcontext(None))
    script.main(flag)
    assert parsed["apply"] is bool(flag)


def test_contacts_with_no_changes_are_not_written(db_session):
    campaign = _campaign(db_session)
    row = _contact(db_session, campaign, "9876543320", "+919876543320")
    before = db_session.execute(select(Contact.updated_at).where(Contact.id == row.id)).scalar_one()
    script.run(db_session, apply=True)
    after = db_session.execute(select(Contact.updated_at).where(Contact.id == row.id)).scalar_one()
    assert before == after
