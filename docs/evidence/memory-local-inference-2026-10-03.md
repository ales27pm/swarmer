# Memory evidence in local iPhone generation — 3 October 2026

This is the next candidate after `c527f5d`. It connects server-selected symbolic
evidence to local goal planning and local tool proposals. It is local source and
test evidence, not a deployed backend, installed iPhone build or measured model
quality result.

## Selection and acceptance

`POST /memory/local-context` derives the project and source scope from SQL and
uses the server's configured catalogs. A tool proposal has only general scope;
mentioning a project in its text cannot grant project access. A goal selection
binds its actual goal version, linked project, conversation, base revision and
durable context. Disabled retrieval is an explicit response, not a substitute
for a failed or missing endpoint.

Enabled selection returns complete evidence cards, or an explicit whole-card
budget omission, and a server receipt with a 30-minute lifetime. The semantic
digest excludes the receipt nonce and expiry, so rereading unchanged context
does not make the reviewed generation stale. Schema 32 introduces two empty
tables for receipts and their source dependencies; it does not run inference,
backfill or promote lessons on initialization.

The mobile client captures the paired connection before selecting evidence.
The resulting envelope is copied and frozen. The tool and goal prompts preserve
all mandatory input before considering the optional cards. An explicit omission
stays in the snapshot and receipt but adds no empty wrapper to the model prompt:
even 32,000 bytes of valid mandatory input can still be used. Malformed cards,
partial cards or an oversized available context fail instead of being clipped.

The flow checks the captured pairing before the native inference call, after it
returns, and before submission. It sends the receipt used for generation, not
the new nonce obtained by rereading context during review. The server rechecks
the binding, current source evidence and configured catalogs when accepting the
plan or tool request in its SQL write transaction. A receipt does not attest
that a compromised client ran the claimed prompt, validate a learned claim, or
replace the tool's authorization and approval rules.

## Local verification

The integrated mobile checks passed **363 tests in 16 suites** in 30.736 seconds:

```sh
node node_modules/jest/bin/jest.js src/lib/application-api src/lib/api/client.test.ts src/lib/api/local-memory-context.test.ts src/lib/api/memory-symbolic.test.ts src/lib/local-inference.test.ts src/lib/local-swarm-plan.test.ts src/screens/local-goal-plan.test.tsx src/screens/local-model.test.tsx --runInBand --no-cache
```

The tests inspect the prompt supplied to the mocked native bridge and the
submission body, including source fields, requirements, pairing changes,
expired context, changed semantic digests, new receipt nonces and the
mandatory-input boundary. They do not run Core ML or MLX on a physical device.
TypeScript and targeted ESLint passed for the candidate mobile code.

A subsequent boundary check accepts four complete public HTTP responses from
the real Python route and a disposable SQLite database. Its checked-in fixture
preserves the issued identifiers and digests, with an explicit extraction time
for expiry checks. The parser suite passed **17 tests**, and the final client
plus parser run passed **89 tests**. See
[fixture provenance](../../mobile/src/testing/local-context-http.md).

An isolated iOS export also completed: **1,329 modules**, **25 assets** and a
Hermes bundle were produced from a source snapshot with matching package and
lock files. The release guard first rejected the development `node_modules`
symlink; the successful export used a separate snapshot with actual cloned
dependencies. Metro reported the existing `@noble/hashes/crypto.js` export-map
fallback. This is JavaScript export evidence, not an Xcode archive or an iPhone
installation.

An independent real HTTP reproduction found that an unconsumed receipt for
another goal could reach the legacy advance path on an already-started goal
when the older memory fingerprint was omitted. The corrected path validates a
pending goal binding before any resume side effects, and repeats acceptance in
the plan's write transaction. For tools, first acceptance requires a fresh task
created for that preparation, with its original input, mode, owner, status and
history intact. Legacy calls without a receipt retain their existing behavior
when symbolic catalogs are disabled.

The focused server run passed **156 tests** in 79.56 seconds, including **60 new
public HTTP and migration tests**. Network connections are forbidden in the new
HTTP tests. They cover forged/stale receipts, same-target replay without a new
effect, cross-target rejection, rollback after a catalog changes during audit,
source deletion, additive migration, exact reentry, every schema-32 DDL/version
rollback boundary including `BaseException`, and malformed/future schema
rejection. The full run finished with **4,411 passed, 2 failed, 2 skipped and
9 deselected** in 1,085.13 seconds. Both failures were in the schema-28 test
fixture: it initialized the current schema, then relabeled it as schema 28 while
retaining newer local-context objects. The migration correctly rejected that
inconsistent preimage. After reproducing both failures, the fixture now removes
only explicitly empty tables introduced after schema 28 before assigning that
version. All original preservation, rollback and no-model-call assertions remain.
The affected projection, vector, local-context migration and symbolic checks then
passed **70 tests** in 14.30 seconds with sockets forbidden. The original full run
is preserved and is not relabeled as fully passing; no production code changed
in this follow-up.
Ruff passes. OpenAPI validates **91 paths and 99 operations**. Mypy retains the
two previously reported baseline issues in `media_contracts.py` and the missing
`redis.asyncio` stub; no new issue was reported for this slice.

## Commit, package and Ubuntu staging

Commit `c56e7eeacf80ab5f856d541a50b9b787abcf99d5` was pushed to `main`.
All **139 application files** and the package configuration match the immutable
wheel `bead51e3834fa057a68114bd05c29d8abc9de3defc8f86af1fe721e191fd0103`.
An independent run against that installed wheel passed **89 tests** in 48.73
seconds, with all 133 loaded application module origins/hashes checked and no
network connections. The test fixture source tree contained no application code.

The new stage-only kit passed **50 private tests**, including six migration
prefixes, interrupted schema-32 DDL, recovery and installed stage behavior.
It then prepared both the candidate and recovery runtime on Ubuntu. Staging
verified the 139 application files and preservation of all **63 existing tables**
on the private database copy. Its receipts confirm no activation or production
database write and unchanged bindings/configuration for all nine services.

An independent read afterward confirmed that production still points to
`local-20261003-memory-compat27-397e979987a6`, retains schema **27**, and reports
healthy API **0.14.2**. The staged candidate is
`local-20261003-memory-local-c56e7eeacf80`; it is not the active release.

## Remaining release evidence

- Complete coordinated API/worker activation from the staged artifacts, with
  the qualified recovery chain: compat27 for schema 27/28 and recovery32 for
  schema 29–32. The recovery does not expose the new local-context endpoint.
- Deploy compatible backend and mobile versions, then verify the actual local
  provider/device path. The new app requires the local-context endpoint even
  when the server explicitly disables symbolic retrieval.
- Measure language retrieval quality and implement evidence-gated lesson
  validation/promotion. Transporting an unvalidated claim does not establish
  that the model learned a correct, currently applicable procedure.
  The [actual FR/EN provider trial](memory-provider-qualification-2026-10-03.md)
  currently stops on a false rejection by the translation reviewer.
