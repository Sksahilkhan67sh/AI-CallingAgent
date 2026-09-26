# Checkpoint 07 — Admin Dashboard — implementation notes

## 1. Dashboard architecture

```
Browser
  -> Next.js Admin Dashboard (App Router, client components)
  -> lib/admin-api.ts (typed fetch client, auth header, error mapping)
  -> FastAPI /api/v1/admin/*
  -> app/services/admin/* (read-only aggregates + thin wrappers over
     existing services)
  -> Existing services/repositories (CampaignService, ContactService,
     CallAnalysisRepository, ...) / PostgreSQL / Redis
```

The frontend never touches Postgres, Redis, or any provider directly --
every page goes through the typed API client to FastAPI.

**Deliberate scope decision, documented here for the same reason as
the CP05/CP06 write-ups above it:** this checkpoint found that no
authentication existed anywhere in the backend (`jwt_signing_key` was
reserved-but-unused config from Checkpoint 00). Rather than retrofitting
auth onto the existing public `/api/v1/contacts`, `/campaigns`,
`/call-attempts/{id}/analysis` endpoints -- which would have required
touching all 228 pre-existing tests and any other undocumented consumer
of those routes -- the dashboard is served entirely from a **new**
`/api/v1/admin/*` surface (`app/api/routes/admin_*.py`,
`app/services/admin/*.py`). Every admin route either wraps the existing
service layer directly (`CampaignService`, `ContactService`) or adds a
genuinely new read-only capability that didn't exist before
(`CallAttempt` list/detail, dashboard aggregates, analytics, system
health). No business logic is duplicated; a new authorization boundary
is added around it.

## 2. Frontend routes

```
/                       -> redirects to /admin
/login                  -> unauthenticated
/admin                  -> Overview (dashboard home)
/admin/campaigns        -> list, filter by status, paginated
/admin/campaigns/[id]   -> metrics + lifecycle controls (admin role only)
/admin/contacts         -> list, filter by status, paginated
/admin/contacts/[id]    -> detail + call history
/admin/calls            -> list, filter by state/analysis status, paginated
/admin/calls/[id]       -> metadata, recovery history, transcript, AI analysis
/admin/analytics        -> time-ranged aggregate metrics
/admin/system           -> component health, queue depth (polled every 15s)
```

