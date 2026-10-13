"""Every ops script parses, is shellcheck-clean (when shellcheck exists), documents itself and
follows the safety header rules."""

import shutil
import subprocess

import pytest

from tests.ops.helpers import OPS, SCRIPTS, run_script

ALL = [*SCRIPTS, OPS / "lib.sh"]


def test_the_expected_scripts_exist():
    names = {p.name for p in SCRIPTS}
    assert names >= {
        "pg_logical_backup.sh",
        "pg_base_backup.sh",
        "archive_wal.sh",
        "restore_wal.sh",
        "verify_backup.sh",
        "prune_backups.sh",
        "check_backup_freshness.sh",
        "restore_logical.sh",
        "restore_pitr.sh",
        "drill.sh",
    }


@pytest.mark.parametrize("script", ALL, ids=lambda p: p.name)
def test_bash_syntax_and_safety_header(script):
    assert subprocess.run(["bash", "-n", str(script)], check=False).returncode == 0  # noqa: S603, S607
    text = script.read_text()
    assert text.startswith("#!/usr/bin/env bash")
    if script.name != "lib.sh":
        assert "set -Eeuo pipefail" in text
        assert "set -x" not in text and "set -o xtrace" not in text
    assert "umask 077" in (OPS / "lib.sh").read_text()


@pytest.mark.skipif(not shutil.which("shellcheck"), reason="shellcheck is not installed")
@pytest.mark.parametrize("script", ALL, ids=lambda p: p.name)
def test_shellcheck_clean(script):
    result = subprocess.run(  # noqa: S603, S607
        ["shellcheck", "-x", str(script)], capture_output=True, text=True, check=False, cwd=OPS
    )
    assert result.returncode == 0, result.stdout


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_help_works_and_needs_no_environment(script):
    result = run_script(script, "--help")
    assert result.returncode == 0
    assert script.name in result.stdout or "Usage" in result.stdout
    assert "ERROR" not in result.stderr


def test_no_script_takes_a_secret_as_an_argument():
    forbidden = ("--password", "--passphrase", "PGPASSWORD=", "--secret", "--key-material")
    for script in ALL:
        text = script.read_text()
        for needle in forbidden:
            assert needle not in text, (script.name, needle)
