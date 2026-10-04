# Cohort 243e913: activation failure and verified recovery

The candidate cohort was **not successfully activated**. Attempt 04 migrated
the database from schema 27 to 32 and started the candidate services, but the
code-review worker failed because its configured Ruff executable was invisible
inside its sandbox. The API now runs the schema-32 recovery release; previous
Ubuntu workers and the previous Mac Swift configuration were restored. The
database was not restored to an older copy.

## Presence guard and attempt 03

The presence guard now preserves the original prepare baseline and captures a
private device-row preimage in the same transaction. Only changes to
`last_seen_at` and `websocket_connection_id` may be accounted for before the
barrier. Identity, credentials, rowids, schema and other protected changes still
reject activation. The effective baseline is tied to the original baseline,
operation and configuration and is reused by backup, verification and recovery.
The private device preimage is not published.

Local qualification: 42 focused tests and 23 independent checks passed. The
integration run passed 153 tests and failed two generated-template pin checks;
regenerating the template and rerunning those two checks passed without further
runtime changes. This is 155 distinct passing integration cases across runs,
not a single green 155-test run.

Attempt 03 verified 86 transferred files, then stopped during prepare because
Ollama still held GPU memory. No services were stopped and no migration occurred.
The resident model subsequently unloaded; the operator did not unload it or
interrupt user work.

## Attempt 04 and cause

The fresh attempt passed preparation, held the Mac Swift worker, crossed the
barrier, backed up the database and applied the schema-32 candidate. Startup
verification failed with `group_not_active`.

The code-review worker journal recorded `ValueError: ruff executable is
unavailable`. The configured Ruff binary existed and was executable on the
host. The candidate sandbox mounts a temporary filesystem over `/home` and
mounts the worker sources and workspace, but omits that external Ruff path.
Checking host file existence did not exercise sandbox visibility.

The initial automatic recovery was deferred because the rollback guard expected
a Mac-held receipt for the operation suffixed `_rollback`; it received the
apply-operation receipt instead. A new collector receipt for the exact rollback
operation was obtained while the Mac worker remained held. The reviewed recovery
command then finished with exit 0. Neither identity guard was bypassed.

Terminal state: `ubuntu-rolled-back-mac-held`, `database_restored=false`,
`full_cohort_activated=false`. The root operator subsequently restored the
original Swift plist byte-for-byte and bootstrapped it successfully.

## Independent readback

At 04:37:44 UTC on 4 October 2026:

- Nine services were active; the API health endpoint responded as version 0.14.2.
- API host and process-mount inventories matched
  `local-20261003-memory-recovery32-capability-56832aa10432`.
- Seven Ubuntu worker inventories and effective mounts matched the previous
  releases; candidate bindings were absent.
- Eight agents were online, with rollback capability metadata. The Swift
  heartbeat at 04:37:38 UTC followed its restoration.
- All 13 recorded activity counters were zero, and the model-call count was
  unchanged from the backup.
- Protected data and rowids matched after exact normalization of the recorded
  apply/rollback metadata events; 484 prior contexts remained, with none added.
  The terminal journal was unchanged.

This is not a byte-identical database claim: the database retains schema 32 and
reviewed operational-table changes after startup. The independent Mac check
covered its heartbeat and metadata, not another local plist/process inspection.
Studio was checked for active state, working directory, environment and service
signature, without a fresh source inventory. The provider check covered recorded
activity, not a broad GPU/process audit. Two verifier harness mistakes were
retained and corrected before the final readback; neither mutated production.

Private evidence root:
`Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/transport-capability-20261003/`.

| Receipt | SHA256 |
| --- | --- |
| Presencefix transfer manifest | `a8d25e0a1a1e19cf114438a20c71c904e981a5e0efc985ff108e0e1919cd9b5a` |
| Presencefix local qualification | `25ef24476d95de1ec58b07925cb4e29ac7b5293469004d1bb2615fb6caf0912f` |
| Attempt 04 Ruff-path diagnosis | `5b750a77a225081c44885c208ff45ec44c94b373dc61af8a4e9d60655af7950d` |
| Attempt 04 terminal journal | `174f486c1c7409405efc56ea26cd91156c950f33d0e02481fc8fa4e50ff8af3d` |
| Swift configuration restoration | `9d186982c065c8027ca993af08e78e490560619a40d9bf1952bb4069c2e59e2c` |
| Final independent readback | `622411092e0b5afe178440b009f0822886cf88c3e1c1409575d53e32349b09eb` |
| Readback scope interpretation | `f2f294d7a3c23115cf32ba29a0eab899bd64e09c870d92877cafe8731287bae4` |

The next activation needs a fresh immutable kit based on the current recovery32
runtime and schema 32, an explicitly mounted and pinned Ruff tool with a probe
inside the effective sandbox, and correctly scoped apply/rollback Mac receipts.
The sealed attempt and its historical evidence must not be edited or replayed.
The separate local M09 schema-33/34 work is not deployed by this recovery.
