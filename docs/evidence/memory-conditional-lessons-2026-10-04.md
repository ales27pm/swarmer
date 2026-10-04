# M09: conditional lessons from selected execution reports

The schema-34 slice adds a versioned registry of procedure candidates and their
selected execution evidence. It **does not automatically learn, promote a
procedure, or change an agent's permissions**. Worker delivery and actual runtime
qualification remain separate work. This slice is local and not deployed.

## Public behavior

An authenticated local operator can approve or revoke an execution profile. It
pins the runtime image, runner, policy, harnesses, dependencies and allowed
producer identities. A criterion mapping pins the exact oracle files, test
profiles and minimum test count. This is an operator qualification declaration,
not cryptographic attestation that a worker executed the declared process.

A paired device can propose a project-scoped lesson, assess it against existing
accepted reports, inspect it, or withdraw it. The candidate binds the goal,
conversation revision, criterion hash, source revision and profile version.
Assessment reads existing reports; it never runs commands or invokes a model.
Writes use expected-version checks, request identities and atomic audit records.
Replays return a freshly revalidated view and cannot undo withdrawal or revocation.

Assessment covers an **explicit selection of at most eight reports**, marked
`evidence_scope=explicit_selection`. It is not an exhaustive search of all project
failures. Subsequent assessments retain the existing selected references, so a
selected counterexample cannot be dropped to obtain a favorable result. Removed
or altered evidence becomes unavailable instead of disappearing from the set.

A complete, comparable failure of a test profile associated with the criterion
quarantines the candidate. A failure outside that criterion's test profiles is
incomplete evidence, not an attributed counterexample; it still prevents success.
Infrastructure failures, incomplete measurements, insufficient tests, truncation,
unavailable profiles and mismatched execution conditions require revalidation.
A synthetic-only profile cannot establish matching qualified conditions.

Reads recheck current project/source/criterion/profile state within one SQLite
snapshot. A later read observes subsequent deletion or revocation; no guarantee
is made that the database remains unchanged after the response is returned.
Optional notes resolve the original and canonical English memory text live in
the same project. They remain untrusted, and are not copied into the lesson
registry or audit. Withdrawal removes the note from the returned view.

Every lesson retains `origin=worker_reported_measurement`,
`producer_assurance=authenticated_lease_only`, `grants_authority=false` and
`promotion=none`. `reported_conditions_match` means the selected reports match
the declared conditions. It is not a claim of trusted knowledge or current
verified execution.

## Storage and API

Migration 33 → 34 adds four empty tables, with no historical backfill or automatic
promotion. Exact schema validation precedes migration/startup; historical schema
tests construct their actual old preimages. Existing schema-33 execution receipts
remain intact. The separately qualified recovery33 binary does not support this
new schema; recovery34 must be qualified before a future schema-34 deployment.

Eight operations are published in OpenAPI: profile approval/read/withdrawal and
lesson proposal/list/read/assessment/withdrawal. Operator authentication is
separate from paired-device and worker credentials. Requests, pagination and
evidence sets are bounded; these responses are not cacheable.

## Local verification

- 199 integration cases passed in 92.38 seconds on the first final freeze,
  including business rules, HTTP boundaries and historical migrations.
- Review reproduced two valid cross-runtime reports causing a `KeyError` or
  strict-zip `ValueError`. Both now return unknown evidence with a profile
  mismatch, rather than failing the request.
- A subsequent regression reproduced an unrelated Node test failure incorrectly
  quarantining a Python criterion. The focused correction passed eight cases in
  5.21 seconds and returns `incomplete/non_criterion_failure`.
- Network connection and DNS attempts were prohibited in these test runs.
- Ruff and targeted typing passed on the initial freeze. Independent checks cover
  concurrent deletion, current-source changes, evidence-set tampering, competing
  writers, replay after withdrawal and separate operator authentication.

The 199-case run predates the final two-file classification correction; the eight
focused cases validate that delta. These overlapping counts are not added. All
profiles and worker reports in these tests are synthetic: no production profile,
lesson, job, model call or device operation was created by this qualification.

Private evidence under
`Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/m09-receipt-lessons-20261003/`:

| Receipt | SHA256 |
| --- | --- |
| 199-case run, `lessons34-final-20261004/receipt.json` | `916f1ed41a786c0616b9f72e588e346128eb73db2b4fe7cb1b40dd29df0578af` |
| Eight-case oracle supplement | `ca499015f161cebe5e99a8100f90920bddf50358632642cf10022236cdc1b531` |
| Final format and source pins | `59beb4b3412dc8970165c3d0caf6e1c3628a118664333f34e336ba60e889cf45` |
| Independent final code review | `ef67fd98e467392bdf18d560e5c1c8a184e847662108222c7db151c3ba4ddf8a` |

M09 remains partial. Required follow-up includes a qualified real producer,
worker context delivery with revalidation, explicit promotion policy and an
end-to-end demonstration that remembered evidence improves a subsequent task.
