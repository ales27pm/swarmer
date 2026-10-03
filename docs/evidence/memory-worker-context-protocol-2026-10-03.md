# Symbolic context delivery to compatible workers

The shared-memory requirement applies to every worker: required context must not
silently disappear at a version boundary. The local-generation candidate was
prepared on Ubuntu, but its live workers still used older consumers. The common
`mongars-worker-v0.9` label did not prove symbolic-context support.

## Execution contract

The registration metadata now accepts `capacity.symbolic_context_version: 1`.
Registration is an authenticated approval step; installing source or publishing
an example agent card does not update an existing identity. The new API rejects
coercion from booleans, floats or strings for this capability.

Every compatible binary sends `context_protocols: ["symbolic-v1"]` with each
claim. The server requires both approved SQL metadata and this request's
handshake before leasing a job containing a symbolic envelope or binding. An
`omitted_budget` envelope still requires the protocol. Jobs without either field
retain the legacy contract. A skipped incompatible job consumes no lease,
attempt or claim audit, and does not hide later eligible legacy work behind the
bounded candidate window.

For scheduling only, an authenticated claim advertises readiness for at most
30 seconds, bounded by the agent freshness timeout. The readiness set is local
to one scheduler instance. Empty claims remove the announcement; expired or
future-dated entries are removed; restart begins with an empty set. A previously
approved identity returning with an old executable therefore cannot claim a
symbolic job or indefinitely outrank compatible claimants. Unseen peers become
ready on their own next compatible claim. This readiness is not persisted as
proof of a running binary, and cannot replace the current request's handshake.

File, research and code-review provide the three actual HTTP clients. Code,
text, project, personal, SQLite, Swift and media consume the shipped file client
through their real imports. All ten families announce on every claim, and none
retries against an older API by removing the field after HTTP 422.

## Qualification

The public regression reproduced the former API handing an `available` or
`omitted_budget` symbolic job to a legacy worker, with a real lease and one
attempt consumed. Network access is forbidden in those tests.

The integrated server check passes **361 tests** across 16 affected files in
149.88 seconds, with the candidate import origins and source hashes verified.
It includes public registration/claim, memory consumers, Swift transfer,
freshness, queue-window fairness, waiting on a SQLite write lock, independent
API instances and published API contracts. These runs do not replace or imply a
new full-server-suite result. Ruff and OpenAPI validation pass; type checking
retains the two previously recorded `media_contracts`/Redis baseline errors.

The complete worker suite passes **1,777 tests, 5 skipped** in 29.82 seconds with
the candidate server path explicitly selected.
The 28 added tests load each family's real client in a separate interpreter,
inspect serialized authenticated requests across repeated claims, exercise
HTTP 422 without downgrade, and inspect the eight source agent-card examples.
Focused client tests, Ruff and strict typing also pass. These are local transport
tests, not live worker or provider execution evidence.

Eight real registration responses also validate against both public schemas.
That test exposed stale published skill enums and metadata limits; the JSON and
OpenAPI documents now include the existing code/media/specialist skills and
research/project limits. Runtime skill policy and family-specific limits are
unchanged. The standalone schema/route check passes 39 tests; those tests are
also included in the 361-test integration run and are not an additional count.

Private receipts are under
`~/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/transport-capability-20261003/`.

## Immutable package qualification

The new local cohort is built from exact Git commit
`243e913c3126a1cb15d290fbbac36e6f5024b7fe`. Its 139 application files match
the source archive, wheel and offline installation. The installed wheel passes
48 public capability/claim/schema tests; this is package-boundary evidence, not
an additional independent source-suite count.

All ten separately extracted worker bundles pass positive symbolic-context and
invalid-authority checks, repeated claim serialization, and refusal by an old API
without a retry that removes the protocol. Their shared capsules match the API
capsule byte-for-byte. Modified client bytes and a missing capsule are rejected
by the package verifier. The parent independently reran the cohort byte checks
and all ten isolated probes successfully.

API wheel SHA256:
`d79d9b41fb6f085c935ed422a2c6f976514e1ad18c268becacf83d4128235853`.
Cohort archive SHA256:
`677c2efab654345193f54f06e98fd29c4157b3c3272f22159eb5e527a81d92a2`.
Cohort index SHA256:
`cf0cc1850ee18cf9bf1d0387c90d36bd74927f1fe13321919f48dbea5663224a`.

These artifacts are in the private `candidate-243e913` directory. The API and
workers are now staged as described below, but have not been activated. Local
import and wire tests do not qualify active service bindings, model quality, media generation,
Swift compilation, or iPhone execution. The
[real reviewer comparison](memory-reviewer-comparison-2026-10-03.md)
remains a separate failed semantic-quality check; transport compatibility does
not make its incorrect judgments trustworthy.

