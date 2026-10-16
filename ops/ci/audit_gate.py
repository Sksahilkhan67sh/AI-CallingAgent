#!/usr/bin/env python3
"""CI dependency-audit gate (HF2).

    audit_gate.py pip [--requirements backend/constraints.txt]
    audit_gate.py npm [--dir frontend]

Both fail the build on an advisory that is not listed in docs/SECURITY-EXCEPTIONS.md:
  * pip: ANY pip-audit finding (pip-audit does not report a severity, so none is waved through),
  * npm: any HIGH or CRITICAL finding in the production dependencies (`--omit=dev`).
An exception needs an id, an owner, a reason and a review date; once the review date has passed
the exception stops counting and the build fails again, so nothing is ignored forever.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

EXCEPTIONS_FILE = Path(__file__).resolve().parents[2] / "docs" / "SECURITY-EXCEPTIONS.md"
_SECTION = "## Active exceptions"
_ADVISORY_ID = re.compile(r"^(PYSEC|GHSA|CVE|OSV)-[0-9A-Za-z-]+$")
_GHSA_IN_URL = re.compile(r"GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}")


def load_exceptions(text: str, today: date) -> tuple[set[str], list[str]]:
    """Return (advisory ids currently excepted, problems with the exceptions table)."""
    allowed: set[str] = set()
    problems: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line.strip() == _SECTION
            continue
        if not in_section or not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells or not _ADVISORY_ID.match(cells[0]):
            continue  # header, separator or an explanatory row
        advisory = cells[0]
        if len(cells) < 5 or not all(cells[1:5]):
            problems.append(f"{advisory}: needs package, reason, owner and review-by date")
            continue
        try:
            review_by = date.fromisoformat(cells[4])
        except ValueError:
            problems.append(f"{advisory}: review-by date {cells[4]!r} is not YYYY-MM-DD")
            continue
        if review_by < today:
            problems.append(f"{advisory}: exception expired on {review_by} -- re-review it")
            continue
        allowed.add(advisory)
    return allowed, problems


def evaluate_pip(findings: list[dict], allowed: set[str]) -> list[str]:
    """`findings` is pip-audit's JSON `dependencies` list."""
    failures = []
    for dep in findings:
        for vuln in dep.get("vulns", []):
            ids = {vuln["id"], *vuln.get("aliases", [])}
            if not ids & allowed:
                failures.append(f"{dep['name']}=={dep['version']}: {vuln['id']}")
    return sorted(set(failures))


def evaluate_npm(report: dict, allowed: set[str]) -> list[str]:
    """`report` is `npm audit --json`. Only HIGH/CRITICAL entries count."""
    failures = []
    for name, entry in report.get("vulnerabilities", {}).items():
        if entry.get("severity") not in ("high", "critical"):
            continue
        advisories = [via for via in entry.get("via", []) if isinstance(via, dict)]
        ids = [
            (m.group(0) if (m := _GHSA_IN_URL.search(a.get("url", ""))) else a.get("url", "?"))
            for a in advisories
        ]
        # A package that only inherits a vulnerability from another package (`via` is a name)
        # is reported through that package; judge it by its own advisories.
        if not advisories or all(i in allowed for i in ids):
            continue
        failures.append(f"{name} ({entry['severity']}): {', '.join(sorted(set(ids)))}")
    return sorted(failures)


def _report(label: str, failures: list[str], problems: list[str]) -> int:
    for problem in problems:
        print(f"EXCEPTION PROBLEM: {problem}", file=sys.stderr)
    if failures:
        print(f"{label}: {len(failures)} un-excepted finding(s):", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print(f"Fix them or add a reviewed entry to {EXCEPTIONS_FILE.name}.", file=sys.stderr)
    if failures or problems:
        return 1
    print(f"{label}: no un-excepted findings")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="kind", required=True)
    pip = sub.add_parser("pip")
    pip.add_argument("--requirements", default="backend/constraints.txt")
    npm = sub.add_parser("npm")
    npm.add_argument("--dir", default="frontend")
    args = parser.parse_args(argv)

    allowed, problems = load_exceptions(EXCEPTIONS_FILE.read_text(), date.today())
    if args.kind == "pip":
        run = subprocess.run(
            ["pip-audit", "-r", args.requirements, "--no-deps", "--disable-pip", "-f", "json"],
            capture_output=True,
            text=True,
        )
        if run.returncode not in (0, 1) or not run.stdout.strip():
            print(f"pip-audit failed to run:\n{run.stderr}", file=sys.stderr)
            return 2
        failures = evaluate_pip(json.loads(run.stdout)["dependencies"], allowed)
        return _report("pip-audit", failures, problems)

    run = subprocess.run(
        ["npm", "audit", "--omit=dev", "--json"], cwd=args.dir, capture_output=True, text=True
    )
    try:
        report = json.loads(run.stdout)
    except json.JSONDecodeError:
        print(f"npm audit failed to run:\n{run.stderr or run.stdout}", file=sys.stderr)
        return 2
    if "error" in report:
        print(f"npm audit failed: {report['error']}", file=sys.stderr)
        return 2
    return _report("npm audit", evaluate_npm(report, allowed), problems)


if __name__ == "__main__":
    sys.exit(main())
