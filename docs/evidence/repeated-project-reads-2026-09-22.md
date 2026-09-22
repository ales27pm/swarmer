# Repeated project reads — 22 September 2026

## Diagnosis

Read-only Ubuntu inspection matched the reported repeated-read conversation. The
current goal recorded fourteen revisions with identical source digests. Its
requests repeatedly selected the same manifest path; reading did not establish
an accepted source edit or a new passing check. No generated project code was
executed by this investigation and no project was resumed or cancelled.

The deployed progress guard skipped every `focus_paths` result and did not run
its stall check when the latest result was a read. Consequently, a read-only
loop could consume the entire model-call budget.

## Correction

The generic runtime now allows a first inspection of each selection and pauses
after three repeated selections without source changes or new passing checks.
Selections are canonicalized independently of path order; alternating already
requested selections does not reset the count. Distinct first reads remain
allowed. The existing three failed-attempt guard remains independent, including
across intermediate reads.

File/check progress or a new user instruction revision resets the counters.
Stale results cannot pause a newer instruction. The pause records an explicit
read-loop diagnostic, preserves files, receipts and charged calls, creates no
clarification question or approval, and dispatches no further iteration.
Repeated result delivery and reconciliation remain idempotent. This is a
consumption guard, not proof that the model can complete the requested project.

## Validation

- Three added regression scenarios failed on the original implementation.
- Current-main focused checks: 62 passed, followed by both maintenance-lease
  variants passing after adding read-loop lease coverage.
- Exact release source: 117 project, conversation, recovery, progress and
  publication tests passed, including the added read-loop regressions.
- Ruff lint/format passed; strict mypy passed all 66 release application files
  and all 70 current-main application files. Bandit passed the changed module.
- Deployment helper tests: 24 passed (admission, fencing, supervision, recovery).
- Candidate package executed the guard against the real production history
  through a read-only SQLite connection and detected the reported loop, without
  inference or production writes.
- A private-copy initialization rehearsal preserved schema 24 and all 57 tables.
  All 67 installed package files matched the release source/wheel; only the
  goal-manager runtime source changed. Settings and worker bindings are preserved.

An initial worktree test invocation accidentally imported the editable main
package from the shared virtual environment. That invalid mixed-tree run was
discarded; the qualified run used `python -m pytest` and verified the worktree
module path before execution.

## Release scope

Main implementation: `c43ca46`. Isolated deployment source:
`f52f7c6dadcd2ab86f83724210a309bd118b6f0d`, based on the currently deployed
`fed4aa51260bbf47041d930e2fb0dd9e86380572`.

Wheel SHA-256:
`34f42c81a5517f311f7f2e76f2f1860da4553fd5689a44dc79ff222f48c6b9e1`.

Only this runtime guard is included. The context/memory/specialist rollout remains
separate. No TestFlight binary change is required.

## Activation and independent verification

Supervised activation completed with exit code 0. An independent read-only check
at **2026-09-22T23:03:28.802712+00:00** confirmed the expected package/source hashes,
healthy API, all six active services, five worker heartbeats under five seconds,
unchanged environment/worker bindings, and all 35 protected-history fingerprints.
No production schema migration, goal resumption or inference was performed.

Before activation the reported execution exhausted its existing 30-call budget
naturally. Its final status and charge count remain unchanged. Future matching
read loops pause under the new guard; this delivery does not assert a successful
model-driven completion of any generated application.

Private receipts are retained in
`~/Library/Logs/SwarmerDeploy/repeated-read-20260922/deploy/`.

| Receipt | SHA-256 |
| --- | --- |
| `stage.json` | `162929e45705bb3acdb17376bc64af8ef2d78d59071e68a6db73ac185ef6bf34` |
| `baseline.json` | `93651d90832633af3e21b4a8a6001fd04151c7e3dad8bd02ece4db79393e57d2` |
| `cutover.json` | `c66604017d8728fc867f1eaf9069ea7268fd4a40eed743d52e8362af3f5a79dd` |
| `independent-verification.json` | `cb3048fa9f0c7069867a18b755ccb06a77062082186a342e8d1f3b225f6d96a9` |
| `read-only-real-history.json` | `11b1708646a4b52219f4c013c2dd13d535ce7d9bed3ddd4576a6f7283b8067ee` |
