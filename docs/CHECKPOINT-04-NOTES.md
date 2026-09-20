# Checkpoint 04 — implementation notes / deviations

## Two new tables: `working_memory_snapshot` and `agent_config`

Both are already fully specified in `docs/specs/Backend/Database-Design.md`
§2.3 and §2.6 (exact columns, types, FKs) — they simply weren't needed
by any earlier checkpoint and so weren't added yet. This checkpoint is
what actually needs them (campaign-configured persona/script for the
system prompt; durable memory checkpointing for "no lost
conversations"), so adding them now is implementing spec that was
already written, not introducing anything speculative.

One real addition beyond §2.3: **`agent_config.goal`** (text, nullable).
`Database-Design.md` §2.3 doesn't list a `goal` column, but
`AI-Agent-Specification.md` §2 and `Prompt-Specification.md` §2 both
require a campaign goal as a first-class configured field (`{{campaign_goal}}`
in the system prompt template) — its absence from the DB schema reads as
a gap in that document, not an intentional omission, so it's added here
and documented rather than silently worked around.

`working_memory_snapshot` deliberately has **no `turns` column**, even
though `Memory-Specification.md` §2 lists `turns` as part of the
working-memory schema. This is intentional, not a gap: `turns` (the
full utterance/response log) is exactly what `ConversationMessage`
(the transcript) already stores — duplicating it into the memory
snapshot would violate this checkpoint's own Step 12 ("memory must NOT
be the transcript"). The application-level `WorkingMemory` object
still exposes `turns` (per the spec's in-application schema), but it's
populated by reading recent `ConversationMessage` rows, not by a
redundant DB column. Only the compact structured state — entities,
script progress, objections, last utterance, disconnect count,
suppression flag, recording consent, schema version — is what actually
gets snapshotted to `working_memory_snapshot`, matching its real
column list exactly.

`Database-Design.md` §4's storage-placement table lists
`working_memory_snapshot`'s store as "In-memory Store (durable-backed)
for active/recent attempts." Read together with this checkpoint's own
Step 13 ("PostgreSQL must hold durable conversation state... do not
rely only on Redis") and Step 34 ("if Redis disappears, the
conversation must not become permanently corrupted... reconstruct from
PostgreSQL where practical"), the durable copy lives in PostgreSQL
(the table above); Redis is used only as an optional hot-path cache in
front of it, never as the only copy. This corrects an earlier,
mistaken reading in `docs/CHECKPOINT-01-NOTES.md`, which treated this
table as Redis-only and skipped it — that was wrong, and is corrected
here rather than carried forward.

## Conversation phase = the canonical phases from `Conversation-Flow.md`

`Opening`, `Discovery`, `ObjectionHandling`, `Closing`, `WrapUp` — these
are not invented; they're §1 of that document, used verbatim as the
`ConversationPhase` enum. The intent taxonomy (9 values) and
`next_action` taxonomy (7 values) are copied verbatim from
`Prompt-Specification.md` §4's structured output schema, for the same
reason — Checkpoint 04's own Step 29 explicitly requires exact
canonical names, not invented ones.

## `ConversationSessionStatus` stays `{ACTIVE, ENDED}` (no expansion)

Checkpoint 04's Step 5 suggests a richer lifecycle
(`CREATED -> ACTIVE -> ENDING -> COMPLETED`) "if the existing canonical
state names differ." No other spec document defines session-level
states more granularly than Checkpoint 01's existing two, and nothing
in this checkpoint's actual behavior needs to observe a session sitting
in a `CREATED`-but-not-yet-`ACTIVE` state (the session row is created at
the same moment the conversation begins) or a distinct `ENDING` state
(ending is synchronous within this checkpoint's scope — there's no
async drain to observe mid-flight). Reusing the existing two-value enum
avoids "no unnecessary abstractions" (Step 63) rather than expanding a
lifecycle nothing reads.

## Audio/STT/TTS providers: fakes only, by design

No STT, LLM, or TTS provider is named anywhere in `docs/specs/` (checked
`Architecture/*.md`, `AI/*.md`, `Backend/*.md`), and no provider
credentials exist in this environment. Per this checkpoint's own Step 2
and Step 13, the correct response to that is the production-shaped
interface plus a deterministic fake, not a real integration nothing
here could actually exercise or verify. `FakeSTT`, `FakeLLM`, and
`FakeTTS` are the only implementations. The LLM interface's structured
output type matches `Prompt-Specification.md` §4 exactly, so swapping
in a real provider later is an adapter that maps its response into the
same shape, not a change to any orchestration code.

## Audio session: text-level, not byte-level, for the fake

`TelephonySTT.send_audio()` accepts bytes, matching the real streaming
contract. `FakeSTT`, having no real speech engine behind it, is driven
in tests via an explicit `simulate_utterance(text, is_final=...)`
method rather than needing pre-recorded audio fixtures mapped to
transcripts — the same reasoning Checkpoint 03 used for
`MockTelephonyProvider.set_outcome`: a fake exists to let the
orchestration logic around it be tested deterministically, not to
imitate the real provider's internals.
