# Effective project steps and independent CRM qualification

## Confirmed change

Two reproduced worker defects are corrected:

- A first model response containing no files no longer runs checks against an
  empty workspace. The worker preserves the previous snapshot and receipts and
  requests one complete source module. Genuine clarification remains supported.
- After an actual Python or Node receipt reports zero tests, the mutation schema
  requires an edit. The parser also rejects an empty mutation. Reading existing
  source remains available, so a model can obtain the API before writing tests.

Neither change fabricates tests, resets a budget, performs an internal retry,
changes the model, or resumes a user project. Creating an edit alone does not
prove that the requested functionality works.

Validation: main project-worker suite **275 passed, 5 skipped**; deployed-base
suite **271 passed, 5 skipped**. Four added parametrized regressions failed on
the old implementation. Ruff and strict worker mypy passed. Deployment fault
injection: **10 passed**, including active-work admission races, configuration
drift, stop failure, recovery, and incorrect archive rejection.

## User-authorized cancellation

At 2026-09-22 23:34:41 UTC the installed canonical GoalManager cancelled
`goal_a68959bd98214d18a077129494f37ce2` following the user's explicit instruction
to stop while keeping saved files. No application lifespan, new authentication
principal, or raw SQL mutation was used. The running job lease was fenced.
All **133 previously stored project revisions** had identical snapshot hashes
after cancellation. Earlier completed jobs and project files were preserved.
The cancelled goal was not resumed.

## Benign reference case and limits

`evals/projects/crm/` defines an offline SQLite CRM with contacts, quotes,
calendar records and email drafts. `scripts/qualify_crm_snapshot.py` runs its
fixed twelve acceptance tests in the existing restricted Docker runtime. It
excludes generated tests and pytest configuration; generated application code
is never imported on the host. A deliberately incomplete implementation was
correctly rejected: **12 tests executed, 11 failed**.

This is a guided reference workflow, not a passing autonomous application
benchmark. Current real-model qualification **failed**:

| Probe | Observed result |
| --- | --- |
| Existing 30B model, three calls | Initial 2,000-token response was incomplete after 164.51 s; a later storage module compiled; generated tests failed on a missing import. |
| Candidate standard 7B, old worker | Empty first response created misleading no-test diagnostics; next response wrote tests before application source existed. |
| Candidate standard 7B, diagnostic repair sequence | Empty response rejected without Docker execution; a storage module was accepted; the next empty test mutation motivated the second fix. After that fix, an actual test was generated but used a closed database. Its repair changed the wrong module and introduced a syntax error; a subsequent identical patch was rejected. |

The final 7B snapshot contained only a storage module and its failing test.
Independent acceptance failed at compilation/collection: **0 acceptance tests
executed**, one reported build failure. Contacts, quotes, events and draft-email
features were not demonstrated. The 7B model was **not promoted** to production.
The corrected probe used evolving code and operator instructions; its timings
are diagnostic measurements, not a controlled model ranking.

An earlier probe directory named `crm-reference-7b-fixed-20260922` is explicitly
invalid: staging its patch failed before that probe ran. It is excluded from
qualification evidence. No generated exploit project was resumed or used as
the acceptance workload.

## Deployment and independent runtime proof

Only `workers/project-worker/project_worker.py` and its release manifest changed
in production. Hotfix commit: `6752f52e3b5dc5a21da538f3393e8877ae23240b`.
All other deployed worker sources were copied byte-for-byte from the existing
immutable release; pending main-branch API and runtime changes were excluded.

- Release: `6752f52e3b5dc5a21da538f3393e8877ae23240b-323269fdf972`.
- Worker source SHA-256: `3df980d08c6f336064f160765156d4ea3fd2ab0d05ca044b3510f6991cc13a83`.
- Model remains `swarmer-project-qwen3-coder-heretic:30b-32k-d2d985e`.
- Runtime image remains `sha256:e3f3afcfd536422e962c3a58217eece5c4430ca46ed96ce06464cce8adf21082`.
- Admission was checked before interruption and again under a SQLite writer
  reservation while the API was frozen. A coherent private backup was taken;
  it was not restored. Only the project worker was stopped and started.

The initial deployment verification failed because its heartbeat probe selected
an obsolete agent ID from `credential.json`. The actual worker uses the unchanged
`worker.env` identity. The rollout unit's failure is retained as evidence; it
must not be mistaken for an uninterrupted successful verification command.

An independent read-only check at **2026-09-22 23:45:30 UTC** verified the active
service binding, new worker PID, actual sandbox Python process and mounted
source hash, unchanged process model/runtime environment, fresh authenticated
heartbeat for the configured identity, healthy API, and unchanged PIDs/bindings
for the API and other four workers. All **35 protected history fingerprints**
were unchanged, including **273 stored revisions** across projects. There were
zero active goals, jobs, model calls, nodes or tools.

Private evidence is retained on Ubuntu under
`~/.local/state/swarmer-qualifications/project-effective-steps-20260922/`
(`baseline.json`, `cutover.json`, `independent-verification.json`) and
`crm-reference-7b-qualified-20260922/` (cancellation, model receipts and failed
acceptance). Reuse of the original rollout helper requires correcting its stale
credential-file identity assumption; do not replay it or rewrite its evidence.

No new iOS binary was required for this server-side change. No physical-device
or general project-completion guarantee follows from these checks.
