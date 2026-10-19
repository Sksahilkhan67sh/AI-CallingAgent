"""Fail CI when more tests skip under a package prefix than the stated allowance.

    python scripts/ci_skip_budget.py /tmp/junit.xml --prefix tests.ops --allow 0

Why: the backup / erasure safety tests skip themselves when a tool (`age`, shellcheck, the
PostgreSQL binaries) is missing. A silent skip looks like a pass, so the budget makes it loud.
Prints GitHub annotations so the count is visible on the check run.
"""

import argparse
import sys
import xml.etree.ElementTree as ET  # noqa: S405  (our own pytest output, not untrusted input)
from collections import Counter


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("junit_xml")
    parser.add_argument("--prefix", required=True, help="junit classname prefix, e.g. tests.ops")
    parser.add_argument("--allow", type=int, required=True, help="most skips tolerated")
    args = parser.parse_args(argv)

    cases = [
        case
        for case in ET.parse(args.junit_xml).getroot().iter("testcase")  # noqa: S314
        if case.get("classname", "").startswith(args.prefix)
    ]
    if not cases:
        print(f"::error title=skip-budget::no {args.prefix} test ran: the budget cannot be checked")
        return 1

    reasons = Counter(
        (case.find("skipped").get("message") or "?")[:100]  # type: ignore[union-attr]
        for case in cases
        if case.find("skipped") is not None
    )
    skipped = sum(reasons.values())
    summary = f"{skipped} skipped of {len(cases)} in {args.prefix} (allowance {args.allow})"
    for reason, count in reasons.most_common():
        print(f"  {count} x {reason}")
    if skipped > args.allow:
        print(f"::error title=skip-budget::{summary}")
        return 1
    print(f"::notice title=skip-budget::{summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
