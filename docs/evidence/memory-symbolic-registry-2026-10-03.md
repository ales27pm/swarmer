# Scoped symbolic memory registry — 3 October 2026

Candidate based on `7dcd7f9`, developed in the isolated
`memory-versioned-projections` worktree. The schema-30 increment connects the
existing concept/claim contracts to SQLite and the paired API. All qualification
uses disposable databases; no production goal, task or image is created.

## Behavior

Six new tables store concepts, multilingual labels, proposed claims, original
source bindings, concept links and explicit relations. The migration preserves
the existing textual views and indexing queue; it does not infer historical
claims, translate text or call a model.

- Concept IDs are independent of language labels. Their public creation path
  always produces `proposed` concepts with `grants_authority: false`.
- Each proposal has its own ID, even when another proposal has the same claim
  fingerprint. Original observations and their source bindings survive instead
  of being collapsed by translation equality or vector similarity.
- Bindings identify a memory, original view, revision and field, with separate
  hashes for the exact UTF-8 field bytes and the content/summary document. Both
  are checked against current canonical records inside the write transaction.
- Concepts referenced by symbolic terms must exist in the claim's exact scope,
  namespace and scheme. Code/path identities and typed literal distinctions are
  retained. Public callers cannot supply a trusted origin or validation status.
- Sources are described as `source_document`, not as proof of who asserted a
  fact. Proposals stay `unvalidated` and cannot override policy or permissions.
- Read paths require an explicit scope and recheck source sensitivity, current
  revision and evidence. Changed evidence is reported as unavailable rather than
  presented as a current claim.
- `contradicts`, `related_to` and `supersedes` are explicit links. Supersession is
  proposal lifecycle metadata, not proof that the replacing assertion is true.
- Forgetting a memory deletes all proposals depending on it, including proposals
  with additional sources, and removes their links/relations in the same
  transaction as text-view deletion. Other proposals and shared concept
  definitions remain. An injected failure rolls back the complete operation.

The identity/label separation is informed by the
[W3C SKOS reference](https://www.w3.org/TR/skos-reference/). It does not require
an RDF store and does not establish semantic equivalence of arbitrary phrases.
SQLite foreign-key enforcement is connection-local; deletion explicitly removes
dependants because existing domain connections do not all enable it. See
[SQLite foreign keys](https://www.sqlite.org/foreignkeys.html).

## Public API

All routes require a paired device. Successful results and domain failures carry
`Cache-Control: no-store`; source descriptions expose identifiers and hashes,
without repeating the original text.

| Route | Purpose |
| --- | --- |
| `POST /memory/concepts` | Propose a scoped multilingual concept |
| `GET /memory/concepts/{concept_id}?scope=...` | Read the proposed concept |
| `GET /memory/{memory_id}/symbolic-source?scope=...` | Obtain current original-source bindings |
| `POST /memory/proposals` | Propose a claim bound to exact evidence |
| `GET /memory/{memory_id}/proposals?scope=...&limit=...` | Read bounded, revalidated proposals |
| `POST /memory/proposals/{proposal_id}/relations` | Add an explicit same-scope relation |

Schema errors return 422; unavailable sources return 404 without exposing their
text; stale or inconsistent evidence and conflicting relations return 409.
The scope contract accepts `general` and `project:*`; it does not reinterpret
legacy `global` or arbitrary strings.

## Verification and deployment boundary

The frozen candidate passed **184 targeted tests in 39.82 seconds**, covering the
symbolic API/store, migration/deletion integration, independent review cases,
concept contracts, previous projection integration and the published OpenAPI.
Ruff checks and formatting pass for all ten changed Python files; strict mypy
passes for the four changed application modules. OpenAPI validates 89 paths,
97 operations, 669 references and seven standalone JSON schemas.

Review reproduced and corrected an 8,001-byte claim producing an internal error,
an oversized result page being validated before its byte limit, and a replacement
with stale or inaccessible evidence still marking another proposal superseded.
Regression checks retain both the exact 8,000-byte boundary and UTF-8 accounting.
SQL excludes inaccessible sources before `LIMIT`; the resulting bounded
candidates are revalidated again inside the same read snapshot.

The private migration receipt `schema30-final-migration-20261003/receipt.json`
has SHA-256 `32640cb8118a6349a826a4c07c581bba17087a3402a5396ecb8a7cc0c3c46ca5`.
It uses the real previously prepared schema-29 file via SQLite backup and proves
66 old tables' contents, columns, rowids, SQL objects and counters unchanged;
six empty tables are added. Initialization is idempotent; foreign keys and
integrity pass. The source file is unchanged and no provider/HTTP call occurs.

The wheel `mongars_control_plane-0.1.0-py3-none-any.whl` has SHA-256
`5f4d87c0d1acdb937768fa1dea6e5161a5950ee2f468b41ab492c0542d04848a`.
All 127 application files match the source and an independent target install.
The installed-package HTTP smoke proves persistence after restart, distinct
French/English observations with equal claim fingerprints, stale evidence
withheld after editing, and transactional removal of dependent proposals after
forgetting. Its SQLite integrity/foreign-key checks pass. The source manifest
has SHA-256 `d4ea1e6429167568806db3543c3706b0b4e5e698e84c6d699949cc99b9979a12`.
Private files are under the existing `goal-model-admission-20261003` evidence
directory, in `schema30-final-migration-20261003` and `package-schema30-20261003`.

These tests exercise real SQLite transactions and public HTTP routes with
deterministic fixtures. They do not establish translation, extraction or retrieval
quality. The complete server suite (`python -m pytest -q -p no:cacheprovider`, run
from the candidate's `server` directory) passed **3,939 tests, with 11 skipped,
in 830.88 seconds**. The existing Starlette TestClient/httpx deprecation warning
remains. The frozen application files still match the wheel manifest.

Production activation is not part of this receipt. The existing rollback kit for
schemas 27/28 rejects 29 and 30; raising its version ceiling alone would leave
old memory writes unable to maintain views, outbox entries and symbolic links.
A compatible predecessor and independently qualified migration/rollback path are
required before activating this candidate. No app installation or ANE result is
claimed here.

The private rollout plan separates the actual previous release from a prepared
fallback. Since migrations 27→28, 28→29 and 29→30 commit separately, recovery must
select the old compatibility release for an observed schema 27/28 and the
qualified fallback for 29/30, without changing the observed database version.
The proposed fallback starts from `7dcd7f9`, retains creation/migration through29,
adds a distinct readable ceiling30 and transactionally purges symbolic dependants
when forgetting memory. That fallback and the extended cutover kit are still to
be implemented and tested; the technical plan is not a deployment receipt.

This registry still needs integration into agent retrieval, measured lexical and
vector fusion, dependency freshness and a trusted lesson-validation process.
The overall multilingual memory objective remains open.
