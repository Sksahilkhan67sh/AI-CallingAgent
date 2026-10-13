"""Fixtures for the CP15B ops tests: throwaway age keys and a throwaway PostgreSQL cluster
(its own directory, socket and port -- never the cluster used by the normal test run)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ops.helpers import (
    IS_ROOT,
    PG_BIN,
    env_with_pg,
    have,
    private_dir,
    requires_age,
    requires_pg,
    scratch_base,
)

CLUSTER_PORT = 55441


def _pg(cmd: list[str], **kw) -> subprocess.CompletedProcess[str]:
    if IS_ROOT:
        cmd = ["runuser", "-m", "-u", "postgres", "--", *cmd]
    return subprocess.run(cmd, env=env_with_pg(), capture_output=True, text=True, check=True, **kw)  # noqa: S603


@pytest.fixture(scope="session")
def age_keys(tmp_path_factory):
    """Two throwaway age key pairs; deleted when the session ends. Never written to the repo."""
    if not have("age", "age-keygen"):
        pytest.skip("age is not installed")
    base = Path(tmp_path_factory.mktemp("keys"))
    base.chmod(0o755)
    keys = {}
    for name in ("a", "wrong"):
        ident = base / f"{name}.key"
        subprocess.run(["age-keygen", "-o", str(ident)], capture_output=True, check=True)  # noqa: S603, S607
        ident.chmod(0o644)  # readable by the postgres OS user in the test run; throwaway only
        recipient = subprocess.run(  # noqa: S603
            ["age-keygen", "-y", str(ident)],
            capture_output=True,
            text=True,
            check=True,  # noqa: S607
        ).stdout.strip()
        keys[name] = {"identity": ident, "recipient": recipient}
    return keys


@pytest.fixture(scope="module")
def cluster():
    """A private cluster with a tiny database `ops` (table t: 5 rows; alembic_version)."""
    if not have("initdb", "pg_ctl", "psql"):
        pytest.skip("PostgreSQL server binaries are not installed (set PG_BIN)")
    base = scratch_base()
    if IS_ROOT:
        shutil.chown(base, "postgres", "postgres")
    data, sock = base / "data", private_dir(base, "sock", owner_pg=True)
    _pg([str(PG_BIN / "initdb"), "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8"])
    with (data / "postgresql.conf").open("a") as conf:
        conf.write(
            f"port = {CLUSTER_PORT}\nlisten_addresses = ''\nunix_socket_directories = '{sock}'\n"
            "wal_level = replica\nmax_wal_senders = 4\n"
        )
    _pg([str(PG_BIN / "pg_ctl"), "-D", str(data), "-w", "-l", str(base / "pg.log"), "start"])
    env = {
        "PGHOST": str(sock),
        "PGPORT": str(CLUSTER_PORT),
        "PGUSER": "postgres",
        "PGDATABASE": "ops",
    }
    _pg(
        [
            str(PG_BIN / "createdb"),
            "-h",
            str(sock),
            "-p",
            str(CLUSTER_PORT),
            "-U",
            "postgres",
            "ops",
        ]
    )
    sql = (
        "create table t (id int primary key, note text);"
        "insert into t select g, 'row ' || g from generate_series(1, 5) g;"
        "create table alembic_version (version_num varchar(32) primary key);"
        "insert into alembic_version values ('rev_test_1');"
    )
    _pg(
        [
            str(PG_BIN / "psql"),
            "-h",
            str(sock),
            "-p",
            str(CLUSTER_PORT),
            "-U",
            "postgres",
            "-d",
            "ops",
            "-c",
            sql,
        ]
    )
    yield {"base": base, "env": env, "sock": sock}
    subprocess.run(  # noqa: S603
        (["runuser", "-m", "-u", "postgres", "--"] if IS_ROOT else [])
        + [str(PG_BIN / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
        env=env_with_pg(),
        capture_output=True,
        check=False,
    )
    shutil.rmtree(base, ignore_errors=True)


__all__ = ["CLUSTER_PORT", "age_keys", "cluster", "requires_age", "requires_pg"]
