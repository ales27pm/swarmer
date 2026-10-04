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

## Second attempt: effective binding rejected, automatic recovery verified

The new `cohort_stopfix_e462968c1a` operation ran on 4 October UTC. Its fresh
read-only preparation succeeded in 2.2 seconds, with baseline SHA256
`3300c7fb0c2a56a55a6bb93d75024ce0e282fdee7131c3abc3706ca249f45ca0`.
The held Mac receipt was bound to this new operation. The nine services stopped,
and the new owned-stop normalization passed for API and Studio. Candidate
binding files were written, but `verify_bindings` rejected their effective
systemd configuration with `effective_cohort_binding_differs`.

This failure occurred **before offline migration and capability metadata
commit**. The journal contains neither intent. Automatic recovery restored the
previous bindings, released the website write reservation before startup and
completed `ubuntu-rolled-back-mac-held`. The deployment command returned exit 1
after 29.9 seconds; this is a failed activation with successful recovery, not a
successful release. Its normal `ExecStopPost` recovery recognized the completed
rollback and did not repeat it.

A separate supervised `verify` command passed in 5.8 seconds. The production
database remains schema 27, foreign-key verification reports no violations and
API health returns HTTP 200. No backup was restored. Root restored the original
Mac plist and bootstrapped the old Swift worker; launchd reported PID 6386, and
the server recorded it online at `2026-10-04T01:00:40.569781+00:00`.

The immutable journal SHA256 is
`a8fba804ea91af92b45518aa1a30b3a97168b09a67fc9c8488179529098b68bb`.
The command results, Mac restoration receipt and copied journal are retained
under `root-cohort-stopfix-transfer-01/`; the copied readback SHA256 is
`5c118c697ece81f68d364ed5c4112c4cf4733b0749bd3f6d808f231cf5b3a567`.
Diagnosis and correction of the rejected effective binding must precede a new
activation attempt; the failed operation and its pins remain unchanged.

The manager journal identifies the binding defect: all eight new drop-ins used
`WorkingDirectory="/home/..."`. The installed systemd parser rejected those
assignments as `WorkingDirectory= path is not absolute`, leaving the previous
working directories effective. The expected paths and template paths agree
after removing those literal quotes; the problem is the unit directive's
serialization, not a decision to change the research worker's directory.
`ExecStart` quoting is a separate grammar and must remain intact.

The diagnostic was read with the host's local time window, 3 October
20:57–20:59, and retained as
`coordinated-activation-243e913-bindingfix/checks/root-cause/systemd-parser-diagnostics.json`,
SHA256 `92fafd161a418e2e157d6d7c923af381ee89cf501942baacdc4522a461052c79`.
The replacement must validate all eight rendered bindings before stopping any
service and exercise the installed systemd parser on disposable unit files.

An independent read-only review additionally verified the nine old runtime
bindings and sources, unchanged protected rows and model-call rows, and zero
capability-audit rows for this failed operation. Its receipt is
`coordinator-independent-review-20261003/stopfix-01/rollback-review-receipt.json`,
SHA256 `1dbbce1ed3f83927ac734a3d047a7689eafa638003573a0aecfad519045bd136`.

## Third attempt: later drop-ins override the candidate

The binding correction passed 98 integrated tests, 15 independent tests and
16 real Ubuntu parser cases. The eight old quoted directives were rejected;
the eight corrected files were accepted without stderr. These checks proved
individual syntax and the adapter's expected runtime directories, but did not
exercise every layer of the installed systemd unit configurations.

The fresh `cohort_bindingfix_0a3619b77e` operation used manifest
`7c268dda55f28910280177704b264930019466610a3541bb1803f3fb3ee5face`,
configuration `2f6da7640332d51437d18bbd7a297db6e5b1af35369b5f71ab4dd5bb1154a83e`
and baseline `ed6bf8ad14450adfaec41895ab643c3f364d3b7320d5a736acff5ad5c47d34b0`.
Its effective-binding check stopped on
`effective_cohort_binding_differs:swarmer-project-worker:argv` before migration
or metadata application. Automatic recovery completed in 29.3 seconds; the
independent supervised verification passed in 5.8 seconds. The original Mac
plist was restored and its worker bootstrapped afterward. No database backup
was restored.

Readback identified a separate ordering defect: the proposed drop-in name
begins with twelve `z` characters, but project and text workers have existing
drop-ins beginning with seventeen. Their later `ExecStart` and
`WorkingDirectory` assignments override the candidate. The last project file
is `zzzzzzzzzzzzzzzzz-2100-completion-focus.conf`; the text worker's last file
is `zzzzzzzzzzzzzzzzz-800-writing-handoff.conf`. The parser fixture lacked these
existing layers and therefore could not detect this defect.

The corrected deployment must prove the effective configuration of all eight
complete unit stacks, including a negative control with the shadowed filename,
and reject a candidate filename that is not ordered after all current drop-ins
before stopping services. Existing drop-ins must remain byte-identical and
must be restored by removing only the deployment's own file.

