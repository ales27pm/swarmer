# Goal-scoped canonical memory requests — 3 October 2026

Baseline: `e4d52d1`. This candidate joins the existing canonical memory service to
agent planning; it does not claim that the entire multilingual/symbolic memory
plan is complete. Validation below uses disposable databases. No production
qualification project, task or image was created.

## Observable behavior

- Startup connects `StateService` to `StrategyRetrieval` only when
  `MONGARS_MEMORY_CANONICAL_LANGUAGE=en`; the default remains `legacy`.
- Each actual HTTP request is admitted and recorded under the requesting goal:
  `memory_normalizer`, `memory_reviewer`, `memory_presenter`,
  `memory_presentation_reviewer` or `memory_embedder`. Model identity, endpoint
  and exact JSON request contribute to the input digest. Strict response parsing
  is inside the recorded operation. A well-formed reviewer rejection completes
  that model call but does not qualify the translation.
- The existing SQLite admission/lease and shared host GPU lock protect these
  requests. Resource contention sends no request and consumes no budget.
  Deterministic local embeddings do not invent model calls. A failed HTTP request
  is an attempted call, with an explicit failure receipt.
- Optional retrieval preserves one planner credit. If that is all that remains,
  the planner receives mandatory inputs and an explicit retrieval-unavailable
  card, not a fictitious successful empty search. Failed embedding requests can
  retain applicable lexical results with a degraded retrieval receipt.
- A stale revision, terminal goal, expired lease or cancellation cannot deliver
  a late result. Cancellation after the reservation commit is drained to a known
  result; an unused reservation is marked `cancelled_before_request`, not sent.
- Context provenance binds the selected hints to their source IDs, revisions,
  scope, canonical hashes and receipt IDs. The historical request digest is not
  rewritten when its context is attached. French presentation is not inserted as
  another durable memory.
- Reconciliation applies a 60-second cooldown to a current memory failure in
  that conversation revision. A new user revision or successful explicit restart
  is not held up by an older failure audit. The audit itself remains intact.
- Server and mobile activity contracts recognize the five new receipt roles;
  they do not change the model router's four execution roles.

## Migration and rollout

Schema 28 expands the role constraint of `goal_model_calls`. Existing rows and
rowids, indices, triggers and incoming compaction references are preserved.
Migration tests cover interruption before rename, rollback to schema 27, retry,
idempotent initialization and rejection of unknown roles and duplicate active
calls. A separate disposable rehearsal used the actual predecessor schema
literals from Git, rather than a hand-written approximation.

