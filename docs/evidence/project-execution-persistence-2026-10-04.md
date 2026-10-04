# Server-bound project execution observations

This is the second local M09 slice, following the
[runner measurements](project-execution-measurements-2026-10-04.md).
It does not complete M09 and has not been deployed.

## Accepted behavior

When a completed project result contains an execution receipt, the dispatcher
validates that receipt before committing the terminal job, task transition or
audit event. It binds the canonical JSON actually stored to the authenticated
agent, current lease ID/generation, claim time and authoritative task, node,
goal and project. Producer declarations are frozen at acceptance. They are
not an attestation of the host, runner binary or successful execution.

The acceptance contains digests and bounded metadata, not another copy of
source files, prompts, result text or credentials. A global run ID cannot be
reused by another task or producer. An identical terminal replay checks the
existing binding without inserting another acceptance.

Revision capture rechecks the accepted result and source digest and atomically
links the acceptance to the actual stored snapshot. A failure rolls back the
revision and link together. A fresh validation can produce a new revision
whose files are unchanged; no artificial edit is required. The snapshot hash
is separate from the raw-result hash because the server derives its public
progress message and native-validation state.

Results without a receipt retain their legacy behavior. Migration does not
invent receipts for historical results. An existing historical snapshot
remains readable without a server-bound acceptance; a new capture carrying a
receipt but lacking its acceptance is rejected. Replaying an erased terminal
receipt cannot reconstruct its acceptance or link.

## Storage and compatibility

Schema 33 adds two initially empty tables: `project_execution_acceptances`
and `project_execution_revision_links`. It checks the exact schema-32 prefix,
uses one transaction, and validates a schema-33 database before ordinary
startup recovery can change task or lease state. The legacy migration fixtures
remove only the new empty tables when constructing their historical prefixes.

The internal deletion helper requires a transaction and removes these two
registries even when connection-local foreign keys are disabled. It is not a
complete project-erasure endpoint: existing job and revision JSON still hold
the original receipt and must be covered by an authorized full erasure.

Activation requires a separately qualified schema-33-compatible recovery
binary and runner image. The previously prepared `243e913` cohort is a
different, schema-32 candidate; neither it nor the old schema-27 production
binary can serve as a schema-33 rollback merely by changing a version number.
No production migration, provider call, Docker run or device test was performed
for this slice.

## Local evidence

- The initial two tests failed for the intended gaps: no acceptance table and
  a receipt accepted despite a changed source file. One extended-test fixture
  then failed because its first agent was still online; the fixture was fixed
  to exercise a genuinely different selected producer.
- 129 persistence, migration, lease and capability tests passed in 41.52 s.
  Ruff passed for 12 files and mypy for five runtime modules. The inventory
  was captured after those checks, not claimed to be a before-test capture.
- The coordinator ran 115 additional dispatcher, project, state and local
  context tests in 58.74 s, with identical runtime/test hashes before and after.
- Five independent controls passed in 3.47 s on the final frozen runtime:
  unchanged files across distinct revisions, cross-producer run-ID reuse,
  erasure without replay resurrection, malformed-schema refusal with the
  database unchanged, and frozen producer metadata after registry changes.
- One subsequent regression passed in 0.58 s: two ordinary startups on a
  populated schema-33 database preserved both new tables, their row IDs, the
  original job result and revision snapshot. Integrity and foreign keys passed,
  with zero provider calls. Only this test was added; runtime hashes stayed
  unchanged. Its Ruff check also passed.

Private receipts under `m09-receipt-lessons-20261003/`:

| Receipt | SHA256 |
|---|---|
| `persistence-checks-01/receipt.json` | `2f82a4f55925789a445c1a4c755771a66fe6abe764a3140cf7b113492a6d190f` |
| `root-persistence-integration-c26lryp2/receipt.json` | `95327b8ea15bfd5de11163fd3ee5661b27f084b9dd5b1390fa5a0d3f9da5df2d` |
| `independent-persistence-review/final-review.json` | `5aa3e80a3a1b4b883386749a30b8121e99fc315f77fefb12c56a3b72132e2670` |
| `persistence-checks-supplemental/receipt.json` | `4a38dc84dd9821e2cd88a2ee6c83200e2bc7613cac76e400a9f7bb478e8a93f7` |

These are scoped tests, not a new full-server-suite result. The existing
Starlette/httpx deprecation warning remains. The earlier independent review
is retained separately; its hashes preceded the final canonical-JSON binding
and formatting changes.

## Remaining work

Historical retrieval must revalidate these links and preserve their untrusted
status. Producer qualification, conditional lesson candidates, counterexamples,
explicit promotion, applicability, revocation and complete erasure remain
separate work. A complete measurement or accepted worker job does not by
itself establish correct code or a generally reusable procedure.