## 3. Backend API endpoints (all new, under `/api/v1/admin/`)

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/auth/login` | POST | none | issues a JWT |
| `/dashboard/overview` | GET | any role | call/campaign/intelligence/reliability aggregates |
| `/dashboard/analytics` | GET | any role | time-ranged, campaign-filterable metrics |
| `/dashboard/system` | GET | any role | Postgres/Redis/queue/worker health |
| `/campaigns`, `/campaigns/{id}` | GET | any role | list/detail + metrics |
| `/campaigns/{id}/status` | POST | **admin only** | lifecycle transition, thin pass-through to `CampaignService.update_campaign` |
| `/contacts`, `/contacts/{id}` | GET | any role | list/detail, phone always masked |
| `/call-attempts`, `/call-attempts/{id}` | GET | any role | new capability: list/detail with transcript, analysis, recovery events |

All GET endpoints are read-only monitoring surfaces (§5: OPERATOR
covers everything ADMIN can read; only the campaign status-transition
control is ADMIN-only, since it's the one operational control this
checkpoint exposes).

## 4. Authentication & authorization

No user table, no registration/password-reset flow. Two fixed
identities configured via `Settings` (`admin_username`/`admin_password`,
`operator_username`/`operator_password`), following the exact
"env-configured secret, dev-only-insecure default" convention this
repository already uses for `telephony_webhook_secret`. Constant-time
comparison (`secrets.compare_digest`), same approach the webhook check
already uses.

JWTs are signed with `jwt_signing_key` (existed in `Settings` since
Checkpoint 00, was never used until this checkpoint) via `PyJWT`
(newly added dependency). `app/api/admin_deps.py::require_admin`
decodes and validates the token server-side on every protected route;
`require_role("admin")` additionally checks the role claim. **The role
claim is set once, server-side, at login time from the matched
identity -- never trusted from anything the client sends afterward**
(§4).

The Next.js side (`lib/auth-context.tsx`) keeps the token + role +
username in `sessionStorage` (cleared when the tab closes) and gates
`/admin/*` client-side via a redirect-to-`/login` check in
`app/admin/layout.tsx`. **This is a convenience, not the security
boundary** -- a user who bypasses it simply gets 401s from every admin
API call, which is independently enforced server-side regardless of
what the frontend does or shows.

## 5. Authorization boundary / IDOR

Every admin route requires a valid bearer token; there is no
tenant/multi-org model anywhere in this repository (a pre-existing gap,
not new to this checkpoint), so authorization stops at
"authenticated admin/operator" rather than per-resource ownership.
Changing a call/contact/campaign ID in a detail URL returns that
resource's own data or a 404 -- never another resource's data mixed in
-- verified directly in
`tests/test_admin_call_attempts.py::test_changing_id_in_url_does_not_leak_a_different_calls_data`.

## 6. PII handling

Phone numbers are never returned in full by any admin endpoint --
`app/services/phone.py::mask_phone_number` (e.g. `+1******1234`) is
applied in every contact/call-attempt list and detail response. The
full `normalized_phone_number` column is never serialized into an
admin schema.

## 7. Analytics definitions

Documented in `app/services/admin/analytics_service.py`'s module
docstring, matching the checkpoint's own §24 wording:

- **Connection rate**: attempts that reached `CONNECTED` at some point
  (final state `ENDED_NORMALLY` or `DROPPED_MID_CALL`) / total dial
  attempts.
- **Completion rate**: attempts ending `ENDED_NORMALLY` / total dial
  attempts.
- **Retry rate**: `DROPPED_MID_CALL` attempts / connected attempts --
  same definition CP05's own eligibility model uses.
- **Opt-out rate**: contacts closed (`ContactStatus.CLOSED`) in range /
  total dial attempts. This is an **approximation** -- opt-out isn't
  tracked as its own `CallAttempt` state (see the CP06 notes above on
  why `Closed` covers both never-connected exhaustion and opt-out), so
  this can't be perfectly isolated from the rare "suppressed, contact
  status never updated" CP05 edge case documented in the CP06 notes.
  Called out here rather than silently treated as exact.
- **Analysis completion rate**: `COMPLETED` analyses / all analysis
  jobs created in range.
- **Interest rate**: `INTERESTED` analyses / all `COMPLETED` analyses
  in range.

Every count is explicitly attempt-scoped or analysis-scoped -- never a
bare contact count substituted for either (§45). Verified directly:
`tests/test_admin_dashboard.py::test_a_contact_with_two_attempts_counts_as_one_contact_two_attempts`
seeds one Contact with two CallAttempts (one `FailedToConnect`, one
`EndedNormally`) and asserts `calls.total == 1` while
`analytics.total_dial_attempts == 2`.

## 8. Data fetching, pagination, search/filtering

All aggregates are computed server-side with `GROUP BY`/`func.count`/
`func.avg` in a small, fixed number of queries per endpoint -- never by
paginating through and summing in the browser (§10). All list endpoints
(`campaigns`, `contacts`, `call-attempts`) are server-side
paginated/filtered via query parameters; filter and page state is
persisted in the URL (`useSearchParams`/`router.push`) so refresh and
browser back/forward both work (§32). No debounced free-text search
was added in this pass (see Known limitations) -- filtering is by
status/campaign/state dropdowns only.

## 9. Real-time / polling strategy

No WebSocket/SSE/Socket.IO infrastructure exists anywhere in this
repository. The system page polls `/dashboard/system` every 15 seconds
(`app/admin/system/page.tsx`); every other page fetches once on load
and offers a manual retry on error. This is a documented limitation,
not a real-time dashboard -- see below.

## 10. Worker heartbeats

`app/worker.py` and `app/analysis_worker.py` each now write a
timestamped Redis key (`heartbeat:worker`, `heartbeat:analysis_worker`,
TTL `worker_heartbeat_ttl_seconds` = 30s) once per loop iteration. This
is a best-effort liveness signal, not a distributed-systems-grade health
check: a missing/expired key means "no worker touched this key
recently", not a proven crash. The System page renders this as
`unknown` (not `degraded`) when no heartbeat has ever been observed,
which is exactly what a live 255-test run against a real server showed
(no worker process was running during that check).

## 11. Error handling

Every list/detail page handles loading (skeleton matching the final
table shape, `components/admin/States.tsx::TableSkeleton`), empty
(`EmptyState`, plain text, no illustrations), and error
(`ErrorState`, calm message + retry button) states explicitly. The API
client (`lib/admin-api.ts`) maps HTTP status to a fixed set of safe
messages (`ApiError`) -- a raw backend exception message is never
surfaced to the UI (§27, §47), verified in
`__tests__/login.test.tsx`.

## 12. Security review

- No secrets in any `NEXT_PUBLIC_*` variable -- only the API base URL.
- JWT stored in `sessionStorage`, not `localStorage`; cleared on 401 and
  on explicit sign-out. This is a documented trade-off for this
  checkpoint's scope (browser-only admin tool, no XSS-exposed
  user-generated content anywhere in the dashboard), not a full
  session-security design (no httpOnly cookie, no refresh-token
  rotation).
- IDOR tested explicitly (see §5 above).
- Phone numbers masked everywhere (§6).
- CORS restricted to `admin_cors_origins` (default
  `http://localhost:3000`), not `*`.
- No stack traces, SQL errors, or provider credentials ever reach a
  frontend response (`app/core/errors.py`'s existing handlers +
  `ApiError`'s fixed message mapping).
- No campaign/contact/call-attempt data flows through a component that
  renders raw HTML from user/AI-generated content (React's default
  escaping is never bypassed with `dangerouslySetInnerHTML` anywhere in
  this checkpoint's code).

## 13. Performance verification

Seeded locally: 10 campaigns, 1,000 contacts, 2,958 call attempts,
1,000 completed analyses (`random`-distributed statuses/outcomes).
Measured against a live `uvicorn` instance with real Postgres 16 +
Redis 7 (not the pytest `TestClient`):

| Endpoint | Response time |
|---|---|
| `GET /dashboard/overview` | ~45ms |
| `GET /dashboard/analytics?range=30d` | ~27ms |
| `GET /call-attempts?limit=50&offset=0` | ~20ms |
| `GET /call-attempts?limit=50&offset=2900` (deep page) | ~16ms |
| `GET /contacts?limit=50` | ~7ms |
| `GET /campaigns` | ~5ms |

No query downloads a full table -- every list is `LIMIT`/`OFFSET`
bounded, every aggregate is a fixed small number of `GROUP BY` queries
independent of row count. This is short of the suggested 5,000-attempt
minimum (2,958 achieved) and far short of the optional 10K+ stretch
target -- not claimed as tested at that scale.

## 14. Accessibility

Semantic `<table>`/`<dl>`/`<button>`/`<label htmlFor>` throughout (no
`<div>` pretending to be a button or table). Status is never
color-only -- `StatusDot` always renders the text label alongside the
colored dot (§33). Focus-visible outlines defined on all interactive
elements (`login/page.module.css`). `prefers-reduced-motion` respected
globally (`app/globals.css`). Not run through an automated a11y audit
tool (e.g. axe) in this pass -- see Known limitations.

## 15. Design system

No design system, Tailwind, or component library existed in this
repository before this checkpoint -- the frontend was an untouched
`create-next-app` scaffold. Built from scratch as plain CSS custom
properties (`app/globals.css`) + CSS Modules per component (zero new
styling dependencies). Reused the existing Geist font already wired up
via `next/font/local` in the root layout rather than introducing a new
font. Restrained palette (warm neutral surfaces, one accent color,
semantic status colors only), 4-8px border radii, borders over shadows,
tables as the primary list primitive, no card-per-metric grids -- see
the design brief this checkpoint was given for the full rationale.

## 16. Testing

Backend: 27 new tests (`tests/test_admin_*.py`) -- login success/failure,
token validation (missing/malformed/wrong-scheme), role enforcement
(admin-only campaign status transition), aggregate correctness
including the explicit contact != attempt fixture, IDOR, 404 handling,
phone masking, suppression indicator, pagination. 255 total passing
(228 pre-existing + 27 new). `ruff check .` clean. `mypy app/` clean
(125 files).

Frontend: 23 tests total (21 new + 2 pre-existing) across
`__tests__/admin-components.test.tsx` (StatusDot, loading/empty/error
states, pagination boundary behavior, metrics), `__tests__/call-detail-
components.test.tsx` (transcript ordering/immutability/empty state,
analysis panel including the "never labeled guaranteed/certain" lead-
score check and the pending-state no-fabricated-fields check), and
`__tests__/login.test.tsx` (successful login + redirect, failed login
with a safe error message and no redirect). `eslint .` clean,
`tsc --noEmit` clean, `next build` succeeds for all 11 routes.

## 17. Known limitations

- **No tenant/authorization model** exists anywhere in this repository
  (pre-existing, not new to this checkpoint) -- authorization is
  "authenticated admin/operator", not per-resource ownership.
- **No real-time transport** (WebSocket/SSE) exists in the backend;
  the System page polls every 15s, every other page is fetch-once +
  manual retry. A live-call-count / "call connected" event stream is
  not implemented.
- **No free-text search** (contact/campaign/call search) was added in
  this pass -- only status/state/campaign dropdown filters. Search was
  explicitly requested (§31) but scoped out here given the size of the
  rest of this checkpoint; the pagination/filter/URL-state
  infrastructure it would need already exists.
- **No recording playback** -- no recording/media storage exists
  anywhere in this repository yet (confirmed also in the CP06 notes),
  so the call detail page has no recording section at all rather than
  a placeholder for one.
- **JWT in `sessionStorage`**, not an httpOnly cookie -- a deliberate,
  documented trade-off for this checkpoint's scope (see §12), not a
  full session-security design.
- **Frontend package.json pre-existing dependency conflict**: `npm ci`
  fails on a `vitest`/`@types/node` peer-dependency mismatch that
  predates this checkpoint (the scaffold's own `vitest@^5.0.1` wants
  `@types/node@^22`, the scaffold pins `@types/node@^20`);
  `npm install --legacy-peer-deps` is required. Not fixed here as it's
  outside this checkpoint's scope, but documented since it blocks a
  plain `npm ci`.
- **No automated accessibility audit** (e.g. axe-core) was run; manual
  semantic-HTML/focus/color-contrast review only.
- **Opt-out rate is an approximation**, not an exact metric -- see §7
  above.
- Performance verified at ~3,000 call attempts, not the full 5,000+/
  10,000+ suggested scale (§13 above).

## 18. Deferred (explicitly out of scope per this checkpoint's own
    instructions)

CRM features (deal pipelines, lead assignment), billing, global
production hardening (rate limiting, secrets manager, autoscaling,
disaster recovery, kill switch), and a full campaign-analytics/charting
UI are all CP08/future-checkpoint scope and were not started here.
