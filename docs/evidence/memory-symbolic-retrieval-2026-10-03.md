# Symbolic retrieval integration — 3 October 2026

This records local implementation and verification of the next candidate after
`801a23f`. The [schema-31 staging candidate](memory-schema31-staging-2026-10-03.md)
remains immutable. This work has not activated a production schema, changed a
deployed memory policy or run a model on the iPhone.

## Retrieval and authority

`POST /memory/search` accepts an optional `symbolic.catalogs` selection containing
distinct namespace/scheme pairs and requires an explicit supported scope. The
default request remains compatible. Exact concept labels and exact symbol/path
identities add source-qualified candidates; they never promote proposals or
merge independent observations.

The SQL reader verifies every bound source, concept and relation in the requested
scope. A relation to an inaccessible proposal is not exposed. Conditions,
polarity, modality, typed quantities and literal case remain part of the full
proposal. Catalog selection is not permission to read another project.

Agent retrieval obtains its catalog selection from trusted server configuration,
with an empty default. It resolves the linked project in SQL. Source content,
model output and goal text cannot select new agent catalogs. The worker envelope
is `symbolic-context-v1`; it contains whole `symbolic-evidence-v1` cards or an
explicit budget omission. It never grants authority.

The implementation carries these cards into goal planner/evaluator context and
receipts. Source, scope and revision checks run again at admission and result
acceptance. Evaluator decisions and worker results are accepted in the same
SQLite write transaction that rechecks their sources; the configured catalogs
are also checked immediately before commit.

Direct chat and task planning now select general-scope evidence from the trusted
catalogs. Those endpoints have no authenticated project binding: a project name
in prose cannot expand their scope. The complete card reaches the final model
transport, and the receipt records proposal IDs and exact read tokens. A changed
source, task, conversation or catalog prevents acceptance. Tool creation retains
the existing argument validation and approval policy. Even a task without a
conversation records its accepted receipt.

Manual worker dispatch selects from the stored task input, with a separate task
binding; native Swift validation binds to its real validation request. Text,
code and project workers receive complete model context. Deterministic tool and
media consumers validate and separate the evidence from execution arguments;
they do not rewrite image prompts or spoken text. This distinction matters:
transporting evidence does not establish that a generative model used it well.

The reader filters candidate identities and labels before full qualification,
without limiting the raw rows and hiding a relevant older claim. A regression
adds 500 irrelevant proposals and requires the same SELECT count for matching
and nonmatching queries. A local 502-proposal measurement reported 1 SELECT for
a nonmatch and 55 for a multi-source public hit; five-read medians were 11.401 ms
and 36.362 ms. This is a local sample, not a production latency guarantee or a
million-record scalability claim.

## Mobile path verified locally

The Memory screen now lets the owner explicitly enable concept search and choose
scope, namespace and scheme. It calls the same application API as the automation
interface. Changing catalogs invalidates an outstanding response; a changed
pairing clears the selection. Legacy search needs no new arguments.

Returned proposals are labeled **unvalidated**. Their full claim, conditions,
source bindings, concepts and relations can be expanded without prose truncation.
Malformed or unsupported evidence is unavailable rather than presented as valid.
The public application API refuses a card if its credential-redaction policy
would remove applicability fields. It does not silently return the altered claim.

Three real public HTTP fixtures exercise the Python-to-TypeScript boundary.
Unicode bounds count code points. Unsafe JavaScript integers are rejected;
typed literal strings preserve large exact quantities. The parser checks shape
and scope, not truth or live SQL state. It does not reconstruct a Python claim
fingerprint from JavaScript floating-point values. See the
[fixture notes](../../mobile/src/testing/symbolic-http-fixtures.md) for remaining
lossless-JSON transport limits.

Verified commands from the managed checkout's `mobile/` directory:

```sh
node node_modules/typescript/bin/tsc --noEmit
node node_modules/jest/bin/jest.js src/screens/memory.test.tsx --runInBand --no-cache
node node_modules/jest/bin/jest.js src/lib/application-api src/lib/api/client.test.ts src/lib/api/memory-symbolic.test.ts src/lib/api/memory-presentation.test.ts src/components/symbolic-memory-evidence.test.tsx --runInBand --no-cache
```

TypeScript passed. The screen suite passed **18 tests**; the application API,
client, presentation and evidence suites passed **242 tests**. Targeted ESLint
also passed. These use isolated local fixtures and mocked HTTP at the device
boundary, not a physical iPhone or a live provider. The public server acceptance
suite passed **20 tests** with real SQLite and no model providers.

The final combined server suite passed **4,353 tests**, with **2 skipped** and
**9 external-integration tests deselected**, in 993.14 seconds. It ran as the
actual workspace owner from `server/`, with bytecode/cache writes disabled:

```sh
python -I -B -c 'import os,sys;sys.path.insert(0,os.getcwd());import pytest;raise SystemExit(pytest.main(["-q","--tb=short","-p","no:cacheprovider","-m","not integration"]))'
```

The accepted XML receipt and a 76-file SHA-256 source manifest are private under
`~/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/symbolic-integration-20261003T200412Z/`.
The manifest matched before and after the server run. Subsequent documentation
updates do not change the tested runtime. The suite emits one installed
Starlette/httpx deprecation warning; it is not an inference or device test.

The private API wheel has SHA-256
`79dc83100874754f6e21ba9f719b3514efc5aac13494895608182e591343bbbd`.
All **136 application files**, including **134 Python files**, match the frozen
source. Two initializations from the extracted wheel create and preserve an
isolated schema-31 database with integrity `ok`, with sockets disabled and zero
provider calls. Worker package qualification is separate from this API wheel.

The final standalone worker suite also passed **1,749 tests**, with **5 skipped**,
in 29.92 seconds. It used the same owner/interpreter and pytest options, with
`../workers` as the test path. Its separate XML receipt is `worker-results.xml`
alongside the server receipt. This covers local worker behavior and transport;
it does not assert that an Ubuntu worker installation or live GPU execution has
already occurred.

The Python type check still reports two pre-existing issues: the Chroma profile
override in `media_contracts.py` and missing `redis.asyncio` types in the local
environment. The new direct-call modules introduce no additional mypy errors.

## Still required

- Finish final worker-package qualification; the combined server suite and
  independent source reviews have passed.
- Qualify a new immutable application/recovery candidate before activation.
- Verify configured model roles and the actual iPhone search/display path.
- Integrate evidence selection and response fencing in the iPhone's initial
  local goal planner and local tool proposal path. The Memory screen's evidence
  display does not establish coverage of those model paths.
- Measure FR/EN retrieval relevance, chronology, scale and negative transfer.
- Implement evidence-gated lesson validation/promotion, applicability and
  dependency freshness. Unvalidated proposals are not trusted learned rules.
- Extend lossless structured transport if arbitrary JSON numbers/reserved keys
  must round-trip between Python and the iPhone.
