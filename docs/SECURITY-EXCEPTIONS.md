# Security exceptions (dependency audit allowlist)

CI (`ops/ci/audit_gate.py`) reads the table under **Active exceptions** as its allowlist:

* `pip-audit` of `backend/constraints.txt` fails on **any** finding that is not listed
  (pip-audit does not report a severity, so nothing is waved through by severity).
* `npm audit --omit=dev` fails on any **high or critical** finding that is not listed.

An entry needs the advisory id (`PYSEC-…`, `GHSA-…`, `CVE-…`), the package, why it does not apply
or has no fix, an owner, and a review-by date (`YYYY-MM-DD`). **When the review-by date passes the
entry stops counting and the build fails again**, so an exception cannot be forgotten.
Rows outside the table are documentation only and are never read as exceptions.

## Active exceptions

| Advisory | Package | Why it is accepted | Owner | Review by |
|---|---|---|---|---|

_None. The pinned backend set and the production frontend dependencies have no open findings
(HF2, verified: see `docs/HARDENING-HF2-NOTES.md`)._

## Known, not gated: development-tooling findings

These are in dev-only packages that never ship in the backend image or the frontend runtime
image, so the CI gate does not read them. They are listed so they are not lost.

* **`braces` (GHSA-vfj7-8cjw-p6xm)** -- stack exhaustion on deeply nested glob patterns, reported
  through `eslint-config-next` -> `@next/eslint-plugin-next` -> `fast-glob` -> `micromatch` ->
  `braces` (`npm audit`, 5 "high" rows, one root cause). It affects the linter reading this
  repository's own files, not any request-handling code. The advisory lists no patched version;
  npm's suggested "fix" is a downgrade to `eslint-config-next` 14, which would undo the Next 16
  alignment. Re-check on the next Dependabot cycle (weekly). Owner: repository owner.
