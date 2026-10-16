# P0-4 Decision A: Dograh-only vs native providers

Labels: [READ] = read in code/docs (file:line), [RAN] = command executed. Section 1 contains only facts that were read or run.
Paths are relative to the repo root.

## 1. Facts

### 1.1 Production already rejects the native engine [READ]
- `backend/app/core/config.py:655-659`: when ENVIRONMENT=production, `calling_engine != "dograh"` adds the problem "CALLING_ENGINE must be 'dograh' ... (the native engine only has mock providers)". Comment at :653-654: "every native provider is still a mock".
- `backend/app/core/config.py:143`: the default is `calling_engine: str = "native"`; closed set at :26 (`native`, `dograh`).
- Providers default to mock: `telephony_provider` :101, `stt_provider`/`llm_provider`/`tts_provider` :128-130.

### 1.2 Modules that only run on the native path [READ + RAN line counts via `wc -l`]
| Path | Lines | Why native-only (evidence) |
|---|---|---|
| `backend/app/services/ai/**` (21 files; see list below) | 1431 | Only importer outside the folder is `dialer_worker.py:1267` (`start_conversation`), which is skipped when `calling_engine == "dograh"` (`dialer_worker.py:917-921`) [RAN grep `services\.ai`] |
| - `ai/conversation/orchestrator.py` | 476 | |
| - `ai/policy/engine.py` | 174 | |
| - `ai/conversation/context.py` / `start.py` | 109 / 55 | |
| - `ai/llm/fake_llm.py`, `stt/fake_stt.py`, `tts/fake_tts.py`, `audio/fake_audio.py` | 103 / 36 / 19 / 42 | fakes only |
| - `ai/memory/schema.py`, `memory/store.py` | 102 / 90 | working-memory store |
| - `ai/factory.py`, `ai/schemas.py`, base interfaces | 53 / 48 / 124 | |
| `backend/app/services/telephony/mock_provider.py` | 92 | the only provider `get_telephony_provider()` can return (`telephony/factory.py:15-22`) |
| `backend/app/models/agent_config.py` | 33 | read only by `ai/conversation/start.py:13,36` |
| `backend/app/models/working_memory_snapshot.py` | 59 | written by the orchestrator; see 1.2a |
| `backend/alembic/versions/4d189edbc3f3_add_agent_config_and_working_memory_.py` | 69 | migration creating both tables |
| Total of the four code rows above (ai, mock_provider, agent_config, working_memory_snapshot; migration excluded) (`cat ... | wc -l`) | **1615** | |

Native-only tests [RAN `wc -l`]: `tests/ai_helpers.py` + 8 files `tests/test_ai_*.py` = **1042 lines** (9 files);
`tests/test_provider.py` 73 lines tests `MockTelephonyProvider` (`test_provider.py:6-7`).
Tests that merely reference native modules but also cover shared logic (not dead): `tests/conftest.py` 161, `tests/cp15_helpers.py` 142, `tests/test_dialer_worker.py` 329, `tests/test_concurrency_dialer.py` 128, `tests/test_recovery_integration.py` 241, `tests/test_cp14b_sweeper_budget_admission.py` 502, `tests/test_cp15a_retention.py` 464, `tests/test_cp15a_migrations.py` 157, `tests/test_cp15a_data_inventory.py` 89.

**1.2a Not fully dead even if Dograh-only [READ]:** `WorkingMemorySnapshot` is still referenced by data-lifecycle code:
`app/services/data_lifecycle/erasure.py:34,65-71` (erasure scrub), `app/services/data_lifecycle/retention.py:55,146-163,276` (retention step `working_memory_snapshots` on `transcript_days`),
and `app/core/data_inventory.py:280`. The table would stay in the schema and the erase/purge paths would keep touching it.
Dialer branches on the engine at `app/services/queue/dialer_worker.py:397, 491, 917`.

