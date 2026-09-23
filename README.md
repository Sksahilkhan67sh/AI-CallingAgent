# AI Calling Agent

Production-grade outbound AI calling platform: imports a contact list, places
calls through a telephony provider, runs a real-time speech-to-text →
LLM → text-to-speech conversation loop, recovers from mid-call disconnects,
and produces post-call lead analysis. Full specification lives in
[`docs/specs`](docs/specs).

## Status

**Checkpoint 05 — Recovery & Reconnection Engine.** Adds what happens
when a call disconnects mid-conversation or never connects: a single
`RecoveryManager` decides, deterministically, whether to retry (per the
existing `RetryPolicy` — max 2 retries, 30s/10min spacing) or
terminalize, schedules due retries on a Redis-backed durable delay
queue, and hands the actual redial to the *existing* Checkpoint 03
dialer — no second queue or dialer. A reconnected call restores the
previous attempt's structured working memory (not its transcript, kept
separate per Checkpoint 04's own rule) and continues the conversation.
Opt-out, suppression, a paused campaign, or a closed calling window all
correctly prevent or defer a retry without ever creating a duplicate
call. See `docs/CHECKPOINT-05-NOTES.md` for the full reasoning.

Earlier checkpoints: the real-time AI voice conversation engine
(Checkpoint 04), the durable outbound queue + dialer (Checkpoint 03),
contacts/campaigns CRUD + bulk import + campaign membership (Checkpoint
02), a hardened Postgres schema (Checkpoint 01A), and the
FastAPI/database foundation (Checkpoints 00-01). See
`docs/CHECKPOINT-0*-NOTES.md` for the reasoning behind schema and
scope decisions made along the way.

## Recovery architecture (Checkpoint 05)

```
Call disconnects mid-conversation (orchestrator.handle_disconnect)
      or a dial never connects (dialer worker's failure branch)
                        |
                 RecoveryManager
        (suppression/opt-out always wins first)
                        |
              read the existing RetryPolicy
           (per-reason rules, max_retries, spacing)
                 /                        \
         not retryable                  retryable
       or max attempts                       |
              |                    schedule on Redis sorted set
         terminalize              (recovery:scheduled, due-at score)
       (CallEvent + audit)                    |
                              worker loop periodically dispatches
                                due jobs onto the *existing*
                                calls:outbound stream
                                              |
                              the *existing* CP03 dialer worker
                              processes it exactly like any other
                              dial: admission control, provider
                              reconciliation, a brand-new CallAttempt
                                              |
                                    on connect: CP04's
                              start_conversation(..., is_reconnect=True,
                              previous_attempt_id=...) restores the
                              prior attempt's working memory
```

- **One decision owner**: `RecoveryManager` is the only place that
  reads `RetryPolicy` and decides retry vs. terminal — both the
  dialer's never-connected-failure path and the orchestrator's
  mid-call-disconnect path call into it, rather than each having their
  own logic.
- **Durable delay, not a timer**: a Redis sorted set
  (`recovery:scheduled`, score = due-at unix timestamp) — not
  `asyncio.sleep()`, not an in-memory list. `ZREM`'s return value is
  the atomic multi-worker claim.
- **Reuses the existing dialer entirely**: a due recovery job is just a
  `DialJob` (with `recovery_type`/`previous_attempt_id` set) pushed onto
  the same `calls:outbound` stream — there is no second dialer.
- **Paused campaign / closed calling window**: neither terminalizes a
  pending retry — the dispatcher reschedules it (to a short recheck
  interval, or to the window's reopening time) rather than dropping it.


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
