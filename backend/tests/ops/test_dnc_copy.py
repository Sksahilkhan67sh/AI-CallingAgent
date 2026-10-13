"""Secondary DNC copy (export / reconcile) and the erasure-fingerprint copy: encryption,
integrity, idempotence, never-deletes, removal handling, counts-only audit, CLI behaviour."""

import contextlib
import json
import os
import re
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.models.audit_log import AuditLog
from app.models.enums import SuppressionSource
from app.models.erasure_log import ErasureLog
from app.models.suppression import Suppression
from app.repositories.suppression_repository import SuppressionRepository
from app.scripts import export_suppressions, reconcile_suppressions
from app.services.data_lifecycle.erasure import erase, reapply_erasures
from app.services.dnc_copy import DncCopyError, export_copy, read_copy
from app.services.phone import phone_fingerprint
from tests.cp15_helpers import NOW, make_campaign, make_contact, next_number
from tests.ops.helpers import requires_age

pytestmark = requires_age


def add_dnc(db, number, *, source=SuppressionSource.MANUAL_API, reason="synthetic", age_days=0.0):
    SuppressionRepository(db).insert_if_absent(number, source=source, reason=reason, created_by="t")
    if age_days:
        db.execute(
            text("update suppression set requested_at = :t where phone_number = :n"),
            {"t": NOW - timedelta(days=age_days), "n": number},
        )
    db.flush()


@pytest.fixture
def dest(tmp_path):
    d = tmp_path / "dnc"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def rec(age_keys):
    return [age_keys["a"]["recipient"]]


def _export(db, dest, rec, **kw):
    kw.setdefault("now", NOW)
    return export_copy(db, dest, recipients=rec, **kw)


def _count(db, model):
    return db.execute(select(func.count()).select_from(model)).scalar_one()


# ---- export ------------------------------------------------------------------------------


def test_export_is_encrypted_0600_checksummed_and_leaves_no_plaintext(db_session, dest, rec):
    numbers = [next_number() for _ in range(3)]
    for n in numbers:
        add_dnc(db_session, n)
    manifest = _export(db_session, dest, rec)
    files = sorted(p.name for p in dest.iterdir())
    assert files == [manifest["artifact"], f"{manifest['artifact']}.manifest.json"]
    art = dest / manifest["artifact"]
    raw = art.read_bytes()
    assert raw.startswith(b"age-encryption.org/v1")
    assert not any(n.encode() in raw for n in numbers)
    assert oct(art.stat().st_mode & 0o777) == oct(0o600)
    assert oct((dest / files[1]).stat().st_mode & 0o777) == oct(0o600)
    import hashlib

    assert manifest["sha256"] == hashlib.sha256(raw).hexdigest()
    assert manifest["row_count"] == 3 and manifest["kind"] == "dnc"
    assert re.fullmatch(r"dnc-\d{8}T\d{6}Z\.csv\.age", manifest["artifact"])
    on_disk = json.loads((dest / files[1]).read_text())
    assert on_disk == manifest
    assert not any(n in json.dumps(manifest) for n in numbers)


def test_export_holds_only_the_four_documented_fields(db_session, dest, rec, age_keys):
    number = next_number()
    add_dnc(db_session, number, reason="opt-out on call")
    manifest = _export(db_session, dest, rec)
    _, rows = read_copy(dest / manifest["artifact"], age_keys["a"]["identity"])
    row = next(rows)
    assert (row.phone_number, row.source, row.reason) == (number, "manual_api", "opt-out on call")
    assert set(vars(row)) == {"phone_number", "source", "reason", "created_at"}


def test_export_manifest_newest_created_at_and_empty_table(db_session, dest, rec):
    assert _export(db_session, dest, rec)["newest_created_utc"] is None
    add_dnc(db_session, next_number(), age_days=3)
    newest = _export(db_session, dest, rec, now=NOW + timedelta(seconds=5))["newest_created_utc"]
    assert newest.startswith((NOW - timedelta(days=3)).date().isoformat())


