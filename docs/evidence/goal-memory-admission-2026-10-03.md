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
this later correction. The correction is committed as `adbeff0`.

### Corrected provider trial: execution passes, semantic presentation rejected

An immutable `adbeff0` application bundle with explicit `reasoning_effort=none`
completed six real requests in **33.335 seconds**. Each returned HTTP 200,
`finish_reason=stop`, and zero reasoning characters. The write replay made no
extra request, retrieval did not mutate storage, one original journal entry and
one accepted canonical memory were retained, and no goal/task/project/job was
created. All HTTP outcomes were known and the owned GPU marker was cleared.
Fourteen harness tests, including cancellation, crash and foreign-marker cases,
passed before this attempt.

Independent reading rejected the French presentation despite its model review:
the original asks to keep `rapport.csv`, while the French rendering says to
archive it. That introduces a more specific action. Negation, uncertainty and
the exact `30 ms` literal survived, but those successes do not erase the changed
obligation. The initial review considered the English text faithful, but the
subsequent review below identifies an ambiguity in it. The stored
mechanical receipt remains unchanged; a separate `semantic-review.json` records
the semantic rejection. This trial **does not qualify production activation**.

Private evidence: `provider-adbeff08ab36/live-01/` in the qualification directory.
A subsequent comparison must include both faithful and deliberately altered
translations; simply making the reviewer approve this example is not success.

### Reviewer comparison and source-preserving display

Inspection of the actual pivot found `Keep exactly 30 ms and file
\`rapport.csv\`.` Without `the`, `file` can also be read as a verb. This is a
plausible contributor to the back-translation, not evidence of the model's
internal reasoning. The presentation reviewer saw only that English pivot,
not the original French noun phrase. Keeping the original is therefore useful
at the read boundary as well as for provenance.

Before any live comparison, the fixed three-case corpus was disambiguated to
`Keep exactly 30 ms and the file \`rapport.csv\`.` The declared labels were
accept for a faithful French rendering, reject for changing keep to archive,
and reject for reversing the send prohibition. Six real requests compared the
installed pinned 9B and 30B models, under the shared GPU lock and production-idle
checks, without production database writes or automatic retries.

The comparison completed in **103.902 seconds**. Every response was complete;
the owned GPU marker was cleared. The 9B returned the expected accept/reject
decisions for all three examples, but its action-change rejection set only
`uncertainty_preserved=false`, while affirming meaning and no added facts.
The 30B accepted the action change incorrectly and matched two of three labels.
Neither result qualifies a model substitution or broad semantic correctness.
Per-call times were 11.678/5.195/5.152 seconds for 9B and
41.411/18.810/21.440 seconds for 30B, including their loading differences.
No statistical speed or accuracy comparison is inferred from three examples.

Private evidence: `reviewer-comparison-adbeff0/live-01/`. The v2 harness archive
SHA256 is `44449adc9f5ab9027887df4f9a90df0a3cce2d98ef4cc5516f86ab17a3cfbeea`;
the predeclared corpus SHA256 is
`cba55bc83f5c3a868c3dedf9323e620159f1d7d62ee08540c4f895dd637457fb`.
Sixteen private tests and independent review preceded the live run. Verdicts,
durations and response hashes were retained; response reasoning was not exported.

The new code instead reuses the exact qualified French original where available.
It binds the journal, accepted canonical receipt, scope, source hashes and current
canonical revision, including a final recheck after other items are translated.
English sources and items containing mixed-language source fields retain the
reviewed translation path. A mixed batch only sends those remaining items to the
presenter. No original is rewritten, no source view is indexed as a second memory,
and the display grants no additional authority. Mobile and strategy consumers
distinguish `mode=original, validation_status=source_preserved` from the legacy
`model_reviewed` translation contract. Language remains an assertion of the
accepted normalizer metadata, not a property proven by the content hash.

