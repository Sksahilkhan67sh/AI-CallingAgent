# AI Calling Agent

Production-grade outbound AI calling platform: imports a contact list, places
calls through a telephony provider, runs a real-time speech-to-text →
LLM → text-to-speech conversation loop, recovers from mid-call disconnects,
and produces post-call lead analysis. Full specification lives in
[`docs/specs`](docs/specs).

## Status

**Checkpoint 03 — Queue + Dialer.** Adds the durable outbound calling
pipeline: eligible contacts are enqueued onto a Redis Streams queue,
admission-controlled (CPS + concurrency, global/campaign/provider) and
picked up by a calling worker that dials through a telephony provider
abstraction (mock only for now — see `docs/CHECKPOINT-03-NOTES.md`),
persists the outcome to PostgreSQL, and acknowledges the job only after
that persistence succeeds. No real-time AI conversation, STT/LLM/TTS,
retry execution, post-call analysis, or dashboard yet.

Earlier checkpoints: contacts/campaigns CRUD + bulk import + campaign
membership (Checkpoint 02), a hardened Postgres schema (Checkpoint 01A),
and the FastAPI/database foundation (Checkpoints 00-01). See
`docs/CHECKPOINT-0*-NOTES.md` for the reasoning behind schema and
scope decisions made along the way.

## Queue architecture (Checkpoint 03)

```
Eligible Contact -> enqueue -> Redis Stream (calls:outbound)
                                       |
                                consumer group (dialer-workers)
                                       |
                                 Calling Worker
                                       |
                   Admission Controller (circuit breaker -> concurrency -> CPS)
                                       |
                       Final eligibility re-check (DB state)
                                       |
                    Claim/create CallAttempt (idempotent, DB-constraint-backed)
                                       |
                           Telephony Provider adapter
                                       |
                        Persist outcome -> release slot -> ACK
```

- **Queue**: one Redis Stream + one consumer group. At-least-once
  delivery (`XREADGROUP`/`XACK`); a crashed worker's unacked job is
  reclaimed via `XAUTOCLAIM` after `QUEUE_RECLAIM_IDLE_MS`.
- **Idempotency**: `campaign_id + contact_id + attempt_number`, enforced
  by a PostgreSQL unique constraint (the final authority — Redis/queue
  logic is a fast path, never the source of truth for "did this call
  happen").
- **Admission control**: circuit breaker (per provider) → concurrency
  reservation (global/campaign/provider) → CPS (global/campaign/
  provider), all Redis-backed so they work correctly across multiple
  worker processes. Configured via `Settings`, not a DB column — see
  `docs/CHECKPOINT-03-NOTES.md`.
- **Provider abstraction**: `app/services/telephony/base.py` defines
  the contract; `MockTelephonyProvider` is the only implementation
  until real provider credentials exist.
- **Worker lifecycle**: `python -m app.worker` — connects, loops
  (claim → admit → dial → persist → ack), and shuts down gracefully on
  SIGTERM/SIGINT (finishes the in-flight job, doesn't drop it).

## Structure

```
backend/    FastAPI service — app/, tests/
frontend/   Next.js app — app/, components/, lib/, __tests__/
docs/specs/ Full product, architecture, AI, security, and ops specification
.github/    CI workflows
```

## Local development

### Backend

Requires PostgreSQL and Redis running locally (see `docker compose up db
redis`, or point `PRIMARY_DB_URL`/`REDIS_URL` at your own instances).

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
# http://localhost:8000/health
```

Run the calling worker (separate process, same environment):

```bash
python -m app.worker
```

Tests run against a real Postgres database (default: `ai_calling_agent_test`,
create it once with `createdb ai_calling_agent_test` and `alembic upgrade head`
against it) and a real Redis database (index 1, separate from the dev
default of index 0) -- never SQLite or a fake in-memory queue, so
Postgres- and Redis-specific behavior is actually exercised.

Tests: `pytest` · Lint: `ruff check .` · Types: `mypy app` · Migrations: `alembic revision --autogenerate -m "..."` / `alembic upgrade head`

### Frontend

```bash
cd frontend
npm install
cp .env.example .env.local
npm run dev
# http://localhost:3000
```

Tests: `npm test` · Lint: `npm run lint` · Types: `npx tsc --noEmit` · Build: `npm run build`

### Docker

```bash
docker compose up --build
```

Starts Postgres, Redis, the backend on :8000, a calling worker, and the
frontend on :3000.

## Configuration

Infrastructure configuration (secrets, connection strings, provider keys) is
read from environment variables — see `backend/.env.example` and
`docs/specs/Deployment/Environment-Config.md` for the full reference. Business
configuration (retry policy, agent script) belongs in the database once that
layer exists, per the same document — it is intentionally not environment
config.

## Contributing / workflow

This repository follows a strict checkpoint workflow (see project
instructions): `main` ← `develop` ← `feature/checkpoint-NN-*`. Every
checkpoint ships as a pull request against `develop` for manual review and
merge — agents do not merge their own PRs or push to `main`.