The rebuild disables foreign-key enforcement outside its own transaction, copies
and compares both directions, restores schema objects and checks foreign keys
before committing the version. This follows SQLite's documented
[table-rebuild sequence](https://www.sqlite.org/lang_altertable.html#otheralter).

The currently deployed predecessor rejects schema 28. Existing release helpers
also require equal before/after schema versions. A code-only rollback to that
predecessor is therefore not qualified. Before production migration, prepare and
qualify a compatible predecessor plus an exact 27→28 migration guard; preserve
the existing shared GPU admission setup and the full nine-service topology.
Do not bypass these guards or implicitly restore a database over later writes.

The older mobile activity parser rejects the five new roles. Ship the compatible
client before exposing the new receipts. Leaving canonical mode on `legacy` is
not sufficient: goal-scoped episode embedding calls also use `memory_embedder`.

## Validation results

The integrated full server run completed with **3,669 passed, 11 skipped and two
failures** in 741.97 seconds. Its private log is
`~/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/full-suite.log`.

1. `test_schema26_migrates_idempotently_without_synthesizing_coverage` expected
   literal schema 27. It now expects the current schema; all 31 project-evidence
   tests pass after that correction.
2. `test_process_timeout_and_output_limit` raised macOS `PermissionError` from
   the unchanged Swift worker's final process-group cleanup. The same test passed
   alone as the same user in 0.21 seconds. This does not erase the full-run
   failure or prove the intermittent cleanup issue resolved.

The broader run predates the final restart-cooldown and activity-consumer
corrections. Fresh post-correction results:

| Command / boundary | Result |
| --- | --- |
| Seven new memory/role integration, transport, storage, migration and activity suites | 64 passed in 10.21 s |
| `pytest -q tests/test_goal_memory_execution.py tests/test_goal_memory_execution_races.py tests/test_goal_context_payloads.py tests/test_project_evidence.py` | 51 passed in 12.51 s |
| `pytest -q tests/test_activity.py tests/test_activity_memory_roles.py tests/test_openapi_route_coverage.py tests/test_contract_schemas.py` | 50 passed in 41.54 s |
| Mobile activity parser, timeline and API-boundary Jest suites | 91 passed in 11.46 s |
| Changed mobile source TypeScript and ESLint | Passed |
| `python scripts/validate_openapi.py` | 83 paths, 91 operations, 622 references, 7 schemas valid |
| mypy on the 13 changed server source modules | Passed |
| Ruff on server source and the changed tests | Passed |

The activity regression reproduced HTTP 503 before the contract correction; it
now reads every memory role through both task and goal activity endpoints. The
restart regression failed for both generated and manually supplied plans before
the cooldown correction, then advanced the dependent synthesis in both cases.

The full application mypy run still reports two unrelated unchanged issues:
missing `redis.asyncio` typing/package support in `message_board.py`, and the
`model_profile` Literal override in `media_contracts.py`. These are not represented
as a passing global type check.

### Completed full run and immutable package

The subsequent full run finished with **3,681 passed, 11 skipped and one
deprecation warning in 736.50 seconds**. It ran as the ordinary macOS operator,
with pytest's cache provider disabled, from the source collected at `b012fe0`.
Its log is `full-suite-final.log` in the same private qualification directory.
The later Swift termination-escalation change in `7d7e0a2` was validated by its
24 non-compilation Swift tests; it was not in that already-collected full run.
The root cause and native regression evidence are recorded separately in
[Swift process cleanup](swift-process-cleanup-2026-10-03.md).

An isolated wheel built from Git `b012fe0` contains exactly the 122 expected
application files. Source, wheel and a separate target installation have
identical file contents. Wheel SHA256:
`3cb8702f5fbfa4dee2eab8ee79118402f2648f826740e4c10278e973a6b84c1b`.
The installed copy initializes a disposable schema-28 database with valid
foreign keys and accepts all five memory activity roles. It was not deployed.

### First real provider trial

A private immutable harness was transferred to Ubuntu and exercised the exact
`b012fe0` application source in a disposable database. Its synthetic Ubuntu
check passed six calls; the live trial then made **one actual model request**
and stopped on rejection, without a retry. The configured model was
`swarmer-research-qwen35:9b-8k-6488c96fa5fa`, with its served manifest digest
verified before inference. No production task, goal, memory or image was created.

The first translation returned HTTP 200 after **45.518 seconds**, but used all
2,048 completion tokens in the reasoning field, returned empty content and
`finish_reason=length`. The strict parser correctly reported
`invalid_provider_response`; no translation or review was accepted. The full
HTTP response was received and the trial cleared only its own GPU marker;
there were no unresolved requests. This is evidence of an execution-setting
problem, not a successful semantic translation.

Ollama's [v0.32.3 compatibility implementation](https://github.com/ollama/ollama/blob/v0.32.3/openai/openai.go#L591-L618)
maps `reasoning_effort: "none"` to disabled thinking. Its
[structured-output guidance](https://docs.ollama.com/capabilities/structured-outputs)
also recommends putting the output schema in the prompt. The provider already
supported an explicit reasoning setting, but the tested application startup did
not expose it for memory. Increasing the budget or accepting the reasoning as
the result would not address this failure.

Private receipt: `provider-candidate/live-01/receipt.json` under the qualification
directory. The original failure receipt and response remain unchanged.

### Correction after the real rejection

`MONGARS_MEMORY_NORMALIZATION_REASONING_EFFORT=none` now reaches both memory
providers through startup. The default remains unset; no production setting
has been changed. Each request also includes its compact JSON schema in the
trusted system message, while source data stays in the user message. This
prompt policy is versioned in the provider signatures. The existing byte limit
includes the additional schema and strict parsing still rejects a truncated
response; no retry or token-limit increase was introduced.

Before the fix, 14 new regression cases failed. After it, **292 affected tests
passed in 27.53 seconds**, including 21 new cases. The changed source passes
mypy, Ruff and formatting checks, and an independent review found no blocker.
These focused results follow the full run above; the full run does not include
this later correction. The corrected real-provider trial is still to run.

## Remaining proof

- No server deployment, production canonical-memory activation or historical
  backfill is included in this evidence.
- Historical real-provider trials ended in HTTP or semantic-review rejection;
  they do not establish French→English→French runtime success. A new bounded
  real-provider trial must retain its actual acceptance or rejection result.
- Project/episode migration, conceptual claim identities, dual native/pivot
  indexes and their transactional outbox remain separate outstanding requirements.
- Mock transport tests prove accounting, scope and fencing at the real service
  boundaries; they do not prove a deployed model's translation quality.