### 1.3 What the native path still lacks for real use [READ + RAN]
- Telephony: only `mock` is implemented; anything else raises `ValueError` (`telephony/factory.py:15-22`).
- STT / LLM / TTS / audio gateway: the factory returns only `FakeSTT`, `FakeLLM`, `FakeTTS`, `FakeAudioSession`; other values raise `ValueError` (`ai/factory.py:21-50`, header :3-4).
- Audio transport: `AudioSession` is an abstract interface (`ai/audio/base.py:11-34`) whose only implementation is `fake_audio.py` (42 lines). No websocket/RTP code exists in `app/`. [RAN: tree listing of app/services/ai/audio shows only base.py, fake_audio.py]
- `agent_config` API route: none. `grep -rli "agent_config\|AgentConfig" app/api app/schemas` returns 0 files [RAN]; the model is only used by `start.py`.

### 1.4 What Dograh-only still depends on that is UNVERIFIED [READ]
- Live Dograh E2E not run: `docs/CHECKPOINT-09-NOTES.md:576` ("UNVERIFIED - BLOCKED BY ENVIRONMENT"), `:579` (production readiness BLOCKED), `:700-702` (§19.6 limitations).
- Completion-token vocabulary (`hangup, completed, ...`) unverified against real output; a miss means completed calls retried up to 2x: `CHECKPOINT-09-NOTES.md:703-706`.
- `docs/CHECKPOINT-10-NOTES.md:77` (Dograh returns 500/502/504 only when a run may exist: UNVERIFIED), `:84` (no published enum for the status fields), `:121` (whether recording is enabled in the Dograh workflow: UNVERIFIED).
- `docs/CHECKPOINT-13-NOTES.md:3` ("NOT PRODUCTION READY - LIVE DOGRAH E2E: BLOCKED"), `:146-148` (missing DOGRAH_API_KEY, trigger UUID/base URL, webhook URL, test number), `:157` (disposition code, voicemail/system_cancelled mappings are judgments).
- `docs/CHECKPOINT-14B-NOTES.md:37` (auth header not verified live), `:41` (no endpoint to run QA on an arbitrary transcript), `:46` (cost attribution UNVERIFIED/BLOCKED), `:51` (QA annotation key handling UNVERIFIED).

## 2. Options
| | A1 Dograh-only now | A2 Native providers later | A3 Both |
|---|---|---|---|
| Effort drivers | Live E2E against a real Dograh account; verify status/disposition vocabulary; webhook rate sizing | Real STT, LLM, TTS, audio gateway + telephony adapters and transport, agent_config API, real-time latency/barge-in work; per-provider contracts and compliance | Everything in A1 plus everything in A2, plus keeping two engines consistent |
| Main risks | Vendor dependency on Dograh; unverified behaviours in 1.4 | Large build with no production path today (1.3); native code is only tested with fakes | Double maintenance; shared tables/dialer branches (1.2a) must work for both |
| Needed before first real call | The five blocked items in `CHECKPOINT-13-NOTES.md:146-148`; one live test call; legal items from P0-7 | All of A1's compliance items plus a working provider stack | Both |
| Native code | **Keep frozen** (not deleted): the production guard at `config.py:655` already isolates it; the working-memory table is still touched by erasure/retention (1.2a) | Becomes the main work area | Keep and maintain |
| Delete option | Deleting `services/ai/**` (1431 lines) is possible but needs changes to erasure/retention/data_inventory, a migration decision for two tables, and removal of 1042+73 test lines; do it only after the owner confirms A2 is dropped | n/a | n/a |

## 3. Recommendation (judgment; the owner decides)
Choose **A1 Dograh-only now, native code frozen (not deleted)**. Reasons:
1. Production already refuses anything else (`config.py:655-659`), so A1 is the behaviour that exists; A2/A3 add a build that has no real provider code (1.3).
2. The remaining launch risk is the unverified live Dograh behaviour (1.4); effort spent on native providers does not reduce it.
3. Freezing costs nothing: no production path imports the native modules (1.2), while deleting touches erasure/retention (1.2a).
Revisit A2 after the first verified live calls and the legal answers (P0-7). Phase 1 scope impact: none for data-loss fixes; do not refactor `services/ai/**`.

OWNER DECISION:
