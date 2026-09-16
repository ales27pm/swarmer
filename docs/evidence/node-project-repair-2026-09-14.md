# Node project repair — 14 September 2026

## Reproduced symptom

The physical iPhone screenshot showed `npm run build` failing because
`package.json` did not exist, followed by `node --test` collecting zero tests
and exiting with code 5. A read-only inspection of the matching Ubuntu project
confirmed these were real results from incomplete intermediate revisions.

| Revision | Captured at (UTC) | Cumulative files | Result |
| --- | --- | --- | --- |
| 1 | 06:22:11 | None | Clarification |
| 2 | 06:24:02 | `README.md`, 535 bytes | Node manifest absent; zero tests |
| 3 | 06:26:13 | `README.md`, 560 bytes | Same failures |
| 4 | 06:29:10 | `README.md`, 589 bytes | Same failures |
| 5 | 06:33:00 | `README.md`, 1,023 bytes | Same missing manifest and tests |
| 6 | 06:36:53 | `README.md`, 1,146 bytes | Same missing manifest and tests |

At 06:30:16 UTC the goal was still running, with the fifth project job active.
The fourth result described another documentation edit and a future manifest
creation. It had not delivered the manifest. The private inspection is retained
at `/private/tmp/swarmer-project-node-checks-20260914.json`.

At the original inspection, the running project worker source matched the repository baseline. The check
harness copies the supplied source into `/workspace/project`, then reads the
manifest there. No manifest existed elsewhere in these snapshots. The runtime
image is pinned to
`sha256:e3f3afcfd536422e962c3a58217eece5c4430ca46ed96ce06464cce8adf21082`.
This is not evidence of an incorrect runner working directory.

## Cause and scope

Failed checks already downgrade a model's premature `complete` result to
`continue`. The control plane had correctly continued this project and the
iPhone correctly displayed its failing checks. These protections are retained.

The generator had special guidance for pytest collecting no tests, but Node's
equivalent failure used the generic repair prompt, which preferred patches to
existing files. With only a README present, successive responses kept modifying
documentation rather than supplying the missing Node project files.

The correction is limited to the project generator's repair instructions and
output schema, with regression tests and worker documentation. It does not
change the fixed check commands, runtime image, sandbox, model-call budget,
project approval, application API or iPhone binary.

After a failed npm build with no root manifest, a mutation retaining runtime
`node` or `python_node` must create `package.json` as its single file change.
The model grammar expresses this using the existing `oneOf`/`enum` forms. A
separate post-parse guard rejects nonconforming responses before applying files
or running checks. Python remains available for applications that serve static
browser assets without a Node build, and source inspection remains possible.

An unambiguous empty Node TAP result, without another failing check, selects explicit guidance to create real
`node:test` files against the actual application API. Skipped, failing,
cancelled or ambiguous reports retain the ordinary repair path. This guidance
does not certify the quality of future generated tests.

## Qualification

Three regression checks failed on the original generator for the intended
missing behavior. After the correction, the project-worker suite passes
**224 tests**, with **5 Docker checks skipped locally**. Ruff format/check,
strict mypy over six worker sources, Bandit and the independent review pass.

ROOT separately ran the complete worker gate from `scripts/check.sh`:
**437 passed, 5 skipped in 3.53 seconds**, exit zero. Its log is
`/private/tmp/swarmer-node-repair-all-workers-final.log`. An initial invocation
used an incorrect file-worker test filename and collected no tests; it was
corrected before this successful gate. The focused regression and static logs
are `/private/tmp/swarmer-node-project-*.log`.

The frozen source commit is `2c46e01b33f8e8218edd12c27e92c9329b930f2f`.
Both GitHub and Vibecode `main` were pushed without force and independently
read back at that commit. The project generator SHA256 is
`fc2a136cba194aac15933d3c4255ea4d743adcc59f74a98a3e90308f04532139`.

## Real-model qualification — 16 September, UTC

A fresh read-only snapshot at **02:19:00 UTC** confirmed that the original goal
had naturally reached `budget_exhausted` at **14 September, 06:50:42 UTC**,
after 10 steps and 23 model calls. It was not cancelled, restarted or modified
for this repair. No executable goal, active job, lease or reserved model call
was present. The earlier temporary release kit was no longer available, so a
new private kit was reconstructed from the frozen Git sources and the actual
running worker inventory, rather than treating the earlier preparation as a
completed deployment.

The isolated fixture ran from **02:23:55 to 02:25:12 UTC**, with exactly **one**
real call to the existing pinned
`swarmer-project-qwen3-coder:30b-32k-06c1097e` model. The complete qualification
took **77.423 seconds**. It used the candidate generator and the unchanged
production Docker runtime to repair a benign dependency-free contact-name
normalizer consisting only of `README.md` and `app.js`.

The initial checks were real runner results: missing `package.json` caused the
npm build to fail, while Node collected zero tests and the harness returned
exit 5. The single model iteration created only `package.json`, with no
dependencies or development dependencies, and preserved both existing files
byte for byte. The real npm build then passed. The zero-test check correctly
remained failed with exit 5 and the iteration remained `continue`.

