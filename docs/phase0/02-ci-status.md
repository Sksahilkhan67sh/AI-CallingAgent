# P0-2 CI status on main (HUMAN-TODO for run results)

## What could not be collected
`gh` is not installed and `GH_TOKEN` is not set [RAN: `which gh` empty, env check]. The unauthenticated
REST call `GET /repos/Sksahilkhan67sh/AI-CallingAgent/actions/runs?branch=main&per_page=10` returned
HTTP 403 "API rate limit exceeded" for the shared sandbox IP [RAN]. One approach per rule 7 plus a
retry; no token was borrowed from the shared text file (rule 2).
Run id / conclusion / date per job: **HUMAN-TODO**.

## Exact clicks
1. https://github.com/Sksahilkhan67sh/AI-CallingAgent > **Actions** > left list **CI**.
2. Filter: Branch = `main`. Open the newest 3 runs.
3. For each run write down: run number/id (URL tail), conclusion, date, and the result of the jobs
   `backend`, `frontend`, `dependency-audit`.
4. Paste the table below.

| Run id | Date | backend | frontend | dependency-audit |
|---|---|---|---|---|
| | | | | |

## Facts gathered from the repo
- Workflow: `.github/workflows/ci.yml` defines jobs `backend` (line 13), `frontend` (line 83), `dependency-audit` (line 112); triggers on push/PR to `main` and `develop` (lines 4-7); Python 3.11 at lines 50 and 120 [READ].
- HEAD of main is `8a1e013` "Merge pull request #57 from Sksahilkhan67sh/develop"; the commit below it, `9e3d13e`, is titled "CI: DIAGNOSTIC - surface pytest FAILED/ERROR lines as annotations (temporary)" [RAN: `git log --oneline`]. A temporary diagnostic commit on main suggests CI was failing at some point; reason unknown. [ASSUMED] Check it on the Actions tab.
- develop vs main: `git diff --stat origin/main origin/develop` is empty, so the **trees are identical** [RAN]. main is 12 merge commits ahead of develop and develop has 0 commits main lacks (`git rev-list --left-right --count origin/main...origin/develop` = `12 0`) [RAN]. So no content is waiting.
- HF2 "awaiting PR/merge": docs/PHASE1-PROGRESS.md:5 still says "Delivered, awaiting PR/merge" for `hf2-dependencies-audit-gates` [READ], but `origin/main` history contains "Merge pull request #56 from .../feature/hf2-dependencies-audit-gates" (5703d27) [RAN]. The progress doc is stale: HF2 is merged.
- PHASE1-PROGRESS.md:5 cites "pytest 1840 passed / 1 skipped (Py 3.11.17)" [READ]; the Phase 0 brief cites a 3.12 baseline of 1730 passed. Different numbers, see P0-3.