The combined post-change server regression has **270 passed** in 44.65 seconds,
with one existing Starlette deprecation warning. It covers original and mixed
source display, source/revision races, canonical writes, query normalization,
provider output policy, API errors, strategy retrieval and goal-scoped request
accounting. The first combined run had 265 passes and two assertions expecting
the now-avoided presentation calls. Those cases now explicitly exercise both
source languages: five actual memory calls for the French-source path, seven
for the English-source path, including the existing embedding requests; planner
credit, recorded call identities and degraded embedding behavior remain checked.
These counts describe that integration fixture, not every search operation.

Mobile display and screen tests have **41 passed**. TypeScript, changed-file
ESLint, mypy on the three source modules, Ruff and formatting checks pass.
The OpenAPI validator reports 83 paths, 91 operations, 622 references and seven
schemas valid; twelve new schema cases cover the two presentation modes and
reject missing bindings and mismatched statuses. Independent code review found
no blocker. This correction does not establish that the English pivot itself
is semantically reliable; production canonical activation remains unqualified.

### Compatible mobile artifact prepared

The activity-compatible mobile source built successfully in **111.535 seconds**.
The signed DEBUG IPA contains the five memory role names in its embedded Hermes
bundle; all 254 tracked/source inputs and 50 protected native files remained
unchanged during the build. The ten existing Core ML fixtures and native audio
symbols are retained. This is build and packaging evidence, not device behavior.

IPA SHA256: `185734de1baafadd6a78189699036dd3498055d5f98c15249f0c04a7705d00b7`;
embedded bundle SHA256:
`c0075957290cd69273ba4aff3f6f210d8584d97bb0bfb0d3122a886ab0674d24`.
Private receipts are in `mobile-activity-7d7e0a2/`. The displayed version/build
number remains unchanged, so it cannot distinguish this artifact from the older
installed one. The phone was freshly observed connected but passcode-locked;
installation and activity-route runtime validation have not occurred.

The corrected server package was rebuilt from `adbeff0`, separately from the
earlier `b012fe0` artifact. Its 122 application files match source, wheel and
isolated installation byte-for-byte; the installed copy initializes schema 28
and accepts the explicit memory reasoning setting. Wheel SHA256:
`d085a9f90a47e83f2fc19b3c28f2121ad4642b2291b6d9cca9331ab84e0dba1f`.
This package is also not deployed.

Both artifacts above predate the source-preserving display change. They must not
be described as containing that newer server/client contract.

### Four-call real-provider trial from `9288fd4`

The source-preserving candidate completed its bounded live trial in **23.592
seconds**. Exactly four HTTP requests returned complete HTTP 200 responses:
translation/review for creation, then translation/review of the French query.
Their elapsed times were 10.035, 5.017, 3.233 and 4.342 seconds, respectively.
The model and digest were the same pinned 9B used above; there was no retry or
model substitution. The GPU reservation was released only after every HTTP
outcome was known.

The disposable database contained one memory, one original journal entry and
one canonical receipt. Replaying the write made no additional request. Search
made no database writes, issued zero presentation requests, and returned the
exact original French text with `mode=original` and
`validation_status=source_preserved`. Provenance bindings passed the actual
consumer validator. No production goal, task, project, job, memory or image was
created or changed. Embedding retrieval was disabled in this lexical trial;
it does not qualify dual-vector retrieval or a deployed agent execution.

The English pivot again read `Keep exactly 30 ms and file \`rapport.csv\`.`
Manual review did **not** accept its semantic qualification: the French noun
phrase is still ambiguous as an English action. Exact source display resolves
the observed second-translation loss, but does not repair this pivot. The
automatic `mechanical_pass_semantic_review_required` receipt is preserved; a
separate manual semantic receipt records this limitation. Production canonical
activation remains unqualified.

Private evidence: `provider-9288fd4-original/live-01/`. The transferred harness
archive SHA256 is
`037fdcb5c5e83925ecad09e3ea99aec623a14853237bf683e491673ba5a9a0fb`.
Its 123 application files came from the exact source commit; 25 harness tests
and independent review preceded the real run.

