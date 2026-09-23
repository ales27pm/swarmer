# Native project transfer and model recovery — 22 September 2026

## Implemented behavior

- A paired device approves one immutable project revision, compiler worker and
  target. The opted-in iMac worker automatically retrieves and stages that
  snapshot, compiles/tests it and returns an identity-bound receipt. No model
  argument can grant source execution. The app exposes preparation, confirmation,
  status and cancellation through its common application API.
- Changing the revision/conversation or cancelling the validation/parent goal
  revokes execution. Queued native jobs are cancelled as well; no offline worker
  is needed to complete cancellation. Source files remain in project history.
- The project model cannot request another read of a file already fully present
  in its final prompt. Omitted and partial source remain readable. Invalid output
  is still rejected; the implementation does not silently retry a model call.
- E5-small's pinned configuration is a BERT encoder with an XLM-R tokenizer.
  Its previous `xlm-roberta` architecture check was incorrect. Embedding models
  now have a distinct purpose and cannot be selected or loaded for generation.
  Older E5 records are recognized without rewriting weights or Documents files.
- Planner decoding prefers executable workers before synthesis and documents
  dependencies on plan-node IDs. Unknown dependencies are rejected at parsing as
  well as graph validation. Executor errors distinguish unsupported tools,
  incompatible arguments and policy denial without exposing private arguments.
- An older server's missing `/memory/status` route is explained accurately in
  the app, with stale status cleared. It is never treated as evidence that
  embeddings are disabled or functioning.

## Real execution evidence

The previous benign addition fixture was repeated against the actual Ubuntu
`swarmer-project-qwen3-coder-heretic:30b-32k-d2d985e` model using the corrected
worker. One call changed only `src/calculator.py`, requested no further focused
read, preserved both applicable AGENTS.md receipts and passed compileall plus
**two actual pytest tests** in the pinned network-isolated runtime. No user goal
or project was modified. Inference took approximately 79.5 seconds. This proves
the arithmetic repair, not arbitrary CRM completion; project readiness still
reported the missing README instead of fabricating completion.

Private result SHA256:
`88c0ca0c392883d44d35e6e1eba8880999503684c4138a66573e5f001540e042`.

One real planner call to the existing 9B profile completed in 24.46 seconds. It
returned two independent documentation searches and one synthesis with exactly
those two node IDs as dependencies. Strict parsing and DAG checks passed. No
search was executed and no goal/job was created by this qualification. Result
SHA256: `2ba3edcfa52fcb473aaad36e2bfe16190e0607707f993d053a61b073b4d8ae23`.

The authenticated API-to-worker test stored a small Hello Swift package, approved
its revision, fetched it under a worker lease, staged it privately and ran real
Swift compilation and **one XCTest**. The receipt was accepted for that exact
revision; its goal was not automatically completed. This is a local integration
test, not evidence of production transfer or physical iPhone execution.

## Regression and migration checks

- Project worker: 369 passed, 5 optional skipped; redundant-read regressions
  failed before the fix, including the two prior continuation cases.
- Swift worker and transport: 71 passed, with real compiler checks.
- Native transfer API: 21 non-compiler cases and one real compiler case passed.
- Parent cancellation: 3 failures reproduced before correction, then 29 cases
  including neighboring cancellation/recovery tests passed.
- Planner: 215 passed. Executor diagnostics: 60 passed.
- Distributed state, migrations, project progress and Swift dispatch: 179 passed.
- Context/memory regression selection: 65 passed.
- Mobile: 789 tests in 53 suites, TypeScript and ESLint passed.
- Compiled Swift model-store tests: 20 passed, including existing E5 records and
  incompatible configurations. Server Ruff and mypy passed all 74 app modules.

A coherent read-only copy of the production database was migrated from schema24
to main's schema26. All **56 pre-existing tables were byte-for-byte equivalent
at row-serialization level**, with no foreign-key violations and integrity `ok`.
Only context snapshots, context compactions and native validation grants were
added. The live database was not migrated during this rehearsal. A production
rollout needs a compatible recovery binary; schema24 code cannot reopen schema26.

The first unsigned Debug app build stopped with `No space left on device`, not
a source compilation diagnostic. Only reproducible DerivedData from the earlier
September21 TestFlight build was removed; its archive, IPA, source and receipts
remain. Subsequent build/deployment evidence is recorded separately after it
finishes. No E5 inference on a physical iPhone is claimed by these tests.
