# AI Calling Agent

Production-grade outbound AI calling platform: imports a contact list, places
calls through a telephony provider, runs a real-time speech-to-text →
LLM → text-to-speech conversation loop, recovers from mid-call disconnects,
and produces post-call lead analysis. Full specification lives in
[`docs/specs`](docs/specs).

## Status

**Checkpoint 04 — Real-Time AI Voice Conversation Engine.** Adds the
conversation layer that runs once a call connects: streaming STT ->
conversation orchestrator -> structured LLM output -> deterministic
policy engine -> TTS -> audio, with turn-by-turn transcript
checkpointing, structured working memory (distinct from the
transcript), bounded context, interruption/stale-response protection,
opt-out detection feeding the canonical suppression table, and bounded
failure handling throughout. STT/LLM/TTS/audio-gateway providers are
mock-only for now (see `docs/CHECKPOINT-04-NOTES.md`). No disconnect/
reconnect recovery, retry scheduling, post-call analysis, lead scoring,
or dashboard yet — those are later checkpoints.

Earlier checkpoints: the durable outbound queue + dialer (Checkpoint
03), contacts/campaigns CRUD + bulk import + campaign membership
(Checkpoint 02), a hardened Postgres schema (Checkpoint 01A), and the
FastAPI/database foundation (Checkpoints 00-01). See
`docs/CHECKPOINT-0*-NOTES.md` for the reasoning behind schema and
scope decisions made along the way.

## AI conversation architecture (Checkpoint 04)

```
Call Connected (dialer worker)
        |
 start_conversation() -- internal only, not a public endpoint
        |
  ConversationSession created, audio + STT sessions start
        |
Customer audio -> STT (finalized utterances only) -> ConversationOrchestrator
        |
  load/update WorkingMemory (structured state, NOT the transcript)
        |
  bounded context (recent turns + memory + campaign system prompt)
        |
        LLM -> StructuredOutput (intent, entities, next_action, response_text)
        |
  PolicyEngine: response validation, phase routing, opt-out backstop,
  termination rules -- all deterministic, the LLM never decides these
        |
       TTS -> audio output (discarded if a barge-in made this turn stale)
        |
  checkpoint memory + transcript message to PostgreSQL
        |
  loop, or end conversation (opt-out / goal met / max turns / max
  duration / unrecoverable failure) -- ends update CallAttempt +
  Contact + ConversationSession and log a CallEvent
```

- **Memory vs. transcript**: `ConversationMessage` (existing, Checkpoint
  01) is the transcript -- what was said, in order. `WorkingMemory` /
  `working_memory_snapshot` (new) is compact structured state -- what
  the agent needs to continue (captured entities, script progress,
  objections, last utterance). Memory is never the transcript restated.
- **Stale-response protection**: every turn has a generation number; a
  barge-in bumps it, and any LLM/TTS output computed under a stale
  generation is discarded before it reaches audio output.
- **Opt-out**: detected two ways -- the LLM's own structured output
  (`requires_suppression`) and an independent deterministic keyword
  check on the raw utterance (Step 17's "LLM must not be the sole
  enforcement mechanism") -- either one ends the call and writes to the
  one canonical `suppression` table.
- **Session ownership**: `conversation_session.call_attempt_id` is
  unique at the database level, so only one orchestrator can ever hold
  an active session for a given call -- a real DB constraint, not an
  in-memory lock.
- **Providers**: `STT_PROVIDER` / `LLM_PROVIDER` / `TTS_PROVIDER` /
  `AUDIO_GATEWAY_PROVIDER` -- `mock` is the only supported value until
  real credentials exist.


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
