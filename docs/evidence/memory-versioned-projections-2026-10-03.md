# Versioned memory views and durable indexing — 3 October 2026

Candidate based on `9288fd4`, developed in the isolated
`memory-versioned-projections` worktree. This record concerns local implementation
and disposable databases. It does not record a deployment or a new qualification
project/image in user data.

## Observable contract

The authoritative memory write now commits its textual views and indexing intent
in the same SQLite transaction. A stopped process can leave an indexing backlog
without losing the accepted memory. `memory_text_heads` selects the current
revision, `memory_text_views` retains original/canonical representations, and
`memory_index_outbox` records the projection lifecycle.

- Source text, code identifiers, scope, kind and sensitivity survive verbatim.
  A canonical translation remains a derived view with its source and pipeline
  signature, rather than a second logical memory.
- An update creates another view revision and invalidates old vector work. A
  pin-only update retains the text revision. Deletion removes retained text and
  vectors, leaving a monotonically versioned tombstone.
- A projection checks current source, accepted normalization receipt, revision,
  provider and ownership before dispatch and before publishing its vector. A
  late result cannot restore an edited or deleted memory.
- Batch backfill reserves and rechecks its complete page after admission, then
  makes one embedding call for at most 100 items. A changed page sends no stale
  text. The regular consumer admits one item at a time, bounded to 16 per drain.
- Dispatch is durably marked before I/O. Cancellation, process death or an HTTP
  transport interruption leaves an unknown outcome that blocks further embedding
  dispatch in this queue, even after lease expiry or process restart. Only the
  matching owner/generation can record a received outcome, including a late one.
  A received response still needs vector, source and lease validation to publish.
- `/memory/status` reports count-only projection health under the existing
  pairing and `no-store` contract, including missing configuration, provider
  mismatch, invalid vectors and unknown outcomes. It exposes no source text.

The HTTP adapter now distinguishes a received error/invalid response from a
transport failure. This distinction preserves known retryable failures without
pretending a lost response proves remote execution has stopped.

## Reproductions and verification

Before the dispatch marker was connected, cancelling a provider call and advancing
the lease clock allowed generation 2 to claim the same item. The new regression
proves that expiry is insufficient. A second reproduction deleted/edited a source
while backfill waited for admission: the old implementation still sent its old
text. The corrected path refuses the page before I/O. Review also reproduced a
provider replacement during dispatch-marker commit; the final provider check now
prevents calling that obsolete provider.

Fresh integrated check:

```sh
python -m pytest -q -p no:cacheprovider \
  tests/test_memory_projection_worker.py \
  tests/test_embedding_request_outcomes.py \
  tests/test_memory_goal_execution_transport.py \
  tests/test_memory_text_views.py \
  tests/test_memory_projection_integration.py \
  tests/test_memory_projection_status_api.py \
  tests/test_memory_indexing_regressions.py \
  tests/test_memory_model_role_migration.py
```

Result: **143 passed in 15.78 s**. Ruff passes for the changed Python surface.
Strict mypy with a clean cache and silent followed imports passes for the five
changed services. Independent review reran overlapping targeted suites and found
no remaining blocker in this integrated boundary; their counts are not added to
the integrated count.

The complete server suite (`python -m pytest -q -p no:cacheprovider`) then passed:
**3,871 passed, 11 skipped in 800.93 s**. One Starlette TestClient/httpx deprecation
warning remains. This is the server suite, not an iPhone or deployed-provider
qualification. The final source files are the same ones used in the targeted
checks, packaged candidate and final migration receipt.

OpenAPI validation also passes: 83 paths, 91 operations, 622 references and seven
JSON schemas. No OpenAPI shape changed; the existing status object permits the
new count-only field.

Migration proof includes a disposable copy of the schema-28 database produced by
the frozen `9288fd4` package, rather than only a schema-29 fixture with its version
number lowered: prior table rows/rowids and SQL objects were preserved, three
tables were added, a canonical memory acquired two views, integrity and foreign
keys passed, and a second initialization made no changes. The migration performs
no model calls. A separate synthetic 10,001-item seed took 8.752 s on the Mac;
that is not a production latency measurement. The final DDL, including the
request-state columns and unknown-request index, was then verified against that
actual predecessor again: all existing rows/rowids/columns in its 63 tables and
old SQL objects survived. There are now 66 tables, with only the expected new
`sqlite_sequence` entry for the outbox. The original probe's SHA-256 stayed
`64eaa16a2ee3bafadafee5b9acba6d8d7addd3b72d715aa303ed68762f91c1f3`.
The final private receipt is `schema29-final-migration-20261003/receipt.json`
under the memory qualification log root, SHA-256
`2fa14ac24f2d98096af768bef34e4a3f3a390bf1461fb326bc8337ac6160a1d7`.

## Packaged candidate

An isolated PEP 517 build produced
`mongars_control_plane-0.1.0-py3-none-any.whl` (564,847 bytes), SHA-256
`f20acf2b1f8c2d42ed3c743844a066b68f6e563151ea6ed966c0b1a15cee815a`.
Its 125 application files exactly match the source manifest, SHA-256
`fc72abd143f5fbe73bf9ad136d1faa1050e6fc87a7471174f137fccd7d33ed41`.

The private installed-wheel smoke test commits a memory with indexing disabled,
reopens with a deterministic provider, explicitly drains once, confirms a second
drain makes no call, then deletes and drains the tombstone. It verifies schema
29, no retained text/vector, integrity, foreign keys, and exactly one deterministic
embedding call. There are zero startup or external model calls. The package and
receipt are retained in `package-schema29-20261003` under the same private log
root. This proves package consistency and those lifecycle operations, not GPU,
iPhone or production readiness.

## Limits and rollout requirements

This remains one SQLite vector channel. No native/pivot dual-vector search,
concept/claim persistence, RRF, external index, automatic lesson promotion or
translation-quality qualification is claimed. Replaying a durable backlog is
explicit through the consumer/backfill path; startup does not load models or
call an embedding provider.

An unknown outcome deliberately stays unresolved until actual remote completion
is established. Lease expiry is not an operator recovery mechanism. This queue
fence does not, by itself, fence unrelated GPU workloads outside memory indexing.

Schema 29 is additive but older binaries that accept only schema 28 refuse it.
The reviewed schema-27 compatibility release and schema-28 deployment kit must
not be used as rollback/deployment proof for this candidate. Deployment still
requires a reviewed schema-29-compatible rollback path, matching source/package,
admission checks and a verified copy migration. The already prepared iOS client
also needs device verification before enabling the expanded memory-role path.
Production canonical memory remains disabled; local tests do not qualify the
ambiguous English translation observed in the earlier real-provider trial.
