# Planner validation and recovery diagnostics — 2026-09-23

## Observed failure

The reported goal remained in planning with no persisted nodes. Read-only production
inspection confirmed repeated planner calls classified as `invalid_response`, a healthy
API, matching installed planner sources, and available worker capabilities. The previous
runtime retained neither a rejection audit record nor the underlying parser diagnostic.
The exact cause of those historical responses therefore cannot be reconstructed.
The user's goal and project files were not resumed, cancelled or modified by this work.

## Reproduced contract defect

The model generation schema accepted arbitrary `worker_arguments` objects for
non-specialist workers and synthesis, although the public validator rejected unsupported
payloads. Twenty-two planner/evaluator regression cases demonstrated this disagreement.
The narrow production release already constrained these payloads to null; this defect
in the main branch therefore does not establish the cause of the production screenshot.
The generation schema now constrains these server-derived payloads to null. Specialist
operations keep their bounded argument schemas; valid legacy public API requests remain
compatible. No invalid response is silently repaired or accepted.

## Diagnostic and recovery changes

- The JSON, proposal, dependency, capability and budget validators attach bounded,
  server-defined diagnostic codes. Raw model text and exception identifiers are not
  copied into the diagnostic, public reason or audit payload.
- A rejected planner call records one `goal.plan.rejected` hash-chained audit event in
  the same transaction as the failed call and recoverable goal state. Expired calls,
  terminal goals and superseded conversations cannot overwrite newer state.
- A later, separately charged planner call can receive a fixed corrective instruction
  for the latest rejected call in the same conversation. Successful, transport-failed,
  old-conversation and unknown diagnostics do not supply instructions. Existing cooldown,
  call accounting, capability validation and approval requirements are unchanged.
- The iOS goal screen retains the server diagnostic after refresh and reopening instead
  of hiding it whenever the generic invalid-plan notice is present.

## Verification

Initial diagnostic regressions failed before implementation. A separate regression also
demonstrated the stale-conversation overwrite before its explicit fence was added.
The focused server integration suite passed 79 tests. The generation-schema suite and
neighboring planner/evaluator contracts passed 195 tests. The mobile goal screen suite
passed 56 tests, including initial load, refresh and reopening without an implicit retry.
The integrated server suite passed **598 tests** in 155.89 seconds. Ruff, formatting,
five-module mypy and the diff checks passed. Independent review found no blocking issue.
The root reran the mobile suite: **56 tests passed**, including refresh/reopening.

The minimal production backport passed **413 API tests** and **77 deployment-helper
tests**. Two evaluator `maxLength` assertions (the 4000/4001 cases) also fail on the
unchanged production base and were excluded from that backport suite. They concern the
pre-existing narrow Swift schema, not this diagnostic patch. Main's integrated suite
above includes its corresponding evaluator tests. Independent comparison of all subsets
of the 12 existing capabilities plus an unknown-skill case found **4097 identical planner
formats** and **8194 identical node schemas** (planner/evaluator ordering). The narrow
production backport does not expand capabilities.

## Live observations before deployment

The reported goal naturally progressed to an accepted plan and worker dispatch, then
the evaluator declared it failed at **2026-09-23 21:49:17 UTC**, before this deployment.
Its stored failure reason equals the evaluator summary; this is an evaluator verdict,
not independently established evidence of an infrastructure exception. No operator
resume, cancellation, tool execution or project-file change was made for that goal.

An isolated benign Swift notes-app planning probe ran against the original production
provider at **21:56:16–21:56:36 UTC**: one request to
`swarmer-research-qwen35:9b-8k-6488c96fa5fa`, HTTP 200, `finish_reason=stop`,
2074 prompt tokens and 239 completion tokens. The server accepted one
`code.build_project` node. No goal, task, tool call or database write was created by
the probe. This did **not** reproduce the historical rejection and proves only plan
contract acceptance, not that the proposed application was built.

Private probe evidence is under
`~/Library/Logs/SwarmerDeploy/planner-validation-20260923/baseline-benign-probe/`:

- Request SHA-256: `d1e6b146a7de9a8b397ef409f3ceec8ae99fee2c7c583b672e55f4f5b6b8914e`.
- Response SHA-256: `485494ed3bde1ec5fca500d46b997695f22009eed69c11109852f544db13d42a`.
- Accepted proposal SHA-256: `87c7743114141cc6db216529e873974de199b19e66fbdbfb42482a62e3da666e`.

## Server rollout