The iMac Swift bundle was separately staged in its immutable release directory
`~/.local/share/swarmer-swift-worker/releases/243e913c3126a1cb15d290fbbac36e6f5024b7fe`.
All seven files match its manifest. The installed existing Python 3.12.13 ran the
isolated symbolic/wire probe successfully with network, subprocess and database
access forbidden inside the probe. The launchd plist, old entrypoint and credential
bytes stayed unchanged. No service was stopped or restarted and no capacity was
approved; the active worker still uses its previous release. This stage receipt
cannot replace the fresh stopped-service receipt required at coordinated cutover.
Its SHA256 is `40defb376ba6569d891df2a5e6f3a9998af47a321f29144a21e4f6d7c3fefeda`.

## Inactive Ubuntu preparation

Six family bundles are installed under
`~/.local/share/swarmer-workers/releases/memory-243e913/`: file, research,
code-review, text, project and media. Audio and Chroma share the media bundle;
each other family has its own complete manifest. All six isolated probes pass
with the existing Ubuntu Python 3.12 environment. An independent readback
rehashed every installed file and found exact equality with the six manifests.
No service binding, model setting, agent capability or production row was changed.

The first staging command stopped at the project probe because its private
temporary parent directory was missing. Five bundles had already been copied.
A separate continuation verified those exact contents, created the missing
private directory, installed the remaining media bundle and completed all six
probes. The failed attempt remains recorded. Service binding/state equality in
the receipt covers the continuation's before/after observations; it is not a
claim that the first command completed. Worker stage receipt SHA256:
`470245e2640056ddec3984d88ddfdba2d8695207e596e9b5ee039a69816cc62a`.

The API is installed, inactive, at
`local-20261003-memory-transport-243e913c3126`. The metadata-compatible recovery
is installed separately at
`local-20261003-memory-recovery32-capability-56832aa10432`.
The new stage-only kit passed 55 local tests before transfer. Its pinned source,
wheel and installed API contain the same 139 application files. On Ubuntu,
staging migrated a private copy from schema 27 to 32, preserving all 63 existing
tables and rowids. Recovery initialization passed at each prefix 27 through 32.
The live database was opened read-only for the copy and was not migrated.

Independent readback confirmed the installed API hashes, nine active services,
healthy API 0.14.2 and `current` still pointing to
`local-20261003-memory-compat27-397e979987a6`. Staging did not switch or restart
services. The new stage command receipt SHA256 is
`d597aac7059ffb2b05c6ab76ff75699a54c1602dcb28c2ba34dd5ef080c6b720`;
the fallback preparation receipt is
`1270de83bded8df5e58f2c445e99390670312ae411b4bedcfbc37621be6f5139`.
Private copies and the independent readback are in
`transport-capability-20261003/ubuntu-stage-243e913/remote-stage/`.

## Recovery and activation boundary

The previous recovery32 package revalidates persisted agent cards on startup and
would erase skills/capacity when it encountered the new key. A distinct private
recovery package now preserves this approved metadata. Only its `agent_card.py`
differs; the former package and staged release remain unchanged.

The recovery regression has **15 expected failures and 5 passes** against the
unchanged predecessor. The corrected recovery passes **84 tests from source and
84 against its installed wheel**, with two inherited obsolete-version assertions
deselected and newer equivalents included. The checks cover whole-database
preservation across two starts for schemas 29–32, all worker families, unknown
versions and no implicit capability on legacy registrations. Inherited cases
cover erasure, rollback, malformed schemas and actual pending/accepted receipts.
The independent generic installed probe also reopens a private schema32 copy
twice with unchanged rows, rowids, schema and sequences. The package contains 128
application files; archive, wheel and installed bytes match. All 121 loaded
application modules have checked origins and hashes.

Recovery wheel SHA256:
`56832aa10432473109ba43e4c4ceb16b286f8e1cee3b82ec26fbb65d4fe3a977`.
This recovery preserves metadata; it does not implement symbolic execution or
the new claim field. Rollback must hold admission and restore a coherent legacy
API/worker/configuration cohort, with no unresolved external effects. It never
restores an old database or strips context from retained jobs. Schema27/28 still
use the separately qualified compat27 predecessor.

The earlier c56 API and c527 worker archives predate this handshake and cannot be
presented as its deployment. The 243e913 artifacts are now prepared on their
target hosts. Corresponding approved registration metadata and a coordinated
activation including the active iMac Swift worker remain required. No production
capability, service binding, database content, configuration or model was changed
by this preparation.
