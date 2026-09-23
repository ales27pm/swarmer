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

## Deployment evidence

Main source commit `cabec3b4bbc8c175ed74154c7a358e59b2aa782f` was pushed to both
configured remotes. The unsigned generic-iOS Debug build subsequently passed
(`ios-build-retry.log`, `BUILD SUCCEEDED`). It includes MLXEmbedders and the new
model-purpose routing; it is not a physical E5 inference test.

The read-progress worker was narrowly backported onto the deployed worker as
`57d8c1aeb4060e6e90e8166864fda813c351e8d6`. Its single runtime-file change preserves
the deployed project contract, model, sandbox and all seven other runtime files.
365 backport tests passed (5 optional skips). Production activation and independent
verification completed at 2026-09-23T03:40:37Z with a fresh heartbeat, all mounted
sources checked and all 35 protected history fingerprints unchanged.

The first rollout attempt stopped before changing any binding because freezing
an existing SQLite writer retained its lock. Recovery resumed the existing API
and worker; no database was restored. A separately retained second attempt took
the database write reservation before freezing the API. Its 47 helper tests
include a regression proving that ordering. Source and service bindings were
then verified independently. The existing rejected task was preserved through an
exact row/audit fingerprint, without exempting running work or cancelling it.

The API was narrowly backported as `c7d36e812c60e9969210a98530e3589dfb74575f`,
with a schema26-compatible fallback `9f207cebecc960f840575add9089fcca3e812e03`.
Remote staging proved source/wheel parity, unchanged dependency/settings/policy
metadata, additive migration and a fallback that leaves all tables unchanged.
The API lane passed 83 fault-injection and migration tests before execution.

API cutover and independent verification completed at 2026-09-23T03:47:33Z.
All six existing agents supplied fresh authenticated heartbeats; all 37 protected
fingerprints, worker credentials/bindings and external policy epoch6 were
preserved. The live migration added only `swift_project_validations`, without
promoting main's unrelated context/compaction changes. GET `/memory/status`
returned authentication-required401 rather than method-not-allowed405. At this
stage this proves the authenticated route exists, not embedding inference or a
successful authenticated status fetch.

The iMac worker was switched to immutable source `cabec3b` and a private staging
root while preserving its enrollment and SSH tunnel. The first activation receipt
failed its immediate process-argument comparison after launchd bootstrap; it did
not capture the transient arguments, so their cause is not asserted. Independent
inspection at 2026-09-23T03:51:40Z confirmed the exact candidate argv/plist, source
hashes, unchanged credentials/tunnel, no native job, and a post-activation
heartbeat. No second restart was needed. The failed receipt remains retained
beside `mac-swift/receipt-independent-current.json`.

A subsequent production qualification used a newly paired, explicitly named test
device and a new benign Swift goal. The actual project worker generated only
`Package.swift`. The old native guard immediately paused authoring. After source
inspection, the exact stored revision was transferred under its authenticated
grant to the iMac and `swift test` ran for 8.428 seconds. It correctly failed with
an empty target: exit1, zero tests, source unchanged. This verifies real transfer
and rejection of incomplete source, not a successful Swift project. The fixture
goal was cancelled, its revision retained, its temporary device revoked (the old
bearer then returned401), and no active job remained.

That same test device fetched `/memory/status` with HTTP200. The deployed server
reported EmbeddingGemma configured but not probed; context, compaction and hybrid
features were false. Local E5 and server embedding status remain distinct.

## Incremental native authoring correction

The production probe exposed an early native guard: the first accepted Swift
file stopped later authoring iterations. The worker now preserves an explicit
native authoring state across small edits and deletions. A complete proposal
requests separate validation of the exact revision; it never records a passed
native check. Python/npm checks are not run for native drafts. Existing stall,
timeout, cancellation and source-consent limits remain enforced. Legacy results
without the marker retain their fail-closed behavior.

The regression failed before correction (7 worker and 2 server cases). Validation
passed 384 worker tests (5 optional skips), 196 server tests including a real
benign Swift compilation, Ruff, mypy for all five changed services, and diff
checks. This does not by itself qualify an autonomous model-generated project.

Main correction `34e9fae` was pushed to both remotes. Its narrow API backport is
`0748cccc9149de1990605b57c5ee79dd1b813ca6`; the compatibility recovery build is
`887072f81d822f6896d1ddfa245c7fa3b86cbf73`. Recovery accepts the new stored marker
while unconditionally pausing native authoring and omitting the marker from old
worker payloads. Both builds leave schema26 and all58 existing tables unchanged.
The API backport passed220 tests; the recovery build passed217 tests, including
four regressions for reading/applying marked snapshots with and without Swift
paths. Both passed mypy and Ruff. The deployment helper passed76 tests with one
inapplicable addition-only case skipped.

The API cutover completed successfully and independent verification at
2026-09-23T04:18:21Z confirmed all 37 protected fingerprints, six fresh worker
heartbeats, unchanged credentials/settings/policy, and no active work interrupted.
The exact reviewed baseline was
`e5c669f0f98e867c9e31b232074a35eed8f2816325d68b584f1065f3435d6386`.

The worker backport `145f79bd137226edcfbb13331a1f7cfc55a86fc0` passed380 tests
(5 optional skips). Its first deployment preparation stopped before changing
services because a reused historical guard required schema24. The new helper
adapts only that gate to schema26 while retaining the exact full-schema hash
algorithm; the historical guard is unchanged. Six additional regressions cover
schema fingerprints, changed schemas, forbidden versions and the in-memory
adapter. All55 helper tests then passed.

