# Project guidance and iMac Swift tools — 2026-09-22

## Scope

Root and nested `AGENTS.md` instructions now follow directory scope from the
accepted project revision. Complete applicable instructions are pinned in the
model prompt. Creates, replacements, patches and deletions require matching
worker-owned guidance receipts. New or updated Markdown instructions take effect
on the following iteration. Unrelated source files remain selectively loaded.

`code.swift.build` and `code.swift.test` use the actual iMac compiler through an
authenticated worker. The operator separately pins an approved workspace digest;
a model-provided digest cannot approve a workspace. Receipts bind operation,
source and target arguments, exit status, unchanged source and actual test counts.
Old persisted policy epochs deny the new skills until explicit activation.

This route does not automatically transport or approve generated native project
snapshots. The `code.build_project` native validation guard remains. This work
does not establish an iPhone app build, physical-device execution or TestFlight
submission. Existing user project source was not executed for qualification.

## Qualification

- Main project worker: 356 tests passed, 5 optional tests skipped.
- Isolated deployed worker baseline: 352 passed, 5 optional tests skipped.
- Main guidance/project regression selection: 93 passed.
- Isolated API guidance/project regressions: 65 passed.
- Swift API/worker tests on main: 46 passed, including real Swift compilation
  and authenticated claim/heartbeat/result submission; broader selection: 171 passed.
- Isolated API regression selection: 189 passed, 2 real-compiler cases excluded
  from that repeated run after passing separately; 13 existing dispatch/enrollment
  tests also passed.
- Independent guidance review: 16 worker and 9 server tests passed.
- Private deployment helpers: 32 worker and 65 API fault-injection tests passed.
- The repaired worker verifier then passed 33 helper and 11 recovery-verifier tests.
- Final shared Swift schema, dispatch, planner and restart regression selection: 76 passed.
- Changed Python modules passed Ruff and mypy.

One authenticated local API test compiled with Swift 6.2.4 and ran one XCTest in
20,988 ms: exit 0, no failures, source unchanged. This is a real compiler test
against a temporary fixture, not production dispatch evidence.

## Release artifacts

- Main implementation: `c4dbfa5f80464140d843d85f60aa3c2e0a8ab7ec`.
- Worker backport: `4814394a0badfe64cb1da0c0937b21d3bd290d78`.
- API backport: `12afe43e1b28283adf9f88e1e9ccacd81c696311`.
- API wheel SHA256: `9016049e1c4c1d1a8539bd089878b15a4e296d8368274d414d24ce88e38a5660`.
- API staging: 70 installed application files match the wheel and source archive;
  initialization on a private database copy preserves schema 24 and all 57 tables.
  Runtime configurations and dependency metadata are unchanged.

Production cutover and post-cutover Swift dispatch results are recorded below
only after independent verification.

The first worker deployment command reported a failure after updating the service
binding: the private helper indexed its source inventory with the new two-file
tuple while constructing the verification report. The worker itself was running
with a fresh authenticated heartbeat; the API returned HTTP 200. The original
failed command and `binding-updated` receipt were retained. Follow-up verification
used a separately reviewed read-only checker, without changing the original
baseline or replaying the deployment. At `2026-09-23T02:00:36Z`, it confirmed both
new source hashes, all mounted runtime files, a fresh authenticated heartbeat,
unchanged unrelated processes/configuration, all 35 protected fingerprints and
API health. No database mutation or model call occurred during that verification.


The API cutover independently passed at `2026-09-23T02:02:12Z`, retaining the
35 protected history fingerprints and the five existing worker identities.
Startup added the two missing Swift rules as **denied** (policy epoch 4 to 5).
An attempted separate activation aborted before changing policy when that epoch
differed from its expected value. Configuration inspection showed why a SQLite-only
activation would not persist: startup and maintenance reload the configured YAML.
The durable configuration therefore uses a private external policy and an API
EnvironmentFile override, leaving the immutable release and original environment
file unchanged. Only the two Swift rules change to allow (epoch 5 to 6), without
automatic redistribution. The first private staging attempt stopped because the
API drop-in directory did not exist; no service or live policy had changed. A
new staging directory was used after creating the empty private drop-in directory.
The supervised activation and independent verification passed; a real API lifespan
test separately checks policy activation and restart persistence.

## Live model guidance result

Two benign qualification jobs through the deployed project worker returned valid,
revision-bound receipts for `AGENTS.md` and `src/AGENTS.md`. Their hashes match the
accepted fixture and both complete guides appeared in the model prompt. The
fixture contains a small arithmetic bug and two Python unit tests.

The first model result requested a focused read. The next iteration received the
requested source through the normal continuation contract, but requested another
read. Neither iteration edited files or ran checks. This proves runtime guidance
receipt handling, **not** that this model completed the repair. The repeated-read
behavior remains unresolved; no further automatic retry was launched for this
qualification. No existing user project source was executed or modified.


## Production Swift dispatch

The configuration-only activation completed successfully. Six independent
read-only verifications between `2026-09-23T02:24:19Z` and `02:25:55Z`, spaced
beyond the configured 10-second maintenance interval, retained policy epoch 6,
both allowed Swift skills and all 35 protected history fingerprints. The policy
digest is `sha256:4844c435d6074b5099006a5a78e8c722ee786e5bf930780442841ca8356a0638`.
The private configuration lane passed 33 local tests and Ruff before activation.

The new authenticated `imac-swift-worker` is
`agt_3bc123695bce42248ba882c44e36a6a7`. Its private registration is reused by a macOS
LaunchAgent. A separate reconnecting SSH tunnel carries its loopback API traffic
to Ubuntu; it opens no public iMac listener. The other five workers remain online.

The first benign fixture built successfully but its XCTest compilation failed:
unqualified `add(...)` resolved to an XCTest instance method. The failed job and
receipt were preserved. The corrected fixture uses `Addition.add(...)` and lives
in a separate workspace, with a newly reviewed startup digest. The same enrolled
worker was restarted with that pin; no user project was changed.

Approved fixture source SHA256:
`b31dc774c13da463e44a8b084ab8e51415e05c14d3c43f6035a402b578c8472b`.

| Actual Ubuntu-dispatched operation | Job | Result |
| --- | --- | --- |
| SwiftPM build | `job_40bf4ddf929a44238c5c228567257381` | exit 0, 2,413 ms; build only |
| SwiftPM XCTest | `job_51cee187b1914cd8827fc43f83bc0f7c` | exit 0, 31,142 ms; 2 executed, 0 failures |

Both results passed the server's operation/payload/source-bound receipt validator,
reported unchanged source and came from the registered iMac worker. Test counts
were parsed from the actual xUnit report. This proves the production dispatch,
authentication and real compiler/test return path for the reviewed SwiftPM fixture.
It does not establish a generated-project approval/transfer flow, an Xcode app
build, a physical-device test, or a TestFlight release.

At `2026-09-23T02:29:34Z`, comparison against the coherent pre-cutover database
confirmed that every pre-existing row in the 34 protected history tables remained
unchanged, including all 278 project revisions and 10 coding projects. Only new
qualification task/job/audit/scheduler records and their sequence counters were
added. All six worker heartbeats were fresh, and policy epoch 6 still matched.