### Matching source-preserving server and iOS artifacts

Both new artifacts use `9288fd4fcc7f75dd5f632e12171a2bef435913ef`.
The server wheel has **123 identical application files** across source, wheel
and private installation. The installed smoke check preserves the original in
strategy retrieval, rejects a forged presentation, leaves the database unchanged
on search/reinitialization and initializes schema 28. Its synthetic provider is
separate from the real-provider trial above. Wheel SHA256:
`e8ca905998e95a54b7af87de56d73417dda29d92619948f9e62892c9cc6a41f7`.

The signed DEBUG iOS build succeeded in **164.855 seconds**. The IPA contains the
original/source-preserved contract, French display label and five memory roles;
254 source inputs, 50 protected native inputs and ten Core ML fixtures (88 files)
remained intact. Signature and IPA CRC/content checks passed. IPA SHA256:
`0e66b26415ca2cdec11274e42ed15f1380189a05be1c0572706147c5a91ca313`.
Embedded JavaScript SHA256:
`c4bfd214d1c9115c50deda62b56b97d7e574480943910c72fea7fd8108f213b4`.

Receipts are in `package-9288fd4/` and `mobile-original-9288fd4/` under the same
private qualification directory. Neither artifact was deployed or installed by
these build/qualification steps. They do not establish iPhone runtime behavior
or Core ML Neural Engine compatibility.

### Compatibility predecessor deployed, schema 27 retained

The first rollout tranche activated
`local-20261003-memory-compat27-397e979987a6`. Its provenance is the deployed
`e4d52d1` application plus the reviewed compatibility patch; it is **not** a
schema-28 deployment of `9288fd4`. Only `activity_contracts.py` and
`state_service.py` differ from the previous live API. It retains schema 27 on
initialization and can reopen schema 28 for a subsequent code rollback.

The real private-copy stage checked 120 application files, unchanged dependency
metadata and schema 27→27 across all 63 tables and rowids. The actual preceding
release reopened that same copy without alteration. Stage SHA256:
`ae88840b6dc545ec3f0e7cd8f59c0bfb8cabd0b9deb671b8bc621aa89fdab41f`.
The reviewed completed coordinator config SHA256 is
`2db5f0d971fb06acd359bbb8529172b237591f34650f644338898f01f70c6bd1`;
prepare baseline SHA256:
`573d037fecad2ded555f9a39b034d8f87f2a16f00aa74a464c8c2aaa6cc50967`.

The supervised deployment completed successfully in **10.486 seconds** and
reported no database restoration. Independent live verification at
13:42:21 UTC confirmed nine active services, all **52 protected fingerprints
identical**, seven retained Studio jobs and 37 Studio files unchanged, API health
and Studio readiness. The canonical-language option remains unset (`legacy`).
No model, policy or private configuration was changed. Deployment journal SHA256:
`c1c9425b3e84355d46616c67b8cfddf4817d6404843a95294f6bc6e940afa005`.
Independent verification receipt SHA256:
`8c577c38144ee0958843b9f7b98d07bfb299d7cff1627e7eca90852d90477c93`.

Local receipts: `compat27-deployment-inputs/live-verification/`. The source-
preserving candidate and compatible iOS IPA are still separate artifacts. The
known iPhone was connected but reported `passcodeRequired=true` at 13:24:29 UTC;
it was not installed or relaunched by this tranche.

## Remaining proof

- The compatibility predecessor is deployed. The schema-28 candidate, compatible
  iOS installation, production canonical-memory activation and historical
  backfill remain outstanding.
- The four-call trial proves exact original display for one French source, but
  semantic qualification of its English pivot is still not accepted. A broader
  fixed corpus must include faithful and altered claims; one mechanical success
  is not a production translation-quality gate.
- Project/episode migration, conceptual claim identities, dual native/pivot
  indexes and their transactional outbox remain separate outstanding requirements.
- Mock transport tests prove accounting, scope and fencing at the real service
  boundaries; they do not prove a deployed model's translation quality.
