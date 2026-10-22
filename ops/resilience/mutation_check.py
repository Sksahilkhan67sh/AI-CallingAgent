"""Prove the P2-7 gate is not vacuous: break the code four ways, require the gate to fail.

    cd backend && python ../ops/resilience/mutation_check.py [--only NAME ...]

For each mutation it edits one source file in place, runs `tests/test_p2_db_resilience.py`, and
requires a NON-zero exit; the original text is restored from memory afterwards (never with
`git checkout`, which would also discard uncommitted work). A baseline run first requires the
gate to pass unmutated. Exit 0 only if the baseline passes and every mutation is caught.
"""

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BACKEND = Path.cwd()
GATE = "tests/test_p2_db_resilience.py"


@dataclass(frozen=True)
class Mutation:
    name: str
    path: str
    old: str
    new: str
    expect: int  # how many occurrences of `old` must be present


MUTATIONS = [
    Mutation(
        "disable-pool-pre-ping",
        "app/core/database.py",
        "pool_pre_ping=s.db_pool_pre_ping,",
        "pool_pre_ping=False,",
        1,
    ),
    Mutation(
        "remove-statement-timeout",
        "app/core/database.py",
        '"statement_timeout": s.db_',
        '"statement_timeout": 0 * s.db_',
        2,  # the API role and the worker role
    ),
    Mutation(
        "remove-advisory-lock",
        "alembic/env.py",
        "        acquire_migration_lock(connection, wait_seconds)",
        "        pass  # MUTATION: advisory lock removed",
        1,
    ),
    Mutation(
        "remove-503-mapping",
        "app/main.py",
        "        application.add_exception_handler(db_error, handle_database_unavailable)",
        "        pass  # MUTATION: 503 mapping removed",
        1,
    ),
]


def run_gate() -> tuple[int, list[str]]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", GATE, "-q", "-p", "no:cacheprovider"],
        cwd=BACKEND,
        capture_output=True,
        text=True,
        check=False,
    )
    failed = re.findall(r"^FAILED \S*::(\S+)", proc.stdout, flags=re.MULTILINE)
    return proc.returncode, failed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", default=None)
    args = parser.parse_args()
    chosen = [m for m in MUTATIONS if not args.only or m.name in args.only]

    code, failed = run_gate()
    print(f"baseline (unmutated): exit {code}", "OK" if code == 0 else f"FAILED {failed}")
    if code != 0:
        return 1

    results = []
    for m in chosen:
        path = BACKEND / m.path
        original = path.read_text()
        found = original.count(m.old)
        if found != m.expect:
            print(f"{m.name}: expected {m.expect} occurrence(s) of the target, found {found}")
            return 1
        try:
            path.write_text(original.replace(m.old, m.new))
            code, failed = run_gate()
        finally:
            path.write_text(original)
        results.append((m.name, code != 0, failed))
        print(f"{m.name}: gate {'FAILED (caught)' if code != 0 else 'PASSED (NOT caught!)'}")
        for test in failed:
            print(f"    - {test}")

    after, _ = run_gate()
    print(f"restored: gate exit {after}", "OK" if after == 0 else "NOT RESTORED")
    return 0 if after == 0 and all(caught for _, caught, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
