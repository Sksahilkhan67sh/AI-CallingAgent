"""Production-mode refusals, credential hygiene, partial-file cleanup, file modes, manifests and
the WAL archive/restore commands. Real binaries; skipped with a message when they are missing."""

import hashlib
import json
import os
import signal
import subprocess
import time

import pytest

from tests.ops.helpers import (
    IS_ROOT,
    OPS,
    private_dir,
    requires_age,
    requires_pg,
    run_script,
    scratch_base,
)

CANARY = "CANARY-pw-7f3a91c2d8e44b10"
PHONE = "+919800000777"


@pytest.fixture
def env(cluster, age_keys):
    base = scratch_base()
    dest = private_dir(base, "dest", owner_pg=True)
    stage = private_dir(base, "stage", owner_pg=True)
    yield {
        **cluster["env"],
        "BACKUP_ENV": "production",
        "BACKUP_AGE_RECIPIENT": age_keys["a"]["recipient"],
        "BACKUP_DEST": str(dest),
        "WAL_ARCHIVE_DIR": str(dest / "wal"),
        "BACKUP_STAGING_DIR": str(stage),
        "BACKUP_ENV_FILE": "/nonexistent",
        "PGPASSWORD": CANARY,
    }
    import shutil

    shutil.rmtree(base, ignore_errors=True)


def _files(path):
    return sorted(p.name for p in path.rglob("*") if p.is_file())


@requires_pg
@requires_age
def test_production_refuses_a_missing_recipient(env):
    env.pop("BACKUP_AGE_RECIPIENT")
    result = run_script("pg_logical_backup.sh", env=env, as_pg=True)
    assert result.returncode != 0
    assert "requires an encryption recipient" in result.stderr
    assert not list(__import__("pathlib").Path(env["BACKUP_DEST"]).rglob("logical-*"))


@requires_pg
@requires_age
def test_production_refuses_the_unencrypted_override(env):
    env["BACKUP_ALLOW_UNENCRYPTED"] = "1"
    result = run_script("pg_logical_backup.sh", env=env, as_pg=True)
    assert result.returncode != 0 and "not permitted" in result.stderr


@requires_pg
@requires_age
def test_production_refuses_a_world_readable_destination(env):
    os.chmod(env["BACKUP_DEST"], 0o755)
    result = run_script("pg_logical_backup.sh", env=env, as_pg=True)
    assert result.returncode != 0 and "accessible by 'other'" in result.stderr


@requires_pg
def test_development_still_refuses_unencrypted_without_the_explicit_override(env):
    env.update(BACKUP_ENV="development")
    env.pop("BACKUP_AGE_RECIPIENT")
    result = run_script("pg_logical_backup.sh", env=env, as_pg=True)
    assert result.returncode != 0 and "BACKUP_ALLOW_UNENCRYPTED=1" in result.stderr


@requires_pg
def test_the_development_override_works_and_warns_loudly(env):
    env.update(BACKUP_ENV="development", BACKUP_ALLOW_UNENCRYPTED="1")
    env.pop("BACKUP_AGE_RECIPIENT")
    result = run_script("pg_logical_backup.sh", env=env, as_pg=True)
    assert result.returncode == 0, result.stderr
    assert "WARNING: writing UNENCRYPTED output" in result.stderr
    from pathlib import Path

    manifest = json.loads(next(Path(env["BACKUP_DEST"]).rglob("*.manifest.json")).read_text())
    assert manifest["encryption"]["tool"] == "none"


@requires_pg
@requires_age
def test_logical_backup_is_encrypted_mode_0600_and_the_manifest_is_correct_and_pii_free(env):
    from pathlib import Path

    run = run_script("pg_logical_backup.sh", env=env, as_pg=True)
    assert run.returncode == 0, run.stderr
    art = next(p for p in Path(env["BACKUP_DEST"], "logical").iterdir() if p.name.endswith(".age"))
    man = Path(f"{art}.manifest.json")
    assert art.read_bytes().startswith(b"age-encryption.org/v1")
    assert b"PGDMP" not in art.read_bytes()[:64]
    assert oct(art.stat().st_mode & 0o777) == oct(0o600) and oct(man.stat().st_mode & 0o777) == oct(
        0o600
    )
    m = json.loads(man.read_text())
    assert m["sha256"] == hashlib.sha256(art.read_bytes()).hexdigest()
    assert m["size_bytes"] == art.stat().st_size
    assert m["table_row_counts"] == {"t": 5, "alembic_version": 1}
    assert m["alembic_revision"] == "rev_test_1"
    assert m["database"] == "ops" and m["kind"] == "logical"
    assert m["encryption"]["tool"] == "age" and len(m["encryption"]["recipient_fingerprint"]) == 16
    blob = man.read_text()
    assert env["BACKUP_AGE_RECIPIENT"] not in blob and "AGE-SECRET-KEY" not in blob
    assert PHONE not in blob and "row 1" not in blob
    assert (
        run.stdout.strip().splitlines()[-1].startswith("SUMMARY script=pg_logical_backup status=ok")
    )