Two functional Node tests were then added **independently by the operator's
qualification harness**, not generated by the model. They tested whitespace
normalization and preservation of accented spelling/case; both passed, with
`tests_executed=2`, `test_failures=0`, and a successful build. A separate copy
with deliberately invalid JavaScript syntax failed the same build command,
demonstrating that the script performs a meaningful check rather than merely
printing success. These tests qualify this fixture and runner behavior; they
do not prove that the model can generate adequate tests for arbitrary projects.

No app goal, task, job, approval or control-plane mutation was created by the
fixture. Seventeen protected goal/project/job/task table fingerprints were
identical before and after, and the fixture's Docker containers and networks
were removed. The model was allowed to unload naturally before deployment;
no unload or model reconfiguration request was sent.

## Worker-only deployment — 16 September, 02:36:08 UTC

The package contains exactly eight launched worker source files. Only
`workers/project-worker/project_worker.py` comes from `2c46e01b`; the other
seven retain the exact bytes of the existing `adc2bd65` worker release. In
particular, the older code-worker source remains
`0c382c258af6990fff726b46bf70e579156be92db2beaa74dd7fd03f9b2801a7`.
The package was staged immutably and independently reviewed before execution.
Five package/staging tests and sixteen synthetic cutover/recovery tests passed.

A fresh complete quiescence gate and empty Ollama resident-model list passed
immediately before the single cutover. The helper stopped the API and project
worker together, verified their cgroups were empty, rechecked the authoritative
terminal state, and created a coherent SQLite backup. It then atomically
published only the project worker's systemd drop-in and restarted the same API
with the new worker. The operation took **6.802 seconds**, ending with
`result=deployed`.

The API remained on **`f72eb2ff9232a15ed0d2d84b737682055701e902`**, with its
source inventory, unit, configuration and release manifest unchanged. Its new
PID was **1423018**. The project worker's new PID was **1423019**, with the
candidate source path and process invocation verified. Its existing agent
identity reported a fresh `online` heartbeat at **02:36:07.783775 UTC**.
The model binding, credentials, runtime image and seven other worker sources
were unchanged. The legacy worker remained inactive and disabled.

Local HTTP and HTTPS health both returned `ok`, version `0.14.2`. An existing
worker-authenticated read returned 200; unauthenticated protected routes
returned 401. This is server/worker authentication evidence, not an additional
physical iPhone test or device-token validation.

Schema **24** was unchanged. The stopped backup and live database comparison
reported **`changed_protected_tables=[]`**; only the previously defined liveness
columns and disposable operational tables were excluded. The post-start gate
again found no executable goals, active tasks/jobs/nodes/tool calls/approvals,
capability requests, reserved model calls, memory queries, future leases or
pending outbox events. No database restoration/reset, schema migration, API
manifest rewrite, goal retry or forced cancellation occurred.

The reviewed recovery helper permits only a worker-code rollback after proving
that no new protected work was accepted and both service cgroups are empty.
Otherwise it preserves the database and reports recovery forward as required.
No rollback was needed in this deployment; recovery branches were exercised by
synthetic tests, not deliberate production failures.

The active worker release is
`/home/ales27pm/.local/share/swarmer-project-worker/releases/2c46e01b33f8e8218edd12c27e92c9329b930f2f-fc2a136cba19`.
Private artifacts are retained under
`/private/tmp/swarmer-project-worker-qualification-20260915`; the matching remote
qualification/cutover receipts and backup are under
`/home/ales27pm/.local/state/swarmer-qualifications/node-bootstrap-2c46e01b-20260915`.

| Artifact | SHA256 |
| --- | --- |
| Worker manifest, `package/release.json` | `0d29caa2740a73ddf42cfcc926dd363082f02950aa59ba0d653a6283fb0e14a1` |
| Source bundle, `worker.tar` | `3a6614615712495aba6d304884f977b4af9b86e4ac5d0b7053961d931cf34c51` |
| Real qualification, `qualification-result.json` | `71986b669f79ca2815988a63f44fd60fe0063dd308d533893ced45b14d6403a6` |
| Reviewed/executed helper, `cutover.py` | `73850a38a90d4f95c887f43e37e0c1418a74ffc8be2a55e7552eeebbf62d7d4d` |
| Exact remote receipt, local `cutover-receipt.json` | `2585ade4aee6f5515d6c89bedc5e01ffc192be44d5e294d9d1fcf6443e35abcf` |
| Remote `cutover/stopped-state.db` backup | `103fa4ecd805714f075f1d50bd0adde3e3988c7c642d1d54f9a453d5a88cfc5f` |
| Unchanged API manifest | `2bc0b44afaa4acbd944884b872bc2c35715b08f4ac6b9dab7cc64caee37480d1` |

This deployment repairs the generic Node manifest/test guidance. It neither
completes the original application nor retroactively changes its revisions or
failing check history. No new iPhone binary or TestFlight submission was needed
for this worker-only change.
