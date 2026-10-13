"""Shared helpers for the CP15B ops tests: locate the scripts, run them (as the postgres OS
user when the suite itself runs as root, because initdb/pg_ctl refuse root), and skip cleanly
when the system binaries are not installed."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
OPS = REPO / "ops" / "backup"
PG_BIN = Path(os.environ.get("PG_BIN", "/usr/lib/postgresql/16/bin"))
SCRIPTS = sorted(p for p in OPS.glob("*.sh") if p.name != "lib.sh")
IS_ROOT = os.geteuid() == 0


def have(*tools: str) -> bool:
    return all(shutil.which(t) or (PG_BIN / t).exists() for t in tools)


requires_age = pytest.mark.skipif(not have("age", "age-keygen"), reason="age is not installed")
requires_pg = pytest.mark.skipif(
    not have(
        "initdb", "pg_ctl", "pg_dump", "pg_restore", "pg_basebackup", "pg_verifybackup", "psql"
    ),
    reason="PostgreSQL server/client binaries are not installed (set PG_BIN)",
)
requires_drills = pytest.mark.skipif(
    not (
        have("age", "age-keygen", "initdb", "pg_ctl", "pg_basebackup", "pg_verifybackup")
        and (not IS_ROOT or shutil.which("runuser"))
    ),
    reason="drills need age + PostgreSQL binaries (and runuser when running as root)",
)


def env_with_pg(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "BACKUP_", "NOW_"))}
    env["PATH"] = f"{PG_BIN}:{env['PATH']}"
    env.update(extra or {})
    return env


def run_script(
    script: str | Path, *args: str, env: dict[str, str] | None = None, as_pg: bool = False
) -> subprocess.CompletedProcess[str]:
    path = script if isinstance(script, Path) else OPS / script
    cmd = [str(path), *args]
    full_env = env_with_pg(env)
    if as_pg and IS_ROOT:  # runuser resets PATH even with -m: pass it explicitly through env(1)
        cmd = ["runuser", "-m", "-u", "postgres", "--", "env", f"PATH={full_env['PATH']}", *cmd]
    return subprocess.run(  # noqa: S603
        cmd, env=full_env, capture_output=True, text=True, timeout=600, check=False
    )


def private_dir(base: Path, name: str, mode: int = 0o700, owner_pg: bool = False) -> Path:
    path = base / name
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(mode)
    if owner_pg and IS_ROOT:
        shutil.chown(path, "postgres", "postgres")
    return path


def scratch_base() -> Path:
    base = Path(tempfile.mkdtemp(prefix="cp15b-test-", dir="/tmp"))
    base.chmod(0o755)
    return base
