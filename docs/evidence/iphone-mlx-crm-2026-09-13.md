# iPhone MLX to CRM functional test

## First attempt on build 20260913203646

The user requested a real CRM creation test, including starting the task and
monitoring its execution, with the MLX model loaded on the physical iPhone.
The requested CRM includes clients, quotations/projects, email drafts and a
calendar. Only fictitious test data is needed.

The iPhone 16 Pro session was reopened successfully. The user loaded the pinned
Dolphin MLX model and pressed the local proposal button after entering the CRM
request. A fresh UI capture recorded 85 generated tokens. The output was a
Markdown-fenced JSON object whose `tool_name` was `Flask`, with arguments such as
`app_name`, `database`, `data_type` and `test_framework`.

The app rejected the output with its exact-JSON validation error and explicitly
reported that no task or execution had been created. Even without the Markdown
fence, `Flask` is not an allowed tool. This establishes working on-device
generation and an unsuccessful actionable proposal; it does not establish a
started or completed CRM.

Evidence retained locally:

- `/private/tmp/swarmer-crm-local-invalid-flask.json` contains the fresh scoped
  accessibility snapshot, including the rejection and token count.
- `crm-local-invalid-flask.png` is in the thread's September 13 visualization
  directory.
- `/private/tmp/swarmer-crm-physical-monitor-20260913/` contains the read-only
  backend baseline and scoped monitoring helper.

At the fresh backend baseline, API 0.14.2 at source `f5854de` was healthy, the
project-worker heartbeat was current, and its Qwen3-Coder 30B model was installed.
Ollama had no resident model or active generation. An old Python worker's stale
heartbeat was not counted as actual readiness.

## Missing integration identified

The released local-model screen generated `LocalToolProposal` and submitted it
to a task tool-call endpoint. It did not generate a Swarm plan. The existing
goal-start UI sent an empty start request and therefore used the Ubuntu planner.
Loading Dolphin alone did not change that routing.

The server already accepts an explicit `SwarmPlanProposal` with
`planner_source: iphone_local` for the initial plan. Connecting that contract to
actual local generation requires a mobile change and a new physical-device
build. Initial local planning remains distinct from subsequent evaluation and
project-worker iteration on Ubuntu.

The initial physical test session was closed after the rejected proposal. No
CRM task was launched during this attempt, and no existing goal was altered.

## Model storage inspection

A read-only CoreDevice listing confirmed the actual pinned model files inside
the app's `Library/Caches/huggingface/hub` directory. The model's regular backing
blob is 1,807,499,734 bytes, with a `model.safetensors` snapshot link for revision
`cdc777b578ff86a69f1b05c9bc00df0cdc2f52d1`. Configuration and tokenizer files are
also present; their modification dates are September 8. The files are local,
not weights streamed remotely for each generation. Unloading the MLX container
only releases its in-memory model; it does not delete these cached files.

The user requested permanent model storage accessible through the iPhone Files
app. `Documents` currently also contains the app's `SQLite` directory, so exposing
that directory requires separating internal database storage from user model
files. Read-only inventories are retained in
`/private/tmp/swarmer-iphone-dolphin-cache.json` and
`/private/tmp/swarmer-iphone-documents-inventory.json`.

## Follow-up implementation

The mobile change adds an explicit initial-plan flow for an unstarted goal.
Dolphin generates the plan on the iPhone; the user reviews it before sending
`planner_source: iphone_local` to the existing goal-start endpoint. Strict JSON,
current goal/agent context, pairing changes, budget limits and uncertain-start
handling are checked before submission. Ubuntu still performs evaluation and
project-worker iterations. Invalid or truncated local output cannot start work.

Pinned MLX snapshots are materialized as regular files in
`Documents/Models/<repository>/<revision>/payload`. A complete cached snapshot
can be promoted without another network download. The private model registry
anchors file hashes and interrupted-publication recovery; a public manifest
alone cannot establish pinned provenance. Unloading releases model memory.
Model listing reads metadata; loading verifies the retained files.

The iOS replica and mutation outbox now share a migration opener. It creates a
SQLite backup in `Library/Application Support/Swarmer/SQLite`, verifies schema,
rows, version and integrity, publishes it without replacing another database,
then removes the known legacy database and journal files. Divergent copies are
preserved. Files sharing applies at installation, so the legacy database can
remain visible until the first successful migration; the physical check must
confirm its removal before validating the model folder in Files.

The Swift model-store harness passes 15/15 tests, including cache-independent
files, modified artifacts, cancellation and interrupted publication. Migration
tests cover SQLite/Files URI agreement with spaces and Unicode. A real SQLite
WAL backup comparison on the Mac passed; it is not physical iPhone migration
evidence. The complete mobile suite passes 469 tests across 35 suites, with
TypeScript, ESLint and diff checks passing. Installed dependencies are consistent
and Expo Doctor passes 20/20 checks.

## Development archive ready; device retest blocked

Development build `20260913223250` was compiled from source
`d9da64b082f6b698f0fc878ae6011de8fafb777c`, mobile tree
`1d9df9005ecd70a22bed6da6066a5293e676ea8b`. Xcode completed the Release archive
successfully in 1,796.53 seconds. The signed app has both Files flags enabled,
iOS 18 as its minimum target, the intended development profile and native
MLX/Core ML/GGUF/FileSystem components. App and llama dSYM UUIDs match their
binaries. An audit helper expected a different llama dSYM filename; a separate
audit used the actual `llama.dSYM` without changing or recompiling the app.

The builder was restored with no errors, its temporary dependency alias was
removed and associated build processes exited. Prior release artifacts remain
unchanged. The development IPA is 19,871,264 bytes with SHA-256
`5275867c0b3b2a995240b5480d1302cc45d091977e6705729d3163d7420b0a0d`.
Detailed checks and the installable app are retained under
`/private/tmp/swarmer-local-planner-development-20260913223250/`.

Both GitHub and Vibecode were verified at the source commit. GitHub Actions run
`34788465351` could not start its job because the account is locked due to a
billing issue; it did not run or fail the source tests.

The iPhone remains paired but unavailable in a fresh CoreDevice listing. Its
development tunnel and DDI services could not be reached; no pairing, network
settings or system services were changed. Unlocking the phone on the same
network has been requested. This build has **not** been installed, launched or
uploaded. Its on-device database migration, Files folder, local initial plan
and CRM execution remain unverified. The existing TestFlight build remains
`20260913203646`; a successful archive is not a new TestFlight submission.

The before/after database verification procedure is prepared at
`/private/tmp/swarmer-replica-device-verification-20260913/README.md`, and the
functional sequence is at
`/private/tmp/swarmer-physical-validation-20260913223250.md`. No private database
copy was taken while the device was unavailable.
