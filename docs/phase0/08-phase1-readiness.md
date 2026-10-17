# P0-8 Phase 1 readiness checklist

Status values come from `docs/phase0/STATE.md` at the time of writing. "OWNER ACTION" means only the owner can do it.

## 1. Phase 0 gates
| Gate | Status | Evidence | What closes it |
|---|---|---|---|
| GitHub token revoked, replacement created, security log reviewed | OWNER ACTION (not confirmed) | `01-token-rotation.md` | Owner fills the confirmation line in file 01 |
| CI green on Python 3.11 (local run) | DONE: 1730 passed, 1 skipped, ruff/mypy/alembic clean; tests/ops 110 passed | `03-python311-verification.md` | none |
| CI green on main (GitHub run results) | OWNER ACTION | `02-ci-status.md` | Owner pastes run ids and conclusions |
| Docker builds (backend, frontend) | OWNER ACTION (not run: no Docker in the sandbox) | `03-python311-verification.md` | Owner runs `docker build ./backend` and `./frontend` |
| Decision A (Dograh-only vs native) | Document complete; owner decision blank | `04-decision-A.md` | Owner fills `OWNER DECISION:` |
| Decision B (retention periods) | Document complete; owner decision blank; legal input needed | `05-decision-B.md` | Owner fills `OWNER DECISION:` after legal answers |
| Decision C (single vs multi tenant) | Document complete; owner decision blank | `06-decision-C.md` | Owner fills `OWNER DECISION:` |
| Legal brief sent | Drafted; not sent | `07-legal-brief.md` | Owner sends it and fills the date line |

Phase 1 may start only when every row above reads closed. As of this writing none of the owner rows is closed.

## 2. Findings that change Phase 1 scope (from `FINDINGS.md`)
| Finding | Effect on Phase 1 |
|---|---|
| F5: `ci.yml` does not install `age`, so the ops/backup safety tests (54 skipped in a clean environment) likely skip in CI | Add the missing tool to the CI job so data-loss drills actually run; this directly serves Phase 1 (data-loss fixes) |
| F6: `ops/backup/drill.sh` exits 126 when run as root and the `postgres` OS user cannot read the repo/python | Document or harden the drill invocation for container/root runs |
| F8: Dograh webhook stores transcripts in `conversation_message`; Dograh keeps its own copies that cannot be deleted from this repo | Erasure cannot be complete for Dograh-held data; depends on legal brief questions 11 and 13 |
| F9: empty `RETENTION_*` env values crash startup | Document in deploy notes; consider a clearer error message |
| F2 and F3: progress doc says HF2 awaits merge though PR #56 is merged; a "temporary" CI DIAGNOSTIC commit is in main | Refresh `PHASE1-PROGRESS.md`; check whether the diagnostic step is still active in `ci.yml` |
| F10: no resource-level authorization; any login sees all data | No change if Decision C is single-tenant; otherwise large scope increase |
| F11: no registry scrubbing, AI-disclosure or breach-notification code | Scope depends on legal answers; do not build before them |

## 3. Not verified in Phase 0
- Frontend install, build and tests were not run.
- Live Dograh behaviour (see `04-decision-A.md` section 1.4).
- GitHub Actions history for main.