def test_export_refuses_to_overwrite_and_cleans_up_on_failure(db_session, dest, rec):
    _export(db_session, dest, rec)
    with pytest.raises(DncCopyError, match="already exists"):
        _export(db_session, dest, rec)
    with pytest.raises(DncCopyError, match="age failed"):
        _export(db_session, dest, ["not-an-age-recipient"], now=NOW + timedelta(seconds=9))
    assert not [p for p in dest.iterdir() if p.name.startswith(".")]


def test_export_production_rules(db_session, dest, rec):
    with pytest.raises(DncCopyError, match="recipient is required"):
        _export(db_session, dest, [])
    with pytest.raises(DncCopyError, match="not supported"):
        _export(db_session, dest, rec, allow_unencrypted=True)
    dest.chmod(0o755)
    with pytest.raises(DncCopyError, match="accessible by 'other'"):
        _export(db_session, dest, rec)
    assert _export(db_session, dest, rec, env="development")["row_count"] == 0


# ---- read / integrity --------------------------------------------------------------------


def test_read_rejects_wrong_identity_tampering_and_a_missing_manifest(
    db_session, dest, rec, age_keys
):
    add_dnc(db_session, next_number())
    manifest = _export(db_session, dest, rec)
    art = dest / manifest["artifact"]
    ident = age_keys["a"]["identity"]
    with pytest.raises(DncCopyError, match="could not decrypt") as err:
        read_copy(art, age_keys["wrong"]["identity"])
    assert "AGE-SECRET-KEY" not in str(err.value)

    original = art.read_bytes()
    flipped = bytearray(original)
    flipped[len(flipped) // 2] ^= 0xFF
    art.write_bytes(bytes(flipped))
    with pytest.raises(DncCopyError, match="CHECKSUM MISMATCH"):
        read_copy(art, ident)
    art.write_bytes(original)
    Path(f"{art}.manifest.json").rename(dest / "moved.json")
    with pytest.raises(DncCopyError, match="manifest next to the copy is missing"):
        read_copy(art, ident)


def test_read_rejects_a_copy_whose_row_count_disagrees_and_the_wrong_kind(
    db_session, dest, rec, age_keys
):
    add_dnc(db_session, next_number())
    manifest = _export(db_session, dest, rec)
    art = dest / manifest["artifact"]
    mpath = Path(f"{art}.manifest.json")
    tampered = {**manifest, "row_count": 99}
    mpath.write_text(json.dumps(tampered))
    with pytest.raises(DncCopyError, match="row count"):
        read_copy(art, age_keys["a"]["identity"])
    mpath.write_text(json.dumps(manifest))
    with pytest.raises(DncCopyError, match="not a erasure copy manifest"):
        read_copy(art, age_keys["a"]["identity"], kind="erasure")


# ---- reconcile ---------------------------------------------------------------------------


def _lose(db, numbers):
    """Simulate a restore that predates these numbers."""
    for n in numbers:
        db.execute(text("delete from suppression where phone_number = :n"), {"n": n})
    db.flush()


def _rows(db, dest, rec, ident):
    manifest = _export(db, dest, rec)
    _, rows = read_copy(dest / manifest["artifact"], ident)
    return list(rows)


def test_reconcile_dry_run_then_apply_adds_only_missing_and_never_deletes(
    db_session, dest, rec, age_keys
):
    keep, lost = next_number(), [next_number(), next_number()]
    add_dnc(db_session, keep, reason="original")
    for n in lost:
        add_dnc(db_session, n, source=SuppressionSource.AGENT_IN_CALL)
    rows = _rows(db_session, dest, rec, age_keys["a"]["identity"])
    _lose(db_session, lost)
    extra = next_number()
    add_dnc(db_session, extra, reason="only in the restored db")  # not in the copy: must survive

    dry = reconcile_suppressions.reconcile(db_session, rows, apply=False)
    assert dry["would_add"] == 2 and dry["added"] == 0 and dry["already_present"] == 1
    assert _count(db_session, Suppression) == 2
    assert not db_session.execute(
        select(AuditLog).where(AuditLog.action == "suppression.reconciled")
    ).first()

    applied = reconcile_suppressions.reconcile(db_session, rows, apply=True)
    assert applied["added"] == 2
    numbers = set(db_session.execute(select(Suppression.phone_number)).scalars())
    assert numbers == {keep, extra, *lost}
    originals = {r.phone_number: r for r in db_session.execute(select(Suppression)).scalars()}
    assert (
        originals[keep].reason == "original"
        and originals[extra].reason == "only in the restored db"
    )
    for n in lost:
        assert originals[n].source == SuppressionSource.AGENT_IN_CALL  # original source kept
        assert (
            originals[n].reason == "restore_reconcile"
            and originals[n].created_by == "restore_reconcile"
        )

    again = reconcile_suppressions.reconcile(db_session, rows, apply=True)
    assert again["added"] == 0 and again["would_add"] == 0 and _count(db_session, Suppression) == 4


def test_reconcile_audit_event_is_counts_only(db_session, dest, rec, age_keys):
    n = next_number()
    add_dnc(db_session, n)
    rows = _rows(db_session, dest, rec, age_keys["a"]["identity"])
    _lose(db_session, [n])
    reconcile_suppressions.reconcile(db_session, rows, apply=True)
    event = db_session.execute(
        select(AuditLog).where(AuditLog.action == "suppression.reconciled")
    ).scalar_one()
    assert event.actor == "restore_reconcile"
    assert event.event_metadata == {
        "copy_rows": 1,
        "already_present": 0,
        "skipped_removed": 0,
        "would_add": 1,
        "added": 1,
    }
    assert n not in json.dumps(event.event_metadata) and event.entity_id is None


def test_reconcile_treats_a_non_normalised_number_in_the_copy_as_the_same_number(
    db_session, dest, rec, age_keys
):
    e164 = next_number()
    add_dnc(db_session, e164)
    rows = _rows(db_session, dest, rec, age_keys["a"]["identity"])
    national = e164.removeprefix("+91")  # 10-digit national form of the same Indian number
    rows = [type(rows[0])(national, rows[0].source, rows[0].reason, rows[0].created_at)]
    counts = reconcile_suppressions.reconcile(db_session, rows, apply=True)
    assert counts["already_present"] == 1 and counts["added"] == 0
    assert _count(db_session, Suppression) == 1
    _lose(db_session, [e164])
    assert reconcile_suppressions.reconcile(db_session, rows, apply=True)["added"] == 1
    assert (
        db_session.execute(select(Suppression.phone_number)).scalar_one() == e164
    )  # canonical key


def test_reconcile_honours_a_removal_recorded_inside_the_restored_data(
    db_session, dest, rec, age_keys
):
    removed, re_added, lost_unseen = next_number(), next_number(), next_number()
    for n in (removed, re_added, lost_unseen):
        add_dnc(db_session, n, age_days=10)
    rows = _rows(db_session, dest, rec, age_keys["a"]["identity"])
    _lose(db_session, [removed, re_added, lost_unseen])
    key = get_settings().jwt_signing_key

    def removal(number, days_ago):
        db_session.add(
            AuditLog(
                actor="admin",
                action="suppression.removed",
                entity_type="suppression",
                entity_id=uuid.uuid4(),
                event_metadata={"fingerprint": phone_fingerprint(number, key)},
                created_at=NOW - timedelta(days=days_ago),
            )
        )

    removal(removed, 5)  # removed AFTER it was added (day -10): deliberate removal, honoured
    removal(re_added, 20)  # removal predates the (re-)addition at day -10: must be re-added
    db_session.flush()
    counts = reconcile_suppressions.reconcile(db_session, rows, apply=True)
    present = set(db_session.execute(select(Suppression.phone_number)).scalars())
    assert counts["skipped_removed"] == 1 and removed not in present
    assert re_added in present
    assert lost_unseen in present  # no removal record visible: re-adding is the SAFE direction


# ---- erasure fingerprint copy ------------------------------------------------------------


def test_erasure_copy_round_trip_and_reapply_after_a_simulated_restore(
    db_session, dest, rec, age_keys
):
    number = next_number()
    campaign = make_campaign(db_session)
    contact = make_contact(db_session, campaign, number)
    erase(db_session, actor="t", now=NOW, phone_number=number, reason="subject request")
    manifest = _export(db_session, dest, rec, kind="erasure", now=NOW + timedelta(seconds=1))
    assert manifest["kind"] == "erasure" and manifest["artifact"].startswith("erasure-")
    raw = (dest / manifest["artifact"]).read_bytes()
    assert number.encode() not in raw
    _, rows = read_copy(dest / manifest["artifact"], age_keys["a"]["identity"], kind="erasure")
    rows = list(rows)
    assert len(rows) == 1 and re.fullmatch(r"[0-9a-f]{64}", rows[0].fingerprint)

    # the restore: person is back, erasure_log is gone (it lived in the same database)
    db_session.execute(text("alter table erasure_log disable trigger erasure_log_append_only"))
    db_session.execute(text("delete from erasure_log"))
    db_session.execute(
        text(
            "update contact set phone_number = :n, normalized_phone_number = :n, purged_at = null,"
            " status = 'Pending' where id = :i"
        ),
        {"n": number, "i": contact.id},
    )
    db_session.expire_all()
    assert (
        reapply_erasures(db_session, now=NOW, apply=True).contacts_scrubbed == 0
    )  # nothing to apply

    dry = reconcile_suppressions.reconcile_erasures(db_session, rows, apply=False)
    assert dry["would_add"] == 1 and _count(db_session, ErasureLog) == 0
    done = reconcile_suppressions.reconcile_erasures(db_session, rows, apply=True)
    assert done["added"] == 1 and _count(db_session, ErasureLog) == 1
    assert reconcile_suppressions.reconcile_erasures(db_session, rows, apply=True)["added"] == 0
    report = reapply_erasures(db_session, now=NOW, apply=True)
    assert report.contacts_scrubbed == 1
    db_session.refresh(contact)
    assert contact.purged_at is not None and contact.phone_number.startswith("purged:")


# ---- command line ------------------------------------------------------------------------


@pytest.fixture
def cli(db_session, monkeypatch):
    for module in (export_suppressions, reconcile_suppressions):
        monkeypatch.setattr(module, "SessionLocal", lambda: contextlib.nullcontext(db_session))
    monkeypatch.delenv("BACKUP_ALLOW_UNENCRYPTED", raising=False)


def test_export_cli_exit_codes_and_output(cli, db_session, dest, age_keys, monkeypatch, capsys):
    number = next_number()
    add_dnc(db_session, number)
    assert export_suppressions.main([]) == 1  # no destination
    monkeypatch.setenv("BACKUP_AGE_RECIPIENT", age_keys["a"]["recipient"])
    assert export_suppressions.main(["--dest", str(dest)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("SUMMARY script=export_suppressions status=ok") and number not in out
    monkeypatch.delenv("BACKUP_AGE_RECIPIENT")
    assert (
        export_suppressions.main(["--dest", str(dest), "--now", "2030-01-01T00:00:00+00:00"]) == 1
    )
    monkeypatch.setenv("BACKUP_ALLOW_UNENCRYPTED", "1")
    monkeypatch.setenv("BACKUP_AGE_RECIPIENT", age_keys["a"]["recipient"])
    assert (
        export_suppressions.main(["--dest", str(dest), "--now", "2031-01-01T00:00:00+00:00"]) == 1
    )


def test_reconcile_cli_dry_run_is_the_default_and_prints_no_numbers(
    cli, db_session, dest, age_keys, monkeypatch, capsys
):
    number = next_number()
    add_dnc(db_session, number)
    manifest = _export(db_session, dest, [age_keys["a"]["recipient"]])
    art, ident = str(dest / manifest["artifact"]), str(age_keys["a"]["identity"])
    _lose(db_session, [number])
    assert reconcile_suppressions.main(["--copy", art, "--identity", ident]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "'would_add': 1" in out and number not in out
    assert _count(db_session, Suppression) == 0
    assert reconcile_suppressions.main(["--copy", art, "--identity", ident, "--apply"]) == 0
    assert "APPLIED" in capsys.readouterr().out and _count(db_session, Suppression) == 1
    assert (
        reconcile_suppressions.main(
            ["--copy", art, "--identity", str(age_keys["wrong"]["identity"])]
        )
        == 1
    )
    assert os.path.exists(art)  # the copy itself is never modified or deleted
