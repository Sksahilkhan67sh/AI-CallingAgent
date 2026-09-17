# Checkpoint 02 — implementation notes / deviations

## Bulk import creates the campaign (FR-1.1–FR-1.4)

The checkpoint template's Step 17 describes import generically, but
the reconciled spec is explicit: `Product/AI-Calling-Agent-FRS.docx`
FR-1.4 — *"The system shall create a campaign record and set every
imported contact's status to Pending"* — and
`Product/Acceptance-Criteria.md` US-1.1 — *"Given an empty file or a
file with zero valid rows, when imported, then no campaign is created
and the manager is informed no valid contacts were found."*

So import is implemented as its own campaign-creating operation:
`POST /api/v1/campaigns/import` (name + CSV file in, campaign +
summary out), not as an operation against a pre-existing
`campaign_id`. A campaign can still be created empty via
`POST /api/v1/campaigns` and have contacts added one at a time via
`POST /api/v1/contacts` — import is an additional path, not a
replacement for either.

## "Add/remove contact to/from campaign" under a direct FK

Checkpoint 01 (see `docs/CHECKPOINT-01-NOTES.md`) established that the
reconciled `Database-Design.md` models campaign membership as a direct,
required `contact.campaign_id` foreign key — not a join table — and
Checkpoint 02's own Step 12 explicitly reaffirms not to introduce one.
Under that model, every contact already belongs to exactly one campaign
from the moment it's created (`POST /api/v1/contacts` and the CSV
import both require/produce a campaign_id). So:

- **"Add contact to campaign"** (`POST
  /api/v1/campaigns/{campaign_id}/contacts/{contact_id}`) is
  implemented as *reassigning* an existing contact's `campaign_id` to
  the given campaign, running it through the same eligibility/
  suppression checks as creation. This is the operation that actually
  exists under a direct-FK model — moving a contact into a (usually
  different) campaign — rather than literally attaching a
  previously campaign-less contact, which the schema doesn't allow
  (`campaign_id` is `NOT NULL`, unchanged from Checkpoint 01/01A; this
  checkpoint does not alter that).
- **"Remove contact from campaign"** (`DELETE
  .../contacts/{contact_id}`) cannot mean *clear the association* — the
  same `NOT NULL` constraint rules that out, and Checkpoint 01A's audit
  already confirmed no cascading deletes are appropriate for this data.
  Implemented instead as the soft-deactivation Step 8 itself asks for:
  the contact's `status` moves to `Closed` (the existing terminal state
  from `Call-State-Machine.md`), which takes it out of anything a
  future queue would consider callable while preserving its `campaign_id`,
  attempt history, and audit trail intact.

## Tenant

Re-confirmed (third time, after Checkpoints 01 and 01A): the reconciled
`Database-Design.md` has no tenant concept in any table. Steps
referencing "tenant" throughout this checkpoint are conditional on it
existing ("where the current schema supports them" / "if specified") —
it doesn't, so nothing was added.

## Pagination

Simple limit/offset, not cursor-based: the spec doesn't call for cursor
pagination anywhere, and Step 25 explicitly says not to introduce an
overly complex cursor system without a stated need. Default limit 50,
max limit 200, ordering `created_at DESC, id DESC` (deterministic, per
Step 6's own example).

## Bulk import bound

Max 10,000 rows per import file, read and processed in fixed-size
chunks (500 rows) rather than loading the whole file into memory at
once. 10,000 is a reasonable single-request bound for a *synchronous*
import (Step 17 explicitly rules out background job processing this
checkpoint); a truly 100K+ import belongs to an async job system in a
later checkpoint, not invented speculatively here.
