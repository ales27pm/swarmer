# Project progress and lifetime revisions — 22 September 2026

## Observed failure

Read-only inspection of the reported project confirmed three independent issues:

- Six successive iterations alternated focused reads and rejected model output,
  retaining the same file digest. Free model messages claimed implementation even
  on read-only snapshots. Rejected output itself was correctly refused.
- Rejected/incomplete iterations continued consuming the existing budget. The
  previous special pause covered consecutive timeouts only; intervening reads
  could not establish actual implementation progress.
- The persistent project reached revision 100. Its next payload incorrectly used
  lifetime revision + 1 as the per-goal iteration, violating the maximum of 100.
  The goal-runtime scheduler retried before dispatch; no new model job was created.

The inspection performed no inference, executed no generated project code, and
did not resume, cancel or otherwise change the user's goal. The accepted file
change preceding the loop does not establish the functional claims made by the
model. Existing check receipts are not proof of a native iOS build.

## Change

The control plane now describes accepted file differences and check receipts
directly. Read requests no longer carry free implementation claims. Private job
results retain the original report; existing revisions and conversation history
are not rewritten. Public clarification questions and exact known worker error
diagnostics retain their compatible wording.

Three non-read iterations without a file change or new passing check pause the
goal without creating a clarification question or dispatching a fourth attempt.
Intermediate reads do not reset this count. A real file change, new passing check
or new user instruction revision resets it. Check duration/output churn and model
prose do not count as progress. Valid check-only completion can still reach review.
The pause preserves charged model calls, existing files, receipts and approvals.
The maintenance lease is rechecked before committing the goal projection.

Payload iteration now counts the current goal's recorded revisions, independently
of the project's lifetime revision. The actual base revision ID and file digest
remain unchanged; the iteration and model-call limits are not increased.

This is a server-only change: no worker source, model profile, environment, public
schema, database schema or TestFlight binary change is required.

## Verification

- Final focused project/runtime/recovery/publication suite: **109 passed**.
- Full backend suite: **1,572 passed, nine skipped, one pre-existing failure** in
  `test_project_plan_shape`, whose fixture still uses public `required_skill`
  instead of private wire field `00_required_skill`. This file is unchanged.
- The final focused run additionally covers the new lifetime-revision and lease
  regressions and the public clarification compatibility adjustment.
- Canonical Ruff lint/format pass for all seven changed Python files; strict mypy
  passes all 66 application source files.
- Regression fixtures use benign CRM data without model/network calls or executing
  generated project code. Lifetime tests demonstrate that the old formula rejects
  101 while the current goal receives iteration 2 and its continuation iteration 1.
- Lease-expiry testing preserves the already captured snapshot but rolls back the
  complete goal/node/message/task projection, without another dispatch.

Private evidence is in `~/Library/Logs/SwarmerDeploy/project-progress-20260922/`.

## Deployment and independent runtime verification

Source commit `fed4aa51260bbf47041d930e2fb0dd9e86380572` was pushed to both
`origin/main` and `vibecode/main`. The supervised Ubuntu activation completed once
with exit code 0 and phase `complete`; the API and five workers were healthy at
11:21:30 UTC. Only the API release changed. All six services had planned restarts.

The release wheel SHA-256 is
`2c14f031f98981c7ae1e9936a79f7075926066a4925052275e63869d0569c602`.
Its 67 application files match the pinned source and installed package. The
private database initialization rehearsal preserved schema 24 and all 57 tables;
parsed settings and the live environment file were unchanged.

An independent, read-only check at **11:23:49 UTC** confirmed:

- The running API uses the expected release and all three changed runtime source
  hashes match the qualified files. Health reports `ok`.
- All six services are active; all five workers have fresh heartbeats under three
  seconds old, with their previous identities and configuration.
- All 35 protected fingerprints match: complete contents of 34 user/history
  tables plus their protected sequences, including 317 tasks and 240 project
  revisions. All eight active-work admission gates remain zero.
- The reported goal remains `budget_exhausted`, with 17 model calls. It reached
  its existing 1,800-second limit naturally at 11:11:08 UTC, before deployment.
  Its project's 100 revisions remain intact. No goal was resumed, no inference
  was requested, and no database was restored.

Private receipts are under the evidence directory's `deploy/` subdirectory:

| Receipt | SHA-256 |
| --- | --- |
| `baseline.json` | `d24737ab79a2013ef7a2ee909dc1ba0cd4c08e0e00e6493593c355d98aa8cdca` |
| `cutover.json` | `a18b6e0deeca2047c302851a8bf0eb1a3c85aa23c867dd05264efa8a07fb575e` |
| `independent-verification.json` | `f9ce3363a1308e7e6fca76718f950b0f49b848188baefb6eec378ed7e181c9eb` |

This verifies the generic runtime correction and deployment, not the generated
project's functional claims or a new end-to-end model run. No new iOS/TestFlight
binary is needed for this server-side change.
