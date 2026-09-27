# Checkpoint 08 — Dograh Integration — implementation notes

## What this checkpoint does and does not do

Per the request: **replace Telephony, STT, LLM, and TTS, and the
inbound call-status webhook, with [Dograh](https://www.dograh.com/)**
(a separate, self-hosted, open-source voice-agent platform —
[github.com/dograh-hq/dograh](https://github.com/dograh-hq/dograh)).

Dograh is not a narrow API for "just STT" or "just TTS" — it's a
complete voice-agent runtime with its own workflow builder, telephony
connectivity, and model orchestration. That means for a
`calling_engine="dograh"` call, **all four of those things now happen
entirely inside Dograh**, invisibly to this codebase — this repo never
calls an STT/LLM/TTS provider itself for that call. What this
checkpoint actually builds, inside *this* repository, is the two-way
integration surface:

1. **Trigger**: `app/services/queue/dialer_worker.py` calls Dograh's
   public API Trigger endpoint instead of running our own
   `TelephonyProvider` + `ConversationOrchestrator` (Checkpoints 03/04).
2. **Result**: a new webhook Dograh calls back into
   (`app/api/routes/dograh_webhook.py`) once the call ends, which is
   wired into the *same* CP05 recovery and CP06 analysis machinery a
   native call would have used.

Everything else — campaign/contact management (CP01-02), the retry
decision logic itself (CP05), post-call analysis (CP06), and the admin
dashboard (CP07) — is unchanged and reused as-is.

**What this checkpoint cannot do for you**: deploy Dograh itself,
configure its own telephony/LLM credentials, or design/build the
actual conversation workflow. Those live entirely in Dograh's own UI
and database, which this repository has no access to. See "Manual
setup checklist" below.

## Architecture

```
Existing (native, calling_engine="native", unchanged):
  dialer_worker -> TelephonyProvider.create_outbound_call()
                -> ConversationOrchestrator (our own STT/LLM/TTS loop)
                -> app/api/routes/webhooks.py (Twilio-style call-status)

New (calling_engine="dograh"):
  dialer_worker -> DograhClient.trigger_call()
                   POST {DOGRAH_API_BASE_URL}/api/v1/public/agent/test/{uuid}
                   { phone_number, initial_context: { call_attempt_id, ... } }
                -> [Dograh runs the entire call: telephony + STT + LLM + TTS]
                -> Dograh's own Webhook node
                   POST {this app}/api/v1/webhooks/dograh/call-completed
                   { call_attempt_id (echoed back), call_status, ... }
                -> app/services/telephony/dograh_webhook_service.py
                   -> ended normally  -> enqueue_call_analysis (CP06, reused)
                   -> dropped mid-call -> RecoveryManager.handle_disconnect (CP05, reused)
```

`calling_engine` is a single setting (`native` default). Nothing about
the native path was touched — `_place_call` branches at its very top
and the entire rest of the existing function is untouched.

## Where this contract came from

Not guessed or invented — cloned
[dograh-hq/dograh](https://github.com/dograh-hq/dograh) and read the
actual source and docs before writing any integration code, the same
way earlier checkpoints inspected this repository's own code before
extending it:

- `docs/voice-agent/api-trigger.mdx` — the trigger request/response
  shape and error codes.
- `docs/developer/webhooks.mdx` — the full set of variables available
  to a Webhook node's `payload_template`.
- `docs/developer/call-dispositions.mdx` — confirms `call_status` is a
  free-text "observed reason the call ended" with no fixed/published
  enum, and that `call_disposition` is an opt-in, per-workflow business
  classification — which is why this checkpoint classifies on
  `call_status` via a documented keyword heuristic rather than trying
  to hard-map an enum that doesn't exist.
- `api/routes/telephony.py`, `sdk/python/src/dograh_sdk/client.py` —
  confirmed Dograh's own webhook *receiving* routes are for inbound
  Twilio-style provider callbacks into Dograh itself, not for
  notifying an external backend — outbound notification is the
  separate Webhook *node* mechanism documented above.

## Trigger contract (outbound)

```
POST {DOGRAH_API_BASE_URL}/api/v1/public/agent/test/{DOGRAH_TRIGGER_UUID}
Headers: Content-Type: application/json, X-API-Key: {DOGRAH_API_KEY}
Body: {"phone_number": "+1...", "initial_context": {
  "call_attempt_id": "<our CallAttempt UUID>",
  "contact_id": "<our Contact UUID>",
  "campaign_id": "<our Campaign UUID>",
  "campaign_name": "..."
}}
```

`call_attempt_id` is the correlation key: Dograh has no notion of our
internal IDs, but `initial_context` round-trips through to the webhook
payload verbatim (Dograh's own documented behavior), so this is how
the webhook later finds its way back to the right `CallAttempt` with
no shared database between the two systems.

`DOGRAH_TRIGGER_MODE=test` (default) always runs the workflow's latest
draft; `production` requires the workflow to be published first —
Dograh's own distinction, switches which URL path is used
(`app/services/telephony/dograh_client.py::_trigger_path`).

Response `200`: `{"status": "initiated", "workflow_run_id": 12345,
"workflow_run_name": "WR-..."}`, stored as `CallAttempt.provider_call_id`.
Errors (`400` telephony-not-configured-in-Dograh, `401`/`403` auth,
`404` trigger not found) are all treated as `NeverConnectedFailureReason.PROVIDER_ERROR`
and routed through the existing `_handle_never_connected_failure` ->
`RecoveryManager` path (§20), identically to a native provider failure.

## Webhook contract (inbound) — exact template to configure in Dograh

In the Dograh workflow's **Webhook** node, set the payload template to
exactly this (see `app/schemas/dograh_webhook.py` for what this
repository expects to receive):

```json
{
  "call_attempt_id": "{{initial_context.call_attempt_id}}",
  "workflow_run_id": "{{workflow_run_id}}",
  "call_status": "{{gathered_context.call_status}}",
  "call_disposition": "{{gathered_context.call_disposition}}",
  "mapped_call_disposition": "{{gathered_context.mapped_call_disposition}}",
  "duration_seconds": "{{cost_info.call_duration_seconds}}",
  "recording_url": "{{recording_url}}",
  "transcript_url": "{{transcript_url}}",
  "call_time": "{{call_time}}"
}
```

Set the node's URL to `{this app's base URL}/api/v1/webhooks/dograh/call-completed`,
and its auth to either a Bearer token or an API key (Dograh supports
both) equal to `DOGRAH_WEBHOOK_SECRET` — the receiver accepts either
header (`app/api/routes/dograh_webhook.py::_verify_secret`).

### Why the webhook never needs to handle "never connected"

By the time this webhook fires at all, Dograh already ran a full
workflow — a trigger-time failure (bad number, telephony not
configured) is a `4xx` on the *trigger* request above, handled
synchronously and never reaches this webhook. So classification here
only ever needs to distinguish "ended normally" from "dropped
mid-call" — never "failed to connect" — which is a real simplification
versus the native path's three-way split.

### Classification heuristic (documented, not exhaustive)

`call_status` has no fixed vocabulary (confirmed above), so
`app/services/telephony/dograh_webhook_service.py::_classify` uses a
keyword match: `error`/`fail`/`timeout`/`disconnect`/`drop`/`technical`
→ `DROPPED_MID_CALL` (→ `MidCallDisconnectReason.NETWORK_PROBLEM` if it
also mentions network/connection, else `TECHNICAL_ISSUE`); everything
else → `ENDED_NORMALLY`. Biased toward "ended normally" since a call
that produced a webhook at all ran to completion in the large majority
of cases. Easy to extend if your workflow's actual `call_status`
values need a different split — see the module's own docstring for the
full reasoning, the same "documented approximation" pattern used for
CP06's `FakeAnalysisLLM` keyword matching and CP07's opt-out-rate
approximation.

### Transcript

`transcript_url` is "a public download URL" per Dograh's own docs —
its export format isn't fixed in what's published, so
`_fetch_transcript_lines` is deliberately best-effort: it tries to
parse the response as a JSON array of `{role, content}`-shaped objects
(and a few common key aliases: `speaker`/`text`/`message`), and **never
fails the webhook** if the fetch or parse doesn't work — the call's
terminal-state transition is what's durable and important; a missing
transcript just means CP06's analysis worker sees a call with zero
messages and returns its own already-existing graceful "no content
captured" result (see `docs/CHECKPOINT-06-NOTES.md`).

### Idempotency

Dograh's own docs explicitly warn deliveries may repeat. Handled the
same way CP05/06 handle redelivery elsewhere in this codebase: a
`CallAttempt` already in a terminal state (`ENDED_NORMALLY` or
`DROPPED_MID_CALL`) short-circuits to `{"outcome": "already_processed"}`
without touching anything — verified with a literal duplicate-POST
test.

## Manual setup checklist (outside this repository)

This repository cannot do any of the following for you — they live in
Dograh's own deployment/UI:

1. **Deploy Dograh itself.** It is not embedded in this project's
   `docker-compose.yml` — it's a full separate stack (its own API,
   worker, ARI manager, campaign orchestrator, UI, Postgres, Redis,
   MinIO). Follow Dograh's own Docker deployment guide in its repo.
   Merging Dograh's multi-service stack into this project's own
   `docker-compose.yml` was deliberately not attempted — two
   independent Postgres/Redis pairs under one compose file with
   diverging lifecycles is exactly the "duplicate infrastructure"
   this project's own conventions warn against; run them as two
   separate deployments instead.
2. **Configure Dograh's own telephony + model credentials** (Twilio/
   Vonage/etc., your LLM/STT/TTS provider keys) inside Dograh's
   settings — none of that belongs in *this* repo's config, since
   Dograh is the one placing the call.
3. **Build a workflow** in Dograh's visual workflow builder encoding
   your actual conversation script/logic — this is genuinely design
   work Dograh's UI exists for; nothing here can generate it.
4. **Add an API Trigger node** to that workflow, note its UUID ->
   `DOGRAH_TRIGGER_UUID`.
5. **Add a Webhook node**, paste the exact `payload_template` above,
   point its URL at this app's `/api/v1/webhooks/dograh/call-completed`,
   and set its auth to `DOGRAH_WEBHOOK_SECRET`.
6. **Generate a Dograh API key** (org settings) -> `DOGRAH_API_KEY`.
7. Set `CALLING_ENGINE=dograh`, `DOGRAH_API_BASE_URL`, and the above in
   this app's `.env`.
8. If running in `test` mode (`DOGRAH_TRIGGER_MODE=test`, the default),
   no publish step is needed; for `production`, publish the workflow
   first (Dograh's own requirement).

## Known limitations

- No automated test exercises a real Dograh instance (none is
  available in this environment) — verified against the *documented*
  contract with mocked HTTP responses (`tests/test_dograh_client.py`,
  `tests/test_dograh_dialer_integration.py`,
  `tests/test_dograh_webhook.py`), not a live integration test. If
  Dograh's actual response shape has drifted from its published docs,
  this integration needs re-verification against a real instance.
- `ContactStatus.COMPLETED` is used uniformly for every "ended
  normally" Dograh call — there's no equivalent to the native
  orchestrator's `pending_points`-based COMPLETED-vs-COMPLETED_PARTIAL
  distinction (CP04), since Dograh doesn't expose a comparable signal.
  Documented simplification, not an oversight.
- The `call_status` → terminal-state classification is a keyword
  heuristic, not an exhaustive mapping (see above) — tune the keyword
  lists in `dograh_webhook_service.py` if your workflow's actual status
  strings need different handling.
- `call_disposition`/`mapped_call_disposition` are received and stored
  in the webhook payload but not yet surfaced anywhere (not fed into
  CP06's `CallAnalysis`, not shown in the CP07 dashboard) — CP06's own
  analysis pipeline still runs independently on whatever transcript was
  recovered. Wiring Dograh's own disposition into the dashboard/
  analysis model is a reasonable follow-up, not done here to keep this
  checkpoint's diff focused on the trigger/webhook contract itself.
- Retry-on-`DROPPED_MID_CALL` reuses CP05's existing `RecoveryManager`
  unchanged, which means a Dograh-driven retry also goes back through
  the Dograh trigger path (same `_place_call` branch) — correct by
  construction, but not exercised end-to-end against a real multi-
  attempt Dograh scenario in tests, only unit-level.

## Tests

19 new tests: `tests/test_dograh_client.py` (8 -- trigger success,
config errors, 400/401 mapping, timeout), `tests/test_dograh_dialer_integration.py`
(2 -- successful trigger sets `CONNECTED` without starting the native
conversation loop; trigger failure sets `FAILED_TO_CONNECT` without
crashing the worker), `tests/test_dograh_webhook.py` (9 -- auth via
both Bearer and X-API-Key, wrong-secret rejection, normal-ending → CP06
admission, technical-failure status → CP05 recovery, duplicate-
delivery idempotency, unknown-attempt 404, malformed-UUID 422,
transcript-fetch-failure doesn't fail the webhook). Full suite: 274
passed (255 pre-existing + 19 new). `ruff check .` clean, `mypy app/`
clean (129 source files).
