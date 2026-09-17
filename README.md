# AI Calling Agent

Production-grade outbound AI calling platform: imports a contact list, places
calls through a telephony provider, runs a real-time speech-to-text →
LLM → text-to-speech conversation loop, recovers from mid-call disconnects,
and produces post-call lead analysis. Full specification lives in
[`docs/specs`](docs/specs).

## Status

**Checkpoint 01 — Backend foundation.** Adds PostgreSQL + Alembic
migrations, the core domain schema (campaign, contact, call attempt,
retry policy, suppression, audit log, processed-event idempotency, and a
conversation-persistence foundation), and the first real API endpoints
(`contacts`, `campaigns`) on top of the Checkpoint 00 skeleton. No
queue/dialer, telephony, LiveKit, STT/LLM/TTS, retry execution, post-call
analysis, or dashboard yet — see `docs/specs` for what's coming, and
`docs/CHECKPOINT-01-NOTES.md` for schema decisions made in this
checkpoint.

## Structure

```
backend/    FastAPI service — app/, tests/
frontend/   Next.js app — app/, components/, lib/, __tests__/
docs/specs/ Full product, architecture, AI, security, and ops specification
.github/    CI workflows
```

## Local development

### Backend

Requires PostgreSQL running locally (see `docker compose up db`, or point
`PRIMARY_DB_URL` at your own instance).

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload
# http://localhost:8000/health
```

Tests run against a real Postgres database (default: `ai_calling_agent_test`,
create it once with `createdb ai_calling_agent_test` and `alembic upgrade head`
against it) -- never SQLite, so Postgres-specific behavior (JSONB, native
enums, CHECK constraints) is actually exercised.

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

Starts the backend on :8000 and frontend on :3000.

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
