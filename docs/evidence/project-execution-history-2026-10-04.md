# Revalidated execution history and worker context

This local M09 slice builds on the [server-bound observations](project-execution-persistence-2026-10-04.md).
It is not deployed and does not complete M09.

## Implemented behavior

Historical project checks are read through their task, node, job, authenticated
lease, acceptance and accepted revision. The stored result, receipt, source and
snapshot digests must still agree. Missing or altered evidence is omitted with
an omission count; it is not silently replaced by a generic successful result.
The reader does not repair, recreate or promote records.

The context keeps its existing wire format: `accepted_result`,
`content_trust="untrusted"` and `applicability="historical"`. A complete
measurement can include failed tests. Summaries preserve that distinction and
the limits of worker-reported measurements. If the whole summary and its
caveats do not fit, the item is omitted rather than truncated. Legacy results
without execution receipts retain their existing historical treatment.

Selected experiences are checked again at queueing, claim and active-result
acceptance. These checks compare the exact included cards against their SQL
project scope; they do not replace them with the latest top-ranked selection.
A newer unrelated result therefore does not invalidate an unchanged selection.
An invalid selection at queueing is rejected. After queueing, an invalid
selection cancels the affected job and task with
`worker_experience_context_changed`; it cannot produce an acceptance or
revision. Cancellation participates in the same transaction as its audit and
outbox records. An identical terminal replay does not consume context again.

These fences cover the existing durable-context carriers: writing, Python
generation, project construction and media jobs. They do not invent support
for workers that have no such carrier. Empty optional selections remain
compatible. This verifies included evidence, not the completeness of a
client's proposed selection; the capsule fingerprint is not authentication.

The existing conversation-revision behavior is preserved: stale queued work
is retired before claim, while an already claimed job may still finish after
a user reply. The new checks do not present that historical result as proof
that it satisfies the changed request.

## Publication window

An independent deterministic test on `1bb9b35` reproduced a valid project job
being claimed between the job commit and publication of its node's
`worker_job_id`. Its measured result was rejected, while the legacy result
without a receipt was accepted. The strict receipt binding was correct to
require a published relationship; allowing the job to start before that
relationship existed was the scheduling defect.

The correction defers claims of goal-backed project-construction
jobs until that exact relationship is published. Waiting consumes neither
an attempt nor a lease, and published jobs must remain eligible even when a
full candidate window contains unpublished work. Normal publication or
reconciliation makes the waiting job eligible. Receipt acceptance and
terminal replay keep their strict binding checks.

## Evidence and remaining boundaries

The coordinator's combined run passed **310 tests in 105.66 s** across 15 test
files, including persistence, schema, leases, capsule handoff and compaction.
Runtime and test hashes were identical before and after the run. The component
checks passed: 47 consumer/context tests and 126 dispatcher, lease, revision
and context tests (overlapping sets, not an additive total). Twelve independent
controls passed on the frozen runtime with nine before/after hashes unchanged.
They cover delayed publication with and without receipts, reconciliation,
eligible candidates behind unpublished work, audit rollback, revoked cards,
strict replay and a snapshot-only receipt preventing a downgrade to legacy
history. The first consumer regressions also failed before implementation for
unchanged generic history and failure to withdraw erased observations.

Ruff checks passed. Targeted mypy on the consumer modules passed; targeted
mypy on the dispatcher and context guard with `--follow-imports=silent` also
passed. A fresh transitive run with `--no-incremental` reports two errors:
the Chroma profile Literal override in `media_contracts.py:57` and missing
`redis.asyncio` implementation/stubs in `message_board.py:545`. An exact
archive of baseline `1bb9b355` reproduced the same two diagnostics using the
same environment and options on its dispatcher. This is not a claim that
unrestricted server type checking passes. The coordinator separately passed
Ruff check/format on seven files and fresh targeted mypy on all four changed
runtime modules, again with `--follow-imports=silent`.

The first coordinator Ruff invocation could not create its shared cache because
of filesystem ownership. Its failure remains in the original receipt. Only
the static checks were rerun with private/no shared caches; the passing tests
were not repeated. The supplemental receipt verifies the same source hashes
as the test run. Pytest also reported that it could not update its cache and
the existing Starlette/httpx deprecation warning; neither failed a test.

The baseline publication-window reproduction is stored privately under
`m09-receipt-lessons-20261003/independent-publication-race/baseline-review.json`,
SHA256 `3bff5cd8a4ac5136b0fac79c5bd45f4ad234470bff7991c8e8a60505c5f1c181`.
Its one failure and one passing control exercise local SQL and dispatcher
behavior. The HTTP conflict and worker lease-loss consequence were traced in
source, not claimed as a device or live-provider test.

Private receipts under `m09-receipt-lessons-20261003/`:

| Receipt | SHA256 |
|---|---|
| `consumer-checks-01/receipt.json` | `72bd78e6a5377ba061e29811b072eb10a60cdc76ec73daaab755693715462fa8` |
| `included-experience-fences-final/receipt.json` | `71545ae2d7e04d4138ac03214ecdd98c62e43378cea38968ff2a69810d1119ad` |
| `independent-consumer-review/receipt.json` | `df4b48304c1c89c07d19f26ba42315056b85332daec9eeef76f2b668644c70d0` |
| `root-history-integration-0egri7m1/receipt.json` | `a9c0cd1b115f85de355b10c9674869ce8734385598e8bebab6e7f1d98d095871` |
| `root-history-integration-0egri7m1/static-supplemental.json` | `44d564f8d80a373de33be7e058870c1b3ebbc7d515b727ea1213388a0b3ed887` |

No schema change is introduced by this slice. It depends on schema 33, which
is itself not deployed. Producer attestation, conditional lesson candidates,
promotion, current applicability and complete project erasure remain separate
work. These changes cannot by themselves prove that an agent learns a correct
general procedure or never repeats an error.
