# Schema-31 candidate and compatible recovery — 3 October 2026

The exact application candidate is `8fefb0a0e3c97b19c2ee39cf8b70143714bdd364`.
It includes current original/pivot vector retrieval and the scoped read-only
coverage endpoint. The recovery package starts from
`7dcd7f945df634a104bc6eb1a9d3ed6397dd0e36` with a separately recorded compatibility
patch. This receipt distinguishes local qualification from Ubuntu installation
and production activation.

## Installed-package proof on the Mac

- All 134 candidate application files agree between the exact Git archive,
  extracted source, wheel and isolated installed package. All 127 recovery
  application files agree between its reconstructed base-plus-patch source,
  archive, wheel and installed package.
- Both use Python 3.12.13 with the previously qualified compatible dependency
  environment. Dependency metadata and `pip check` pass. Import-origin checks
  verify the installed application instead of an editable checkout.
- Recovery creates through schema 29 and reads through 31. It checks the exact
  memory schema before initialization, preserves native vectors and indexing
  ownership, and transactionally removes dependent view vectors when forgetting
  a memory. It retains the old index-view-only retrieval behavior; compatibility
  does not mean that native vectors become usable evidence in that older reader.
- The 73-test migration suite covers committed prefixes 27–31, malformed schema
  rejection, artifact/import provenance and interrupted migration. The separate
  recovery suites passed 181 source regressions and 46 installed compatibility
  tests.
- Final proofs use the installed candidate and recovery. Both preserve populated
  schema-31 records through two initializations each. Five injected failures
  after table creation, provider-index creation, old-index removal, deduplication
  replacement and version assignment each leave schema 30 intact. Recovery then
  initializes that same database, followed by a successful candidate migration.
  No database backup is restored over these later writes.
- The local prefix proof uses the frozen predecessor source for schemas 27/28,
  and installed recovery for 29–31. The Ubuntu proof below additionally uses
  the actual installed Linux predecessor for schemas 27/28.

The final proof records zero attempted network connections and no configured
providers. Only disposable local databases are modified. No production
qualification project, task, image, memory or model request is created.

## Artifact identities

| Artifact | SHA-256 |
| --- | --- |
| Candidate Git archive | `13957c0b28eca5d1fc5efd6c065426c41aedee34a26fc9f69636d2e734cb3b40` |
| Candidate wheel | `abbc51ce533e7c59b8be67cb6fc4e53739bb81a4ad4184aa01aa6fcfbd731dac` |
| Recovery source archive | `9c06df3bfde0a74ab26dfd6c0e5b47f3f44527595c367db9051fa766ef5a2bb9` |
| Recovery wheel | `df4d20beb286875ccbfad875e9d41fd00a153f8a547712479400aee86bb72a95` |
| Combined recovery compatibility patch | `a8ce6f796454f31e63171a8c6bc9affc506e2792945a88ffcf087db20efbfa74` |
| Final installed-prefix proof | `d5d0c1edec10644365bd379ee319c83b079887ff9216454afb21a17c775ba340` |
| Final installed populated/failure proof | `5f4e243474117d20e7a94cf175ac989e60ade9ca86e2b194a6940f9f729ccc8e` |
| Final local proof receipt | `6fd2db23dc473fa76607a6539939faf1bab1d2aea89818ad0940be8de2abf807` |

Private evidence lives under
`~/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/rollout-schema31-20261003/`:
`candidate/receipt.json`, `recovery/receipt.json`, and
`migration-proof/final-results.json`. Source databases and private configuration
are not committed.

## Activation and product boundary

The independently observed running Ubuntu release is still
`local-20261003-memory-compat27-397e979987a6`, with schema 27 and a healthy API.
The older schema-30 staging kit is immutable and cannot activate schema 31.
The new stage-only kit installed both candidate and recovery in separate Ubuntu
release directories. Neither is active.

## Ubuntu private installation proof

The stage coordinator passed 47 local tests on unchanged helper hashes,
including complete disposable candidate and recovery installations. Its entry
point accepts only `prepare-fallback` and `stage`; neither switches services or
restores a database. Clone checks reject escaping symlinks, uninstall-record
targets, installation paths and shadowed package origins before pip runs.

All 19 transferred files match the sealed manifest. Both Ubuntu commands
returned exit status zero. The candidate has 134 application files; recovery
has 127. Installation uses the running predecessor's dependency environment
with offline, dependency-free wheel installation into distinct directories.

The candidate migrated a fresh read-only backup of the running schema-27
database through all five committed prefixes, 27–31. All 63 pre-existing tables
and their row identities are preserved. At each prefix, the appropriate installed
reader initialized the same private database twice: the running predecessor at
27/28 and the compatible recovery at 29–31. No backup restoration is used for
these recovery checks.

Protected memory tables already present before migration are unchanged. The ten
new tables are expected migration additions; the sole new sequence entry is
`memory_index_outbox=1`, corresponding to the existing memory's indexing intent.
Every pre-existing sequence row is preserved. Vector and symbolic stores start
empty. Providers are unconfigured and the initialization harness denies network
connections, so migration does not generate embeddings or qualification tasks.

The command receipts confirm all nine service bindings and private configuration
hashes are unchanged. A separate readback confirms the active release remains
`local-20261003-memory-compat27-397e979987a6`, the live database remains at schema
27 with 83 goals, and API health is `ok`.

| Private Ubuntu proof | SHA-256 |
| --- | --- |
| Transferred kit archive | `1371b42fff124c443e56873abec2030ed7980333cfda15c3acec6047eda387b2` |
| Transfer manifest | `260a594998b67bd048ef8a45287ae3ee76da2374d12235dae109d4c82b398445` |
| Recovery command receipt | `c3673e795f4ac1f7ee29c4d1153687106020bcbfd507a4b6c767cf2246bc8184` |
| Candidate stage command receipt | `696e518c42fa8a26c95442a7a10bbb34775ef086f45def9a5bb084473be54f95` |

The remote kit is
`~/.local/share/swarmer-control-plane/rollout-kit-memory-schema31-20261003-8fefb0a0/`.
The releases are `local-20261003-memory-schema31-abbc51ce533e` and
`local-20261003-memory-recovery31-df4d20beb286`. Migration copies and receipts
remain private under the candidate's `checks/` directory.

## Remaining activation and product qualification

Production activation still requires compatible mobile activity parsing for all
nine receipt roles and a fresh coordinated cutover/recovery proof. Original
French display needs device qualification. Local migration success does not
establish translation fidelity, multilingual retrieval relevance, Neural Engine
execution or production latency.

The broader memory work also retains explicit remaining requirements:
source-qualified concept-assisted retrieval, a trusted lesson-validation path,
case-sensitive symbol/path search, dependency applicability, phase-aware
retrieval and chronological quality evaluation. Proposal records remain
unvalidated data; retrieval cannot promote them into policy.
