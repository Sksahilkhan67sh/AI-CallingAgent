# P0-6 Decision C: single-tenant vs multi-tenant

Labels: [RAN] = produced by a command here; [READ] = read in code/docs (file:line). Paths relative to repo root.
The repo already records a past choice: "Option A: stay single-tenant, document tenant isolation as DEFERRED" (`docs/CHECKPOINT-11-NOTES.md:13-14`, §6 at :82-85) [READ].

## 1. Inventory

### (a) Tables with no tenant column [RAN]
Command: import `app.models`, iterate `Base.metadata.tables`, look for columns named `tenant_id`, `organization_id`, `org_id`, `account_id`, `owner_id`.
Result: **17 tables, 0 have such a column** (`docs/CHECKPOINT-11-NOTES.md:13` agrees: no `tenant_id` / `owner_id` / user table).
Tables (all in `backend/app/models/`): agent_config, analysis_budget_day, audit_log, call_analysis, call_attempt, call_event, campaign, contact, conversation_message, conversation_session, erasure_log, outbound_control, processed_event, retention_run, retry_policy, suppression, working_memory_snapshot.
Ownership chain [RAN, same metadata]: only `campaign` is a root with no parent link to anything; `campaign_id` appears on agent_config, call_analysis, contact, retry_policy; `contact_id` on call_analysis, call_attempt, suppression. Eight tables hang off no campaign/contact/attempt/session column: analysis_budget_day, audit_log, campaign, erasure_log, outbound_control, processed_event, retention_run, working_memory_snapshot (the last links through `attempt_id`, see `retention.py:146-147`). A tenant column would belong on `campaign` and propagate by FK, plus directly on the unlinked system tables.

### (b) Constraints that are global today [RAN from SQLAlchemy metadata; files in `backend/app/models/`]
- `suppression`: `uq_suppression_phone_number (phone_number)` and `uq_suppression_contact_id` (`suppression.py:30-31`). One global do-not-call list; the docs state it is never purged (`DATA-INVENTORY.md:28`). Two tenants would share one list and one number could only be suppressed once.
- `contact`: `uq_contact_campaign_phone (campaign_id, normalized_phone_number)`. Scoped per campaign, not global.
- `call_attempt`: `uq_call_attempt_contact_number (contact_id, attempt_number)`; unique index `uq_call_attempt_provider_call_id (provider_call_id)` is global (provider ids are unique across the Dograh account).
- `processed_event`: `event_id` unique, global (webhook de-duplication).
- `agent_config.campaign_id`, `retry_policy.campaign_id`, `conversation_session.call_attempt_id`, `call_analysis.call_attempt_id`: one-to-one, scoped through the campaign/attempt.
- `outbound_control`: system rows for the kill switch and provider halt (`kill_switch.py:35`, `provider_halt.py:75`), global by design: one switch halts all calling.
- Global config limits, not per customer: `daily_dial_cap` (`config.py:325`), `allowed_dial_regions` `["IN"]` and default timezone (`config.py:302-307`, comment "Single-tenant: one default region / timezone for the whole system"), calling window (`config.py:311-316`), one `DOGRAH_API_KEY` / trigger UUID (`telephony/factory.py:26-39`).
- Retention periods are per data class for the whole system (`config.py:550-555`).

### (c) Auth surface [READ]
- No user table. Two fixed identities from env: `admin_username/admin_password` and `operator_username/operator_password` (`backend/app/core/config.py:72-75`, comment at :66-71 "No user table/registration flow -- two fixed operator identities"). Roles are only `admin` and `operator` (`frontend/lib/admin-api.ts:43`; `CHECKPOINT-15A-NOTES.md:97`).
- JWT signed with one global key (`config.py:62-63`). Identity and role come only from the token; forged `tenant_id` / `owner_id` fields are ignored (`CHECKPOINT-11-NOTES.md:79`).
- Resource-level authorization: "none (single tenant)" for every list/detail endpoint (`CHECKPOINT-11-NOTES.md:59` and the protection matrix after :90, 36 routes). So any authenticated principal sees every campaign, contact, call and analysis.
- Login rate limit is per source IP (`CHECKPOINT-11-NOTES.md` matrix, login row); other limits are per principal per minute.

