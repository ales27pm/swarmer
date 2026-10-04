# Project execution measurements for evidence-backed lessons

This is the first implementation slice of M09 in the
[57-item product plan](../plans/consolidated-product-plan-2026-09-28.md).
It does not complete lesson promotion, persistence, retrieval, or deployment.
The current memory transport candidate `243e913` does not contain this slice.

## Contract and trust boundary

The isolated project runner can return an optional `execution_receipt` with
the result. The worker attaches it only after running checks. A focus/read-only
step that reuses historical checks cannot manufacture a fresh receipt, and the
model's generation schema does not request this field.

The origin is `worker_reported_measurement`. An observation marked `complete`
means that the measurement fields were obtained; it does not mean that tests
passed, that the worker is trusted, or that a lesson can be promoted. API
validation binds the receipt to the result's source digest, runtime, check
commands, durations, and exit states. A result for another snapshot is refused.

Measurements include the pinned container image, runner and policy digests,
profile identities, test counts, and source/workspace/dependency hashes before
and after each profile. Raw logs and source text are not duplicated in the
receipt. Source changes and failed tests remain observable outcomes rather
than being relabelled as successes.

Directory measurement uses bounded, no-follow file access. Exceeding the file,
byte, or time bound makes the affected measurement unavailable. A dependency
link leaving its root makes the environment unbound. Legacy runtime images
remain usable but report `legacy_harness` and incomplete measurements.
Interrupted profiles have unknown exit/count fields, not invented zero values.

## Integration boundary

The standalone worker validator and server validator use the same contract.
The worker launcher must include the new helper in its source allowlist.
Ordinary results without a receipt retain their previous serialized shape.

Real container measurements require a new immutable runtime image containing
the updated harness. Local execution of the harness in a private copy is a
different proof from Docker isolation and from production execution.

## Local verification

The complete project-worker directory passes **956 tests**, with **5 skipped**.
The affected server integration/contract/OpenAPI checks pass **82 tests**, and
the **14 new server contracts** were checked again against the final source.
Ruff passes for the 12 changed source/test files; scoped mypy passes for three
server files. The standalone worker and server receipt helpers are byte-identical.

The first worker run exposed an existing isolated-import test that the new
direct import broke. Loading the exact sibling helper, as already done for
the capsule contract, fixes that import without weakening the test. A separate
source-only launcher test proves that the new helper is shipped in its allowlist.

Three Node cases execute the actual harness against a private copied source:
a passing test, a failing test, and a test that changes its own source. The
measured counts and source changes distinguish all three. These fixtures use
no network but do not enforce a network sandbox. No Docker, model-provider or
production execution is claimed by this receipt.

Private proof: `m09-receipt-lessons-20261003/foundation-final/receipt.json`, SHA256
`88c29500e4657ec4c9fb00869ae7f9ac8bbce9814899c08962fbb7b2d259ea26`.
It binds the commands, logs and all 12 source/test hashes before and after the
checks. An independent review found no concrete defect and passed seven
targeted checks, including actual Node execution and imports from the launcher's
source-only bundle. Its receipt is `independent-review/review.json`, SHA256
`e1f196c04881e076fba262e42f1ab06ed78ee504e4c3f051005f5a2d6414f89c`.

Root rechecked the current 12 source hashes and reran the 14 server contracts
plus 51 harness/launcher checks. The launcher checks require an unprivileged
account: an initial root invocation correctly hit that guard, and the same
checks passed under the operator account. `git diff --check` also passed.

## Remaining M09 work

Before any reusable lesson is accepted, the server still needs to persist the
receipt with its authenticated producer, job/lease/claim identity, project,
revision and evidence links. Promotion must check actual passing profiles,
unchanged applicable sources/environment, contradictions, scope, and freshness.
Failure experiences must keep their conditions and observed outcomes without
becoming unconditional prohibitions. Revocation and deletion must propagate to
derived lessons and retrieval projections. These requirements remain open.
