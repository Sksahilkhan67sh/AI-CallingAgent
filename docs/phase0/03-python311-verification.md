# P0-3 Python 3.11 verification

Environment (sandbox, not the owner's machine) [RAN]:
- `python --version` -> Python 3.11.15 (uv-managed, venv `/home/claude/venv311`)
- `psql --version` -> psql (PostgreSQL) 16.15 ; server `select version()` -> PostgreSQL 16.15 (Ubuntu)
- `redis-server --version` -> Redis server v=7.0.15
- Docker: `which docker` empty -> not installed.
- Branch/commit tested: `main` @ 8a1e013 (clean checkout, no code changes).

## Commands and results [RAN]
Deviation: dependencies installed with `uv pip install -r requirements-dev.txt -c constraints.txt` (same files, uv instead of pip). Exit 0.
Env: `PRIMARY_DB_URL=postgresql+psycopg://postgres:postgres@localhost:5432/ai_calling_agent_test`, `REDIS_URL=redis://localhost:6379/1`.

| Command (in backend/) | Result |
|---|---|
| `alembic upgrade head` | exit 0 (ran every migration up to head `e2d1c0b9a8f7`, HF1); re-run exit 0 |
| `alembic check` | "No new upgrade operations detected." exit 0 |
| `ruff check .` | "All checks passed!" exit 0 |
| `mypy app` | "Success: no issues found in 182 source files" exit 0 |
| `pytest -q -p no:cacheprovider --ignore=tests/ops` | **1730 passed, 1 skipped**, 1112 warnings, 125.65 s, exit 0 |
| `pytest -q -p no:cacheprovider tests/ops` (first run, no tools) | 56 passed, **54 skipped**: `age is not installed` (43), `shellcheck is not installed` (11) |
| same, after `apt-get install age shellcheck` (age 1.1.1, shellcheck 0.9.0) | 100 passed, **10 failed** (drills A-E, G-J + summary test): `drill X did not run` with empty output |
| same, after granting the `postgres` OS user read/traverse access to /root, /home/claude | **110 passed, 0 skipped**, 97.81 s |

Why the 10 failures happened (environment, not product code): `ops/backup/drill.sh` re-executes itself as the `postgres`
OS user via `runuser` (shown by `bash -x`, line `exec runuser -m -u postgres -- ...`) when run as root; that user could not
read the root-owned Python install and repo, so the script exited 126. Fixed by `chmod o+rX` in the sandbox only. [RAN]
The 54 skips come from missing tools; they were resolved by installing the tools. Neither required an application change.

## Skip in the main suite [READ]
`backend/tests/test_hf2_audit_gate.py:113-116` skips `test_gate_runs_end_to_end_against_the_real_constraints_file` when `pip-audit` is not on PATH.

## Diff against the baseline
| | 3.12 baseline (brief) | 3.11.15 (this run) | Delta |
|---|---|---|---|
| passed (tests/ops excluded) | 1730 | 1730 | 0 |
| skipped (tests/ops excluded) | 1 | 1 | 0 |
| failed | 0 | 0 | 0 |
| tests/ops | not in baseline | 110 passed (after tooling) | n/a |

Note: 1730 + 110 = 1840, which matches "pytest 1840 passed / 1 skipped (Py 3.11.17)" in docs/PHASE1-PROGRESS.md:5 [READ]. So that figure is
the full run including tests/ops; no discrepancy.

## CI relevance [READ]
`.github/workflows/ci.yml` runs `pytest -q -rfE` from backend/ (no `--ignore=tests/ops`) and has no step installing `age`.
So on a runner without `age`, tests/ops drill tests skip silently. [ASSUMED] ubuntu-latest has no `age`; confirm in the Actions log (see P0-2).

## Docker builds: BLOCKED
`docker build ./backend` and `docker build ./frontend` not run: Docker is not installed in the sandbox and the sandbox network does not allow Docker registries.
Approach tried: `which docker` (not found). No second approach was attempted because no container daemon can run in this sandbox [ASSUMED].
Facts only [READ]: backend/Dockerfile:1 `FROM python:3.11-slim`; frontend/Dockerfile:1 and :8 `FROM node:22-slim`.
Frontend (npm ci/build/test) was not run in P0-3 either: not in the task list.
HUMAN-TODO: owner runs both `docker build` commands locally.
