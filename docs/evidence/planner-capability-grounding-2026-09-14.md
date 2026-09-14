# Planner capability grounding — 14 September 2026 UTC

A device report showed a worker node as dispatched while its independent synthesis
node was skipped for lack of evidence. A read-only runtime audit found that the
worker job was queued, unclaimed and had never attempted execution. Its
`code.generate_python` skill belonged to a legacy worker whose last heartbeat was
about 67 hours old. The active worker offered `code.build_project` instead.

The saved planner context contained only the project worker. The planner still
selected the legacy skill, and the server accepted it. Both nodes had no
dependencies, so the synthesis correctly ran immediately and found no input.
The reported two model credits represented one completed planner call and one
reserved worker credit; they did not prove two generations.

The audit did not start, cancel, replan or execute the reported task, and did not
enable the disabled legacy worker. Raw runtime snapshots remain private.

## Correction

- Agent cards now carry structured skills from fresh, protocol-compatible workers
  in online or busy state. Durable policy filters these skills before card and
  token budgeting. Additional narrative cards cannot supply capabilities.
- The planner's output schema permits only the skills in the cards actually
  presented. Its response is independently checked against the same snapshot.
  A worker arriving during inference cannot retroactively authorize a proposal.
- Initial plans, reviewed iPhone/manual plans, replans and evaluator additions
  recheck current capabilities inside the transaction that inserts their nodes.
  A disappearing worker or changed policy causes rejection before execution
  records are written.
- Evaluator output uses its saved capability snapshot as well. Unknown and empty
  capability lists retain distinct meanings; production insertion fails closed
  when its current policy snapshot is unavailable.
- Rejected supplied plans preserve the pending goal and graph. Model attempts
  remain accounted for and receive an explicit invalid-response state.

This is a server-only change with no schema migration. It does not modify the
already persisted malformed plan or establish physical-device execution.

The existing runtime reached the reported goal's original time limit at
03:15:50.556031 UTC. It marked the goal `budget_exhausted` and cancelled its
unclaimed job without any intervention from this investigation. The read-only
03:16:29 UTC snapshot found no active work, model calls, leases or pending outbox.

## Validation

The initial full server run had 1,168 passing tests, nine skips and two failures
in old fixtures: a manual worker plan without any registered worker, and provider
cards that carried skills only in prose. The fixtures now supply a fresh worker
and structured capabilities respectively; both affected suites pass (12 tests).
The final complete run passes: **1,173 passed, nine skipped** in 222.73 seconds.
Its log is retained at
`/private/tmp/swarmer-capability-grounding-pytest-final.log`.

One real, bounded planner inference used the modified provider with a benign
Python book-catalogue request and a single `code.build_project` capability. The
installed `swarmer-project-qwen2.5-coder:7b-32k` model accepted the schema and
returned HTTP 200 in 11.736 seconds. Its plan contained exactly one project
worker, no dependencies, and retained CRUD, title/author search, local persistence
and automated-test requirements in the worker objective. This proves planning
compatibility, not project implementation. The probe did not use GoalManager,
write the live database or create a job; its dedicated SSH tunnel was closed.

The private probe result is
`/private/tmp/swarmer-planner-capability-real-20260914/result.json`, SHA256
`93182317c42fdf75afbe0d2a3910a959c85565f1505d6b599a9322970d5824f5`.
It records the working-tree source hashes, with `79525cdc` as the parent commit.
The tested planner provider hash is
`361386cdfd56673fb56dbab7d86862a051cd42f38be0557c9e9366c964aedc32`;
the plan-validation hash is
`9762158dd2455e81fe316b627d7e070be6b5ced13622bf2594bc4953deca6687`.

Formatting, Ruff, strict typing of 61 modules, Bandit and the committed OpenAPI
contract check pass. No mobile source, dependency, model or worker configuration
changed.

Independent reviews covered GoalManager/plan-validation transaction and accounting
paths, then ContextBuilder/provider trust and schema boundaries. Neither review
identified a material defect. These were static reviews with test inspection,
not additional production or physical-device executions.

## Deployment

Source commit `581fb4cbc32a897b17f9b046dd1896eeb8b26370` was pushed without force
and independently read back on both GitHub and Vibecode `main`. The subsequent
evidence-only commit does not change the deployed application.

Cutover completed at **03:26:56.186677 UTC**. Ubuntu runs API 0.14.2 from
`/home/ales27pm/.local/share/swarmer-control-plane/releases/581fb4cbc32a897b17f9b046dd1896eeb8b26370-915ffb011df3`.
The manifest SHA256 is
`124a523fb117354d36591a1f6e7b293f75131b58bf31f1809c1bfd9750f0596d`;
the transfer bundle SHA256 is
`5cc88f2440f96340736f3da5c4e57a0d72b4a2d3c47407d3dc6c990394dbd237`.
Git, wheel and installed application sources were compared exactly.

Canonical initialization on a private coherent database copy preserved all 57
tables, including `sqlite_sequence`, and every schema object and table content.
The live database stayed at schema 24. The deployment did not run a migration or
restore a database. Post-start protected-table comparison found no differences.

The existing coupled API and project-worker services restarted with the same
worker identity, source, credentials and model bindings. The legacy code-worker
service remains disabled and inactive. Local and HTTPS health returned 200,
authenticated worker GET returned 200, and unauthenticated private requests
returned 401. The expired goal and its cancelled, never-claimed job matched the
fresh terminal baseline exactly. No work or model call was started by deployment.

A second read-only probe, independent of the deployment helper, passed at
03:27:25 UTC. It verified the exact manifest and process executable, schema 24,
both health endpoints, an online project worker with a 4.332-second heartbeat age,
and the original job's zero attempts. Its private receipt is
`/private/tmp/swarmer-capability-grounding-root-verification.json`.

The stopped-state backup is retained in
`/home/ales27pm/.local/state/swarmer-control-plane/backups/20260914T032650Z-api-hotfix-581fb4cb`;
its SQLite SHA256 is
`a0e19cbbc8b615feed991792d8006ed485f1f830abd9aaa5574fb07cac9a8e35`.
Private stage, cutover and verification receipts are retained under
`/private/tmp/swarmer-capability-grounding-release-20260914/backend-kit/release-581fb4cbc32a/`.

[GitHub run 34802298833](https://github.com/ales27pm/swarmer/actions/runs/34802298833)
did not start its required job because the account is locked for a billing issue.
The passing local gates and live deployment checks are distinct from that CI run.

This server correction applies to newly accepted plans. It does not establish
successful implementation of the reported task or add new physical-iPhone
validation beyond the boundaries recorded in
[the preceding app release](shared-project-memory-release-2026-09-14.md).