The source-only API candidate `9e33eaf1c160c05c2fdd0c8283a70d84160b42ac` was
deployed as release `9e33eaf1c160c05c2fdd0c8283a70d84160b42ac-eec7e3d78b87`.
Only five service modules changed. Database schema 26, configuration, model selection,
worker sources, identities and credentials were preserved. The compatible recovery
release contains runtime bytes identical to the previous `c519dfe` release.

The initial verification timed out waiting for the iMac Swift worker heartbeat, while
the API and five Ubuntu workers were healthy. The Swift heartbeat returned naturally
at **22:12:32 UTC** with the same process ID; no Swift restart was performed and its
temporary absence has no established root cause. Bounded forward recovery completed
without restoring the database or resetting deployment guards.

Independent read-only verification passed at **22:13:49 UTC**: candidate process and
source hashes matched, all six workers had fresh heartbeats, no work was active, and
all **37 protected history fingerprints** were unchanged. This deployment neither
resumed nor modified the reported project.

Private receipts are in
`~/Library/Logs/SwarmerDeploy/planner-validation-20260923/api-deploy/`:

- Reviewed baseline SHA-256: `54451902762e689dec7b947cd08d75d45385d97a90a2858756442ab45ad58940`.
- Completed cutover SHA-256: `234680937b1bb28cc4af21f56f7cd9c955378f08cf12b444f156b4c42a73bd85`.
- Independent verification SHA-256: `8e451b296b50545a36635b515d355cd0d67780dcf8950556485a7e2b7661498f`.

The single post-deployment benign probe completed at **22:14:35 UTC**. The same 9B
model received the server-owned `unknown_dependency` corrective hint as an explicitly
synthetic fixture and returned an accepted one-node project plan (HTTP 200, stop,
2145 prompt tokens, 266 completion tokens, 21.5 seconds). This exercises the new prompt
path but does not establish that the historical rejected response would recover.
It created no task, executed no tool and wrote no database state.

- Candidate probe receipt SHA-256: `0eed66a49cce69200e92096115c594c83416f5c18c26d86d141f5d42a82d44f0`.
- Request SHA-256: `4bc525fa36001506043972f6068ee11624cf9547227c6c3a98873456b4a61578`.
- Response SHA-256: `323f0e9158c852ad0956efe1a978d86cb28d4f6224ba0ac332537e93132476df`.
- Final private evidence manifest (77 files) SHA-256: `dc4698d28fc83d64236723ab35a82ad8fc38761f2a6138d4271e6a14406bd58e`.

A final read-only check at **22:15:42 UTC** again confirmed HTTP 200, no active work
and the same 37 protected history fingerprints.

## iOS release

Build **0.1.0 (20260923215000)**, bundle `org.27pm.mongars`, was archived from
exact main commit `fc6847955ffed894ff67aa94e521d1cd68c00e75`. The 186 source files
matched both Git and the archive manifest. The archive completed successfully in
1589.212 seconds; distribution export completed in 13.720 seconds.

The independent static IPA audit passed: distribution signature and profile agree,
the application dSYM matches, the 12 pinned Swift dependencies are unchanged, and
the native modules compile in optimized Release mode without the Debug API listener.
The three previously missing prebuilt framework dSYMs (React,
ReactNativeDependencies, Hermes) remain an explicit symbolication limitation.
No physical iPhone installation, launch or runtime test was performed in this release.

- IPA size: **43,004,466 bytes**.
- IPA SHA-256: `9d12b585f69ff23943848a729eb7e4e4439734fd68214a784b69ac7dc7942b1a`.
- Independent IPA audit SHA-256: `56c2d63f6b321796bed77f79911791e447a464936a7b46e891532ed93235f184`.
- Private release lane: `~/Library/Developer/Xcode/SwarmerTestFlight/20260923215000/`.

The single Apple upload completed successfully in 70.455 seconds. App Store Connect
readback at **22:27:35 UTC** confirmed `VALID`, `IN_BETA_TESTING`, not expired, and
exact membership in the internal **27pm** group (`found=true`, `scanComplete=true`).
The `fr-CA` test notes were applied and read back exactly. This is internal TestFlight
availability, not an App Store publication or a physical-device test.

- App Store Connect status receipt SHA-256: `473adaad02ef856791bef70e8c2d6d89b50adcfa939a3d750316bf87251194b1`.
- French notes receipt SHA-256: `8c1a98d61f15c06c3ea3c82c9bde260fa01b3bbd6a3e9391c4f1efef95aa049b`.

This corrects a demonstrated schema inconsistency and makes future rejections diagnosable;
it is not evidence that the unseen historical response had that precise defect, nor that
an arbitrary generated project will complete successfully.