### (d) Redis keys that would need tenant namespacing [RAN grep over `backend/app`; not proven exhaustive]
**18 key names/patterns** found: a script checked 18 literal patterns and all 18 are present in `backend/app` (output "families found: 18"). Consumer groups (`dialer-workers`, `analysis-workers`: `config.py:93, 241`) are settings and are not counted.
| # | Key / pattern | Source | Tenant impact |
|---|---|---|---|
| 1 | stream `calls:outbound` | `config.py:92` | one dial queue for all |
| 2 | stream `analysis:jobs` | `config.py:240` | one analysis queue |
| 3 | `enqueued:{idempotency_key}` | `enqueue_service.py:52` | namespace unless keys are tenant-unique |
| 4-6 | `cps:global:{s}`, `cps:campaign:{id}:{s}`, `cps:provider:{name}:{s}` | `admission_controller.py:165-167` | add per-tenant bucket |
| 7-8 | `concurrency:lease`, `concurrency:holder` | `concurrency_lease.py:43-44` | global concurrency cap |
| 9-10 | `circuit:{provider}:errors`, `circuit:{provider}:open` | `circuit_breaker.py:28,31` | per provider account |
| 11-12 | `provider_halt:streak:category`, `provider_halt:streak:contacts` | `provider_halt.py:124-125` | global halt |
| 13 | `recovery:scheduled` (sorted set) | `recovery/scheduler.py:24` | one schedule |
| 14 | `ratelimit:*` | `rate_limit.py:73` | per principal already |
| 15 | `budget:audit:{day}:{reason}` | `spend_cap.py:213` | one daily budget |
| 16-17 | `heartbeat:worker`, `heartbeat:analysis_worker` | `system_service.py:19-20` | system-wide worker health |
| 18 | `conversation_memory:{attempt_id}` | `ai/memory/store.py:90` | attempt-scoped already |
Kill switch and provider halt state are DB rows (`outbound_control`), not Redis (`kill_switch.py:51`, `provider_halt.py:75`).

### (e) Frontend assumptions [READ]
- `frontend/app` has 11 `page.tsx` files [RAN `find`], all under `/admin`, `/login`: admin/, calls, calls/[id], contacts, contacts/[id], system, campaigns, campaigns/[id], analytics, login.
- Token kept in `sessionStorage` (`frontend/lib/admin-api.ts:34-35, 49-74`), sent as `Authorization: Bearer` (`:86`). The only role check in UI is `session?.role === "admin"` (`app/admin/campaigns/[id]/page.tsx:59`).
- No occurrence of tenant, organization or workspace in `app`, `lib`, `components` [RAN grep, empty]. No tenant switcher or tenant-scoped route would exist.

## 2. Options
### Option 1: stay single-tenant (documented, enforced)
What to document / enforce [recommendation items]:
1. State in README/ops docs: one deployment = one business entity; one Dograh account; shared suppression list.
2. Keep the production config guards; add a startup note that two businesses must use two deployments (separate DB, Redis, Dograh account).
3. Keep `DATA-INVENTORY.md` and the CP11 "not applicable" resource-check wording.
4. Change nothing in Phase 4 scope.
Cost: low. Risk: anyone with a login sees all data; acceptable only while there is one controller of the data.

### Option 2: add a tenant model
Ordered list of changes (each is a Phase 1+ migration/code change, not part of Phase 0):
1. Create `tenant` (and `user`) tables; replace the two env identities; token carries `tenant_id`.
2. Add `tenant_id` to `campaign`, propagate/validate through FKs; add directly to `suppression`, `audit_log`, `erasure_log`, `processed_event`, `outbound_control`, `retention_run`, `analysis_budget_day`.
3. Re-scope uniques: `suppression (tenant_id, phone_number)`, and decide if `processed_event.event_id` stays global.
4. Add a tenant filter at every repository/route (36 routes in the CP11 matrix) and tests for cross-tenant access (IDOR).
5. Namespace Redis keys in 3(d) and per-tenant caps (`daily_dial_cap`, concurrency, CPS, regions, windows).
6. Per-tenant Dograh credentials and webhook secret; per-tenant retention settings if laws differ.
7. Frontend: tenant-aware session, user management, role model beyond admin/operator.
Migration risk: backfilling `tenant_id` on live rows; suppression unique change on a never-purged table (data-loss risk if mishandled); every migration needs the backup/restore drills (`ops/backup/drill.sh`) re-run.
Which Phase 4 item it changes: the Phase 4 plan of this project is not in the repo [RAN grep for "Phase 4" finds only `docs/specs/Business/Project-Milestones.md:87-98` "Testing & Hardening"]. The owner must say which roadmap item this would replace; it would at least expand the Phase 4 security/IDOR test scope.

## 3. Recommendation (judgment; owner decides)
Option 1 for now. The code is deliberately single-tenant end to end (3(a)-(e)), nothing in the current requirements names a second customer, and Option 2 touches suppression, the never-purged table. Revisit only when a second business is real.

OWNER DECISION:
