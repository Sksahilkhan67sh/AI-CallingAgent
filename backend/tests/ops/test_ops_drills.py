"""Executes the real drills (ops/backup/drill.sh) on a THROWAWAY PostgreSQL cluster and asserts
each drill's result. One invocation runs all of them (about 40 s); skipped with a clear message
when age / the PostgreSQL binaries are missing. Drill F (freshness / prune) is in
test_ops_freshness_prune.py. Durations are sandbox measurements, never production figures."""

import re
import sys

import pytest

from tests.ops.helpers import OPS, env_with_pg, requires_drills

pytestmark = requires_drills

DRILLS = {
    "A": "logical round trip: identical row counts + row checksums, alembic current/check",
    "B": "physical PITR to a named restore point (t1 present, t2 absent, timeline advanced)",
    "C": "encryption: right key works, wrong key fails, nothing in plaintext, modes 0600",
    "D": "corruption: one flipped byte fails (logical and base), even with the checksum bypassed",
    "E": "lost WAL segment: PITR fails loudly and the freshness check reports the gap",
    "G": "DNC loss: restore drops entries, reconcile (dry-run then apply) brings them back",
    "H": "erasure re-application: erased person returns from an older backup, then is re-scrubbed",
    "I": "old backup + newer code: alembic upgrade head and check",
    "J": "post-restore report lists stranded work and changes nothing",
}


@pytest.fixture(scope="module")
def drill_run(tmp_path_factory):
    import subprocess

    env = env_with_pg({"DRILL_PYTHON": sys.executable, "DRILL_PORT": "55460"})
    out = subprocess.run(  # noqa: S603
        [str(OPS / "drill.sh"), "all"],
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    results = {
        m[1]: (m[2], int(m[3]))
        for m in re.finditer(r"RESULT drill=(\w) status=(\w+) seconds=(\d+)", out.stdout)
    }
    return out, results


@pytest.mark.parametrize("drill", sorted(DRILLS))
def test_drill(drill_run, drill):
    out, results = drill_run
    assert (
        drill in results
    ), f"drill {drill} did not run:\n{out.stdout[-2000:]}\n{out.stderr[-2000:]}"
    status, _seconds = results[drill]
    assert status == "PASS", out.stdout[-3000:]


def test_all_drills_ran_and_the_run_exited_zero(drill_run):
    out, results = drill_run
    assert set(results) == set(DRILLS)
    assert out.returncode == 0


def test_no_key_material_or_plaintext_in_the_drill_transcript(drill_run):
    out, _ = drill_run
    assert "AGE-SECRET-KEY" not in out.stdout + out.stderr
    assert not re.search(
        r"\+91\d{10}", out.stdout + out.stderr
    ), "a phone number reached the transcript"
