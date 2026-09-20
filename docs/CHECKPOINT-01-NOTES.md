# Checkpoint 01 — implementation notes / deviations

Recorded per the checkpoint instruction to review "did I accidentally
implement future checkpoint functionality" and to preserve canonical
terminology already established in the repository.

## `CampaignContact` not implemented

The checkpoint template suggests a `CampaignContact` join table so "the
same contact can participate in multiple campaigns." The reconciled,
"Production Ready" schema in `docs/specs/Backend/Database-Design.md`
models this differently: its ER diagram is `CAMPAIGN ||--o{ CONTACT`
(one campaign has many contacts; a contact belongs to exactly one
campaign), and `contact.campaign_id` is a direct foreign key, not a
join-table relationship. Per Checkpoint 01's own Step 1 rule — "do not
assume old documentation is authoritative if a newer reconciled spec
exists" — this implementation follows `Database-Design.md` and does not
introduce a `CampaignContact` table that would contradict it. If
multi-campaign contact membership becomes an actual product requirement,
that's a schema change for a future checkpoint, made deliberately rather
than by two conflicting documents pulling in different directions.

## `AgentConfig` not implemented

`docs/specs/Backend/Database-Design.md` §2.3 defines `agent_config`
alongside `retry_policy`, but Checkpoint 01's explicit model list does
not include it and no current endpoint or test needs it. Left for the
checkpoint that first needs to configure agent persona/script, to avoid
speculative schema.

## Models scoped to this checkpoint only

`transcript`, `recording_ref`, `analysis`, `disconnect_event`,
`working_memory_snapshot`, `contact_history`, and `final_output` are all
defined in `Database-Design.md` but explicitly belong to later
checkpoints (post-call analysis, recovery execution, dashboard) per
Checkpoint 01's own "Do not implement" list. Not created here.

Note also that `working_memory_snapshot` is placed in the *in-memory
store*, not the Primary DB, per `Database-Design.md` §4 ("Storage
placement summary") — it will not become a Postgres table even in a
later checkpoint that implements it.

**Correction (Checkpoint 04):** the paragraph above was wrong.
`Database-Design.md` §2.6 fully specifies `working_memory_snapshot` as
a normal table with an `attempt_id` FK, documented the same way every
other Primary DB table is; §4's "in-memory store" note describes an
optional hot-path cache in front of it, not its only copy — durable
conversation state has to live in PostgreSQL for "no lost
conversations" to mean anything. The table is actually implemented in
Checkpoint 04 — see `docs/CHECKPOINT-04-NOTES.md`.
