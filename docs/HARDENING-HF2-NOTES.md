# HF2 — Dependencies and audit gates (hardening follow-up 2 of 3)

Branch `feature/hf2-dependencies-audit-gates`, from `develop` at `db5131c` (HF1 / PR #55 merged).
No migration, no new runtime dependency (versions of existing ones were bumped; `starlette` is now
declared because the app imports it directly).

## 0. Current state

Complete and delivered as a bundle. Nothing was pushed, merged or approved. No token was used: the
repository is public and was cloned anonymously.

## 1. Objective and scope

Clear the known dependency advisories, make backend builds reproducible, make `npm ci` work, and
fail CI on new high/critical findings. Out of scope (untouched): everything in HF3, CP12D, metrics.

## 2. Environment and baseline (before any change) — VERIFIED

| Gate | Result |
|---|---|
| Python | 3.12.3 (sandbox). Final gates re-run on **3.11.17**, the version CI and the image use (obtained with the dev-only tool `uv`) |
| `ruff check .` / `mypy app` | clean / clean (182 files) |
| `alembic heads` / `alembic check` | single head `e2d1c0b9a8f7` / no new operations |
| `pytest` | 1814 passed (the time-dependent flaky test happened to pass) |
| frontend `npm ci` | **FAILS** (ERESOLVE, `@types/node@^20` vs `vitest@5`) as described |
| frontend with `--legacy-peer-deps` | `tsc`, lint, 23 tests, build pass |
| `npm audit --omit=dev` | 3 findings: `next` (1 critical), `sharp`, `source-map-js` (high) |
| `pip-audit` (full resolved set) | 51 advisory rows in PyJWT, python-multipart, starlette, pytest |

## 3. Fixes

### 3.1 `get_db` commit timing broke under the FastAPI upgrade — VERIFIED
`get_db` commits in the exit code of a `yield` dependency. FastAPI 0.118+ runs that exit code
**after the response is sent**. With `fastapi==0.133.1` the pin tests (committed first, in
`012137d`, passing on 0.115.0) FAILED twice: the 201 left before the commit landed, and a failed
commit became a silent 201. Fix: one shared marker `DB_SESSION = Depends(get_db, scope="function")`
in `app/core/database.py`, used at all 51 call sites. `scope="function"` runs the exit code after
the endpoint returns and the response is built (`serialize_response` is inside that scope, checked in
the FastAPI source) but before it is sent, i.e. the old ordering. All sites must agree, because the
dependency cache is keyed by scope (a mix would give two sessions per request). Tests
(`tests/test_hf2_get_db_commit_timing.py`, real app, real `get_db`, raw ASGI harness probing the DB
from a second connection at `http.response.start`): committed-before-response; commit failure is
never a 2xx and persists nothing; a drift guard fails if any `get_db` use is not function-scoped
(mutation-checked: one plain `Depends(get_db)` makes it fail).
The reproduction was run against the new version before the fix (2 failed); the fix and the bump
are one commit (`f348ad3`) because the tree cannot be both.

### 3.2 Backend versions — VERIFIED
`fastapi 0.115.0→0.133.1` (first release without a starlette cap), `starlette 0.38.6→1.3.1`,
`python-multipart 0.0.9→0.0.31`, `PyJWT 2.9.0→2.15.0`, dev: `pytest 8.3.3→9.0.3`,
`pytest-asyncio 0.24.0→1.3.0` (lowest allowing pytest 9; no test uses it). `pip-audit` of the whole
pinned set: **no known vulnerabilities**. Full suite green.

### 3.3 JWT and multipart behavior — VERIFIED
New tests (`tests/test_hf2_jwt_and_multipart.py`): HS384/HS512 signed with the real key rejected;
tampered payload rejected; missing `sub`/`role` rejected; malformed multipart (no boundary,
truncated, garbage, no file part) on both import routes is a clean 400/422, never a 500, nothing
created. Valid and oversize imports are covered by existing tests (green).
**Defect found:** a correctly signed token with **no `exp`** was accepted forever (old and new
stack). Failing test first (`3eff1f5`), fix `b5ffd06`: `options={"require": ["exp"]}`. The app
always mints `exp`, so no legitimate session is affected (full suite green).

### 3.4 Reproducible builds — VERIFIED (image build UNVERIFIED)
`backend/constraints.txt` generated from a clean Python 3.11 venv; `scripts/constraints.sh
regenerate|check` (shellcheck clean). `check` exits 1 with a diff when a pin is removed and passes
otherwise. The Dockerfile installs with `-c constraints.txt`.

### 3.5 Frontend — VERIFIED
`npm audit fix` (no `--force`): `next 16.4.2`, `sharp 0.35.5`, `source-map-js 1.2.2`. `@types/node`
22, `eslint 9`, `eslint-config-next 16.4.2` (flat config `eslint.config.mjs`, `.eslintrc.json`
removed). **Plain `npm ci` succeeds**, `--legacy-peer-deps` removed (CI, Dockerfile, README).
`npm audit --omit=dev --audit-level=high`: 0. `tsc`, lint, 23 tests, build green on Node 22.

### 3.6 CI gates — YAML VERIFIED, GitHub run UNVERIFIED
`ops/ci/audit_gate.py` (stdlib only) reads the *Active exceptions* table of
`docs/SECURITY-EXCEPTIONS.md`; entries need id, package, reason, owner, review date and **expire**.
pip: any finding fails (pip-audit gives no severity). npm: high/critical in production deps fail.
Negative runs against the pre-HF2 manifests fail as they should (23 pip findings; npm: `next`
critical, `sharp`, `source-map-js`). CI: constraints check step, `dependency-audit` job, Node 22,
plain `npm ci`, `permissions: contents: read`, actions pinned to exact tags
(`checkout@v4.4.0`, `setup-python@v5.6.0`, `setup-node@v4.4.0`). `.github/dependabot.yml`: weekly;
pip, npm, github-actions, docker (x2); minor/patch grouped. `actionlint` clean; YAML parses.

## 4. Settings table
No application settings added or changed.

## 5. Deploy steps
1. Rebuild both images (backend now installs with `-c constraints.txt`; frontend uses Node 22).
2. No migration. 3. Existing admin sessions keep working (every minted token has `exp`).
4. To change a dependency: edit `requirements*.txt`, run `PYTHON=python3.11
   backend/scripts/constraints.sh regenerate`, commit both.

## 6. Rollback
Revert the PR. `DB_SESSION` is the only code-structure change; reverting it together with the
FastAPI pin is consistent. Do NOT revert only the pin's code part: on FastAPI >= 0.118 a plain
`Depends(get_db)` commits after the response (the drift guard fails in that case).

## 7. Existing tests changed
None changed or removed. 27 added (3 pin/guard, 16 JWT/multipart, 8 gate, 1 of them skipped when
`pip-audit` is absent).

## 8. Found, not fixed
* Dev-default `JWT_SIGNING_KEY` is 31 bytes, so PyJWT 2.15 warns in tests. Production already
  requires >= 32 characters (`_MIN_SECRET_LENGTH`).
* `pytest-asyncio` is used by no test (could be dropped).
* `app/dependencies.py` still exports an unused `get_db` (HF3 dead-code review).

## 9. UNVERIFIED / DEFERRED / DOCUMENTED LIMITATION
* UNVERIFIED: the actual GitHub Actions run, Dependabot opening PRs, `docker build` of either image
  (no Docker in the sandbox), Node 22 in the runtime image. Commands for the owner:
  `docker build -t acagent-backend backend`, `docker build -t acagent-frontend frontend`.
* DEFERRED: refactor `lib/auth-context.tsx` and `lib/use-admin-query.ts` so
  `react-hooks/set-state-in-effect` and `react-hooks/use-memo` (new in eslint-config-next 16) can be
  re-enabled; both rules are turned off in `eslint.config.mjs`, keeping the previous strictness.
* DOCUMENTED LIMITATION: the dev-only `braces` advisory (GHSA-vfj7-8cjw-p6xm; 5 "high" rows in a
  full `npm audit`, one root cause) has no patched release; listed in `SECURITY-EXCEPTIONS.md`,
  not in production dependencies.
* DOCUMENTED LIMITATION: audit results reflect the advisory databases on 2026-10-16.
* DOCUMENTED LIMITATION: the first full run was on Python 3.12.3; the final gates are on 3.11.17.
  In this sandbox the 3.11 interpreter had to be made traversable for the `postgres` OS user
  (`chmod o+x /root ...`) for the CP15B drills; not needed in CI.

## 10. OWNER DECISIONS
1. Two React-compiler ESLint rules disabled (see DEFERRED). 2. pip gate fails on ANY finding
(no severity available). 3. `braces` accepted as dev-only. 4. `starlette` declared in
`requirements.txt`. 5. Tokens without `exp` are now rejected.

## 11. Acceptance checklist
- [x] `pip-audit` on the pinned set: no findings
- [x] `npm audit --omit=dev --audit-level=high`: 0
- [x] `get_db` commit-timing tests pass before (0.115.0) and after (0.133.1) the upgrade
- [x] `npm ci` (no flags) succeeds; `tsc`, lint, tests, build green
- [x] JWT and multipart tests green; constraints check works; gate fails on seeded findings
- [x] ruff, mypy, `alembic check` clean; full pytest green on 3.11.17
- [ ] GitHub run of the new workflow (owner)
- [ ] Docker builds (owner)

## 12. Final gate numbers (Python 3.11.17)
`ruff` clean · `mypy` clean (182 files) · `alembic heads` = `e2d1c0b9a8f7`, `alembic check` clean ·
`pytest`: **1840 passed, 1 skipped, 0 failed** (includes all 9 CP15B drills) · `shellcheck
scripts/constraints.sh` clean · frontend: `npm ci` exit 0, `tsc` 0, lint 0, 23/23 tests, build 0.