Worker activation was supervised successfully. Independent verification at
2026-09-23T04:24:47Z confirmed both changed runtime files, all eight mounted source
files, a fresh authenticated heartbeat, unchanged model/container/configuration,
all 37 protected history fingerprints, and no other process restart. Its baseline
was `71c36e4ee11165a2757767dd64e9e3b0557d38ed52662458dba2915edd1b6a55`.

A second production probe confirmed authoring continues past the first Swift
file, but did not finish the fixture. Its5 total calls comprised3 generation
reservations and2 embedding reservations. Only `Package.swift` was produced;
the second iteration rewrote identical content and the third changed manifest
metadata. It exhausted its operator-selected budget, with three revisions
retained. No native validation was requested for those incomplete sources.
The temporary identity was revoked and zero active jobs were confirmed at
2026-09-23T04:29:42Z.

That probe exposed a second concrete bug: the native branch returned before the
existing no-effective-operation check. An identical full-file replacement now
receives that exact actionable diagnostic, preserving source/checks and bounded
stall accounting. Empty-edit native validation requests remain valid. Generic
continuation guidance points to the next missing milestone in the current
manifest; there is no fixture-specific code. Its regression failed first, then
385 worker tests passed (5 optional skips), followed by258 focused tests and Ruff.

Main `cf95b08` was pushed to both remotes. The worker-only backport `bf73fd3`
passed381 tests (5 optional skips); its deployment helper passed53 tests. At
2026-09-23T04:33:32Z independent production verification confirmed its exact
source, all eight mounted files, fresh heartbeat, and all 37 protected history fingerprints.
API0748, model, container, settings, credentials and other processes are unchanged.
The activation baseline was
`31c9dcf8a03c9ce38c8b36397caa29437953fb7d5aceca71271425bf960ddf26`.

The next probe used5 generation steps plus5 embedding reservations (10 total),
bounded to600 seconds. It confirmed the identical-edit diagnostic in real worker
results, but still produced only a manifest: other iterations changed metadata
instead of adding source/tests. All five revisions and input payloads were
retained; no native validation was requested. At2026-09-23T04:39:38Z its temporary
identity was revoked, the old token returned401, and no active job remained.

A further continuation-context correction replaces reissuing the original
creation request with a task that starts from the accepted workspace. It retains
the original objective and latest user requirements, and may suggest the first
absent canonical file mentioned in the accepted plan. This is advisory, never
proof of completion or an execution grant. URLs, code fragments, unsafe paths,
Windows paths and existing files are excluded from those hints. There are no
qualification-specific names or deterministic generated project files.391 worker
tests passed (5 optional skips), with Ruff and diff checks clean.

The captured-input probe subsequently passed against the actual 30B model. In
one call (51.99 seconds, 258 generated tokens) it added the missing Swift source
file with a real `add` implementation and retained the existing manifest. The
worker correctly reported continued native authoring; no compiler or project
execution occurred in this isolated probe. Its receipt SHA256 is
`acb4dd6102fb5670283f46d87b676b0299ca784d140b1481e41644739ecb45dc`.

Main correction `b0bccfd` was pushed to both remotes. Its narrow worker backport
`69497914ede90220a5ed33b9378d89f2ae664ddd` passed387 tests (5 optional skips);
the deployment helper passed53 tests. Supervised activation succeeded, followed
by independent verification at2026-09-23T04:48:59Z: all eight mounted sources,
a fresh authenticated heartbeat, all 37 protected history fingerprints, and unchanged API,
model, container, configuration and unrelated processes. The reviewed baseline
was `f8353aea5bb979cd4e7991011c4c80915b4c487941c5a590d45c59b8f3bc1a3a`.

Public continuation of the retained third fixture preserved its5-step,
10-call,600-second limits and its original files. Three actual generation jobs
then added the Swift source, two XCTest cases and README in separate accepted
revisions. No source from the isolated probe was injected. The final four-file
snapshot was reviewed at SHA256
`8e2925a056213a8cfabaf4afb3788fcfe7203350d43c949187931e719ab4476a`.

This qualification exposed a dispatch race: synchronization can recover and
attach the just-queued job before dispatch's own attachment update. The latter
treated the zero-row update as cancellation even when the exact same job was
already correctly attached. The fourth generation job was cancelled before
claim. Its reservation, four worker embeddings, three executed generation calls,
and the subsequent evaluator plus embedding consumed the10-call limit.
The goal reached `budget_exhausted`; a later native validation request correctly
created no job because terminal goals cannot grant execution. All four source
files remain retained. This run proves generation progress, not compilation.

The dispatch race was reproduced using two actual manager instances against
one database. With the original predicate, both queued and already-claimed jobs
were incorrectly cancelled after recovery attached them. The correction makes
attachment idempotent under the database write lock, requiring matching goal,
node, task and job identities and active states. Replacements and terminal
parents/nodes still fence the original child. All 98 selected recovery, manager,
project, native-validation, maintenance and lease tests passed, including the
five new race cases. Ruff, formatting, mypy and diff checks passed.

## TestFlight

Build `20260922233500`, version0.1.0, was archived from exact main source
`cabec3b4bbc8c175ed74154c7a358e59b2aa782f`. Archive, export, signature and source
audits passed. The IPA is43,412,898 bytes with SHA256
`778c48814104538aebd47ee7f56a167f3883a7d77871c28c51dfdfb5a4a2f89d`.
Upload succeeded; App Store Connect confirmed `VALID` and `IN_BETA_TESTING`,
membership in internal group `27pm`, and exact French release-note readback at
2026-09-23T04:12:37Z. No external beta submission was made. The temporary signing
keychain was cleaned up. The app dSYM is verified; three precompiled vendor
dSYMs are absent and documented in the retained release receipt.

The backend-only incremental-authoring correction does not require another
mobile build. Physical-iPhone E5 inference remains unverified.
