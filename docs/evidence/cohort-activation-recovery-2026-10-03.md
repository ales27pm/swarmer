# Memory cohort activation: stopped before migration, predecessor restored

Observed on 3 October 2026 in Montréal (4 October UTC). This is an incident
receipt, not a successful activation or a qualification of the complete memory
feature. It supersedes the inactive-stage-only description for this attempted
activation. The implementation plan's remaining work is unchanged.

## Outcome

The attempted API/worker activation of `243e913c3126a1cb15d290fbbac36e6f5024b7fe`
stopped before candidate bindings, database migration or capability changes.
The production database remains schema 27 and the API runs
`local-20261003-memory-compat27-397e979987a6`. Nine Ubuntu services and the
previous Mac Swift worker were restored. No database backup was restored.

Independent verification checked the previous bindings and running source,
runtime environment, protected data, seven fresh Ubuntu worker heartbeats,
API health, Studio readiness and the two unchanged backup hashes. The Mac's
original plist and entrypoint hashes were checked before bootstrap; launchd
reported running and the server recorded a fresh online presence at
`2026-10-04T00:28:24.051319+00:00`.

The failed coordinator journal remains intact with `recovery-deferred` as its
last phase. A supplemental verification receipt proves the manual completion
of restoration; the journal was not rewritten to manufacture a successful run.

## Two reproduced defects

1. **Owned stop incorrectly rejected.** The existing stop barrier sends SIGKILL
   to processes it has frozen and accepts `failed` or `inactive` with PID zero.
   The new adapter and metadata operator require `inactive`. API and Studio
   therefore stopped as intended but failed the subsequent guard. Both had
   `Result=signal`, `ExecMainCode=2`, `ExecMainStatus=9`, and `ExecMainPID` equal
   to the process recorded in the stop journal. The other seven units were
   inactive. No candidate binding/migration/metadata intent existed.
2. **Website reservation blocked API startup.** During recovery, the SQLite
   website writer reservation was retained while starting the API and waiting
   for health. The API's own `website_workflow.initialize()` requires
   `BEGIN IMMEDIATE` on that database. Two startup attempts reported
   `sqlite3.OperationalError: database is locked`; the health wait expired.
   When recovery exited and released its reservation, the configured API
   restart succeeded immediately. The remaining eight services were then
   started with their already-restored previous bindings.

The SQLite mechanism matches its [transaction documentation](https://www.sqlite.org/lang_transaction.html):
only one write transaction can be active, and `BEGIN IMMEDIATE` may fail when
another connection holds one. This explains the observed startup contention;
it does not establish the cause of the separate historical notification-stop
incident recorded as O04 in the product plan.

For restoration, `reset-failed` addressed only the two units whose PID, exit
signal, stopped state and journal evidence matched the owned stop. It did not
reset arbitrary failures. Recovery then passed stopped-data verification and
restored previous bindings before encountering the second defect. After that
defect, the complete verification ran without the website write reservation.

The reviewed local matrix did not exercise real systemd bookkeeping after this
SIGKILL or an API initializer contending for the reserved SQLite database. Its
55 passing tests were insufficient evidence for these two runtime boundaries.

## Evidence and next activation requirements

Private evidence is under `transport-capability-20261003/` in the operator's
Swarmer qualification directory:

- Immutable attempted coordinator manifest SHA256:
  `c87f43363bc506a943f5e52c478b9895ee538fe1132d776a16422c708d8e9d8d`.
- Pre-migration independent readback, including owned-stop and unchanged
  binding evidence: `coordinator-independent-review-20261003/pre-migration-recovery-readback.json`,
  SHA256 `fa5f0cafadb5e1d72d279190d7cad91d11f7121478c9a008a8d2aa19473cdcec`.
- Post-restoration complete verification:
  `coordinator-independent-review-20261003/runtime-restore-verification.json`,
  SHA256 `01a03e083a3dde1454da8418c38bda3f5446a95ac571eccf62e94d467bf4d04d`.
- Original Mac plist restoration and bootstrap receipt:
  `root-cohort-transfer-243e913-02/mac-previous-restored.json`.

Before another activation, the replacement coordinator must exercise a real
disposable systemd unit, reject unrelated failed units, and prove that actual
SQLite startup writes succeed after the reservation is released. Data must
remain checked across that handoff. Deploy and recovery both need this behavior.
The previous immutable kit and failed journal must remain unchanged. A new
attempt requires a new pinned kit, current idle baseline and fresh stopped-Mac
receipt. Passing these checks still does not qualify semantic translation or
the complete iPhone workflow.

## Replacement coordinator qualification

A separate `coordinated-activation-243e913-stopfix` kit corrects both boundaries;
the failed kit is unchanged. The only existing runtime modules changed are
`cohort_cutover.py` and `linux_cohort.py`. It records an owned signal intent with
process identity, service invocation and binding before SIGKILL. Normalization
requires the matching stopped invocation and exit signal; all nine units must
still be inactive before the unchanged metadata operator proceeds.

Both deployment and recovery release the website reservation before startup,
retain the GPU reservation, compare the complete database/website snapshot
across the release boundary, and recheck the held-Mac receipt. Changes at that
boundary prevent startup rather than being ignored as expected runtime churn.

The integrated suite passes **77 tests**. It includes the actual installed
`WebsiteWorkflow` initializer in a separate process against private SQLite
data, as well as interrupted handoff and recovery cases. **Four independent
tests** additionally verify snapshot divergence, expired Mac evidence and
exclusive operator locking. These are local checks, not a production cutover.

A real Ubuntu systemd probe then exercised the same owned-stop adapter. Its
random transient unit ignored SIGTERM, received the owned SIGKILL, entered
`failed/PID0`, and was reset to `inactive/PID0`. Every mutation command was
restricted to that exact disposable unit. Production service PIDs, invocation
IDs and active states were identical before and after the probe.

The first probe stopped in its fixture because a transient unit without an
environment-file property could not be read by the existing binding helper.
The second adds `/dev/null` as an empty environment file and a private working
directory; runtime code is unchanged. The fixture bounds its own stop timeout
to three seconds. Both attempts remain recorded; the failed attempt is not
counted as runtime success.

| Evidence | SHA256 |
| --- | --- |
| Integrated 77-test receipt | `1d46994e044da1489eed880f38b74bda7f1ddd0f5e0f205615af9b0e48ebd96d` |
| Successful Ubuntu probe command receipt | `357222e3707a3b6637b62c5b43700713dfff8cdc8dbaab2b104c4640c5ddc14e` |
| Independent final review receipt | `119c0312c043789af844164ff82117c969c0cd05325bdac68bbdb93d6987d168` |
| Corrected state machine | `30f57a4bfde65bfdaf30b52bfe2fcb4c33fa98daa33b3f41ad602bb2a0897331` |
| Corrected Linux adapter | `faa1d1d22ce897f6921310bd04e1dd120df12919c9aa81a3a375d818317938ea` |

The full memory cohort has not been activated by these checks. A new supervised
attempt and independent live verification remain required.