@requires_pg
@requires_age
def test_a_credential_never_appears_in_output_or_artifacts(env):
    from pathlib import Path

    ok = run_script("pg_logical_backup.sh", env=env, as_pg=True)
    bad = run_script(
        "pg_logical_backup.sh", env={**env, "PGDATABASE": "does_not_exist"}, as_pg=True
    )
    assert ok.returncode == 0 and bad.returncode != 0
    for res in (ok, bad):
        assert CANARY not in res.stdout + res.stderr
    for path in Path(env["BACKUP_DEST"]).rglob("*"):
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes()
    assert not any(p.name.startswith(".") for p in Path(env["BACKUP_DEST"], "logical").iterdir())


@requires_pg
@requires_age
def test_a_killed_run_leaves_no_partial_or_plaintext_file(env):
    """SIGTERM in the middle of the dump pipeline: nothing partial, nothing unencrypted."""
    from pathlib import Path

    from tests.ops.helpers import env_with_pg

    work = scratch_base()  # traversable by the postgres OS user (pytest's tmp_path is not)
    work.chmod(0o777)
    fake = work / "bin"
    started = work / "started"
    fake.mkdir()
    fake.chmod(0o755)
    script = fake / "pg_dump"
    script.write_text(
        f"#!/bin/sh\nprintf 'PGDMP-synthetic-plaintext'\ntouch '{started}'\nsleep 60\n"
    )
    script.chmod(0o755)
    full_env = env_with_pg(env)
    full_env["PATH"] = f"{fake}:{full_env['PATH']}"
    cmd = [str(OPS / "pg_logical_backup.sh")]
    if IS_ROOT:  # runuser resets PATH even with -m: pass it explicitly through env(1)
        cmd = ["runuser", "-m", "-u", "postgres", "--", "env", f"PATH={full_env['PATH']}", *cmd]
    proc = subprocess.Popen(
        cmd,
        env=full_env,
        stdout=subprocess.PIPE,  # noqa: S603
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    for _ in range(100):
        if started.exists():
            break
        time.sleep(0.1)
    assert started.exists(), "the fake pg_dump never ran"
    logical = Path(env["BACKUP_DEST"], "logical")
    assert any(p.name.startswith(".") for p in logical.iterdir())  # the partial exists mid-run
    os.killpg(proc.pid, signal.SIGTERM)
    proc.wait(timeout=30)
    time.sleep(0.5)
    assert list(logical.iterdir()) == [], list(logical.iterdir())
    assert list(Path(env["BACKUP_STAGING_DIR"]).iterdir()) == []


@requires_pg
@requires_age
def test_a_failed_base_backup_leaves_no_staging_data(env):
    """pg_verifybackup/pg_basebackup failure must remove the plaintext staging copy."""
    from pathlib import Path

    from tests.ops.helpers import env_with_pg

    work = scratch_base()
    fake = work / "bin"
    fake.mkdir()
    fake.chmod(0o755)
    script = fake / "pg_basebackup"
    script.write_text(
        '#!/bin/sh\nfor a in "$@"; do case "$a" in --pgdata=*) d="${a#--pgdata=}";; esac; done\n'
        'mkdir -p "$d"; echo PLAINTEXT-SYNTHETIC > "$d/secret"; exit 3\n'
    )
    script.chmod(0o755)
    full_env = env_with_pg(env)
    full_env["PATH"] = f"{fake}:{full_env['PATH']}"
    cmd = [str(OPS / "pg_base_backup.sh")]
    if IS_ROOT:  # runuser resets PATH even with -m: pass it explicitly through env(1)
        cmd = ["runuser", "-m", "-u", "postgres", "--", "env", f"PATH={full_env['PATH']}", *cmd]
    result = subprocess.run(cmd, env=full_env, capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode != 0
    assert list(Path(env["BACKUP_STAGING_DIR"]).iterdir()) == []
    assert not any(Path(env["BACKUP_DEST"]).rglob("base-*"))


# ---- WAL archive / restore commands ---------------------------------------------------------

SEG = "000000010000000000000007"


@pytest.fixture
def wal(age_keys):
    base = scratch_base()
    arch = private_dir(base, "wal", owner_pg=False)
    env = {
        "BACKUP_ENV": "production",
        "BACKUP_AGE_RECIPIENT": age_keys["a"]["recipient"],
        "WAL_ARCHIVE_DIR": str(arch),
        "BACKUP_ENV_FILE": "/nonexistent",
        "BACKUP_AGE_IDENTITY_FILE": str(age_keys["a"]["identity"]),
    }
    src = base / "src"
    src.write_bytes(os.urandom(4096))
    yield {"env": env, "arch": arch, "src": src, "base": base}
    import shutil

    shutil.rmtree(base, ignore_errors=True)


@requires_age
def test_archive_then_restore_round_trips_atomically_and_is_idempotent(wal):
    r = run_script("archive_wal.sh", str(wal["src"]), SEG, env=wal["env"])
    assert r.returncode == 0, r.stderr
    again = run_script("archive_wal.sh", str(wal["src"]), SEG, env=wal["env"])
    assert again.returncode == 0  # PostgreSQL retries: identical content is success
    assert sorted(p.name for p in wal["arch"].iterdir()) == [f"{SEG}.age", f"{SEG}.sha256"]
    assert (wal["arch"] / f"{SEG}.age").read_bytes().startswith(b"age-encryption.org/v1")
    out = wal["base"] / "restored"
    assert run_script("restore_wal.sh", SEG, str(out), env=wal["env"]).returncode == 0
    assert out.read_bytes() == wal["src"].read_bytes()


@requires_age
def test_archiving_different_content_under_an_existing_name_is_refused(wal):
    assert run_script("archive_wal.sh", str(wal["src"]), SEG, env=wal["env"]).returncode == 0
    other = wal["base"] / "other"
    other.write_bytes(b"different")
    r = run_script("archive_wal.sh", str(other), SEG, env=wal["env"])
    assert r.returncode != 0 and "DIFFERENT content" in r.stderr


@requires_age
@pytest.mark.parametrize(
    "name",
    ["../../etc/passwd", "short", "000000010000000000000007.evil", "000000010000000000000007/../x"],
)
def test_archive_and_restore_reject_unexpected_file_names(wal, name):
    assert run_script("archive_wal.sh", str(wal["src"]), name, env=wal["env"]).returncode != 0
    assert (
        run_script("restore_wal.sh", name, str(wal["base"] / "o"), env=wal["env"]).returncode != 0
    )
    assert not (wal["base"] / "o").exists()


@requires_age
def test_restore_exit_codes_missing_is_1_and_corrupt_is_126(wal):
    out = str(wal["base"] / "o")
    assert run_script("restore_wal.sh", SEG, out, env=wal["env"]).returncode == 1  # not archived
    run_script("archive_wal.sh", str(wal["src"]), SEG, env=wal["env"])
    enc = wal["arch"] / f"{SEG}.age"
    data = bytearray(enc.read_bytes())
    data[-5] ^= 0xFF
    enc.write_bytes(bytes(data))
    r = run_script("restore_wal.sh", SEG, out, env=wal["env"])
    assert r.returncode == 126  # PostgreSQL treats 126 as FATAL: recovery never ends silently
    assert not os.path.exists(out)


@requires_age
def test_archive_refuses_an_unencrypted_production_setup(wal):
    env = {k: v for k, v in wal["env"].items() if k != "BACKUP_AGE_RECIPIENT"}
    r = run_script("archive_wal.sh", str(wal["src"]), SEG, env=env)
    assert r.returncode != 0 and not list(wal["arch"].iterdir())


@requires_pg
@requires_age
def test_a_backup_runs_from_nothing_but_the_environment_file(cluster, age_keys):
    """The production path: cron/archive_command give the script a bare environment and only
    BACKUP_ENV_FILE. Every variable in the file (PG*, BACKUP_*) must reach pg_dump/psql."""
    from pathlib import Path

    from tests.ops.helpers import env_with_pg

    base = scratch_base()
    dest = private_dir(base, "dest", owner_pg=True)
    cfg = base / "backup.env"
    cluster_env = cluster["env"]
    cfg.write_text(
        "\n".join(
            [
                "BACKUP_ENV=production",
                f"BACKUP_AGE_RECIPIENT={age_keys['a']['recipient']}",
                f"BACKUP_DEST={dest}",
                *[f"{k}={v}" for k, v in cluster_env.items()],
            ]
        )
        + "\n"
    )
    cfg.chmod(0o600)
    if IS_ROOT:
        import shutil

        shutil.chown(cfg, "postgres", "postgres")
    bare = env_with_pg({"BACKUP_ENV_FILE": str(cfg)})
    assert not any(k.startswith("PG") for k in bare)
    result = run_script("pg_logical_backup.sh", env=bare, as_pg=True)
    assert result.returncode == 0, result.stderr
    art = next(Path(dest, "logical").glob("logical-*.dump.age"))
    manifest = json.loads(Path(f"{art}.manifest.json").read_text())
    # it must have reached the database named in the FILE, not whatever the defaults point at
    assert manifest["database"] == "ops"
    assert manifest["table_row_counts"] == {"t": 5, "alembic_version": 1}
    # and the file itself is refused when anyone else can read it
    cfg.chmod(0o644)
    refused = run_script("pg_logical_backup.sh", env=bare, as_pg=True)
    assert refused.returncode != 0 and "accessible by 'other'" in refused.stderr