Evidence is retained under `root-cohort-bindingfix-transfer-01/`. The immutable
rollback journal SHA256 is
`dfd94b04ec938e7518390ff92e60b2e56a9644fcb33e113ed05ec65b6c2ab964`.
Independent readback verified nine old runtimes, API and Studio health,
schema 27, unchanged protected/model-call rows and no metadata application:
`coordinator-independent-review-20261003/bindingfix-01/rollback-and-unit-layers-readback.json`,
SHA256 `0a96ea496bb63a90976af9f9d0b386223a86bf82ecadf25c4e1aa90d65737628`.

## Complete-layer qualification and fourth attempt: GPU admission refused

The separate orderfix kit makes the candidate drop-in sort after every current
layer and checks that order before stopping services. Its Linux adapter SHA256
is `e1ea8a2c35007e5217b2e6af735e8d191bf83edccd791baf3d9714059145d8b6`;
the binding draft is `e5b591760bd1587daa3453d728ee3bae8877565e8460c011cde31bcefd7879d0`.
The eight template contents remain identical to the bindingfix templates.

The actual Ubuntu user manager loaded 24 disposable, never-started unit
definitions containing the complete original fragment and all effective
drop-ins. Eight preimages and eight corrected bindings matched exactly. The
old filename reproduced the project/text overrides in its two negative cases.
Environment and configuration comparisons were kept exact.

The first full-manager probe completed those comparisons but refused its final
production-state comparison. Its UUID files were removed; manager cleanup was
not confirmed. Independent readback then found the nine process identities,
effective bindings and configuration hashes unchanged. The first probe had
not retained the exact before-state difference, so its cause remains unknown.
It is preserved as a failed proof, not relabelled as a success.

A diagnostic copy added field-difference reporting without changing any guard
or manager command. Its complete rerun passed in 1.37 seconds: all 24 cases,
unchanged production state, and removal of both UUID files and definitions.
No unit was started. Proof is under `root-merged-manager-probe-331af154c944/`;
command receipt SHA256 is
`0d94539653ff05275998eb2e6f4b5cb1a59b4697b435f609d7145bd11306b287`.
The kit has 38 current targeted tests and ten independent checks. The earlier
98-test suite is inherited evidence, not a new execution on this kit.

The sealed orderfix manifest is
`0ceced3c817e4fcda5f7bb3a0b12c4bd042b2fd9e32dbfece7a9edf0b630d3f2`.
Its 76 listed files plus manifest were transferred and hash-checked. Independent
input review confirmed that only the kit paths, operation/actor IDs, binding
draft and code pins changed; the runtime settings and seven retained Studio
jobs were preserved. The reviewed host configuration SHA256 is
`8e45a61cea290b09308fb72195bf0f87ee6c443113964857912d50438d56d568`.

Preparation for `cohort_orderfix_4a09f26da4` succeeded in 2.1 seconds, producing
baseline `f2298b11cece0bbd9d67d4b257c4f94a8a43f274814665c9b0ac4f0e5914774c`.
The fresh held-Mac receipt was accepted, but the deployment's fresh OS-idle
check rejected `Ollama_VRAM_occupied_or_unknown` before stopping any Ubuntu
service. The journal contains only `pre-stop-abort-mac-held`, SHA256
`48929f6d05d8ba17f5f2414b103670f256995ec0a6108574bca67dbab18bd301`.
There was no binding, migration or capability change and no backup restoration.

Readback showed `swarmer-project-qwen3-coder-heretic:30b-32k-d2d985e` occupying
6,558,675,107 bytes of VRAM with context length 64,000. A subsequent independent
read-only audit distinguished actual work from cached weights: one agent job
and one plan node were active, and a project-worker child had an established
TCP connection to Ollama. SQL admission also refused
`active_execution_or_maintenance`. The model was not stopped or unloaded.
The audit verified the nine running process identities, old bindings, source
hashes and configuration against the baseline. The API remained healthy on the
compatible schema-27 predecessor. Its receipt is
`coordinator-independent-review-20261003/orderfix-01/prestop-audit-readback-04.json`,
SHA256 `4f52073e2c41fd3811cefd09afd10e2e5ae9344c9e7e37eb7d24b17bbd6c44e0`.
Root restored the original Mac plist; launchd reported the old Swift worker
running as PID 10691. A subsequent independent readback verified its old
entrypoint, plist and credential identity, plus a server heartbeat at
02:09:51 UTC on 4 October, aged 0.93 seconds when observed and later than the
restoration. Receipt: `coordinator-independent-review-20261003/orderfix-01/swift-heartbeat-and-work-followup.json`,
SHA256 `b66533158f0b2592d34ce3bf9c7c6bb728d5aaee52639bfe810012cf4f5737ce`.

Evidence is retained under `root-cohort-orderfix-transfer-01/`. The full memory
cohort is still not activated. Another attempt requires fresh idle evidence,
a new baseline/journal and a new held-Mac receipt, not reuse of this failed run.
