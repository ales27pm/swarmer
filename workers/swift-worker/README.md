# Swift/iMac worker

Real execution skills: `code.swift.build` and `code.swift.test`. This worker uses
Apple's installed `xcrun swift`, `xcodebuild` and `xcresulttool`; it does not accept
shell commands, arbitrary compiler flags, destinations or signing settings from
model output. Run in an operator-approved checkout under a dedicated macOS account.
Swift package manifests, plugins and Xcode build phases execute code. A fixed argv
is **not** an OS sandbox: review the source and isolate the account before use.

Enroll both skills with the local control-plane administration command after the
operator explicitly enables them in the configured permission policy. The API
reloads `MONGARS_PERMISSIONS_PATH` at startup and during maintenance. Keep the
operator policy outside immutable releases; changing SQLite alone is temporary
and is reversed by the next reload. Deploying code alone does not enable Swift:

```sh
python3 -m app.worker_admin --kind swift --db /PRIVATE/CONTROL/STATE.db \
  --permissions /APPROVED/permissions.yaml --model apple-swift-6.2.4 \
  --credential-file /PRIVATE/REGISTRATION/swift-worker.json
```

The command prints only the agent ID. Transfer the resulting registration file to
the iMac over the operator's authenticated transport; keep it outside the approved
source workspace in an operator-owned `0700` directory, with file mode `0600`.
Do not enroll a duplicate identity on each restart. Include the sibling file-worker
protocol in the immutable release. Launch with Python 3.12 or later:

```sh
python3 workers/swift-worker/swift_worker.py --base-url https://control.example \
  --credential-file /PRIVATE/REGISTRATION/swift-worker.json \
  --workspace /APPROVED/CHECKOUT \
  --approved-source-sha256 OPERATOR_REVIEWED_SOURCE_DIGEST \
  --destinations /srv/worker-config/apple-destinations.json
```

The operator-owned destination file maps stable names to allowed destinations,
for example `{"simulator":"platform=iOS Simulator,id=DEVICE-UUID"}` with a real
hexadecimal simulator identifier. `platform=macOS` is also accepted. The destination parser accepts an operator-configured physical
`platform=iOS,id=DEVICE-UDID`, but this unsigned lane cannot perform physical-device
tests that require signing. No physical-device execution has been qualified.

SwiftPM payload:

```json
{"kind":"swiftpm","source_sha256":"APPROVED_SOURCE_DIGEST"}
```

Xcode payload:

```json
{"kind":"xcode","source_sha256":"APPROVED_SOURCE_DIGEST","project":"App.xcodeproj","scheme":"App","destination":"simulator"}
```

`source_digest(approved_root)` is the SHA-256 snapshot function used by the worker.
After reviewing and approving the workspace, the operator pins that revision at
worker startup independently of every job. A matching job hash identifies source;
it does not approve source. A job cannot update the startup pin or choose a new
workspace. Modified source requires a fresh operator review and worker restart.
The digest hashes relative
paths, file sizes and file bytes; excludes `.git`, `.build` and
`.swarmer-swift-runs`; rejects source symlinks. The root is operator-owned and must
not be concurrently modified by another worker. A changed source digest after
execution invalidates success. The caller must associate the approval with this
hash, not accept a model's unsupported claim of approval.

SwiftPM uses a fresh scratch directory per run, dependency resolution disabled,
credential helpers disabled and two compiler jobs. Resolve pinned dependencies
in the approved workspace before dispatch. Xcode uses Debug configuration and a
fresh DerivedData/result bundle, disables automatic package resolution, and uses
`CODE_SIGNING_ALLOWED=NO` and `CODE_SIGNING_REQUIRED=NO` for every destination.
This lane never enables signing or automatic provisioning. Physical-device test
execution that requires signing cannot pass through this unsigned lane.

Receipts include source hash, canonical request hash (including Xcode target),
source-unchanged check, command exit status, duration,
artifact directory, actual tests observed and failure count. Artifacts are in
`.swarmer-swift-runs/<run-id>/`. Logs are limited to 1 MB; the local absolute command
limit is 1800 seconds, and the authenticated remote lane is bounded
to **120 seconds**, matching the registered agent card. Larger app builds require
a separately reviewed policy and runtime budget change. Lease-cancellation checks
stop the process group. Worker
credentials are not passed to build/test subprocesses.

The worker polls the configured HTTPS origin; it does not open an inbound port on
the iMac. HTTP is allowed only on loopback, for an operator-established secure
tunnel. Redirects are rejected. An unavailable or lost lease aborts work and never
submits stale success. A service restart can reuse the same private registration.

These two explicitly dispatched tools remain separate from `code.build_project`.
Connecting a legacy workspace worker does not approve or validate generated project
snapshots. The additional consent-bound snapshot mode below requires its matching
server dispatch and receipt contract; it does not remove native project validation
guards or treat ordinary generated checks as compiler evidence.

SwiftPM test counts come from xUnit testcase entries (not summary claims or console
messages); skipped tests do not count as execution. The Swift 6.2.4 runner requires
parallel mode for xUnit output, so this worker uses `--parallel --num-workers 1`.
These XML results describe XCTest cases; a Swift Testing-only package cannot yet
produce a verified success through this SwiftPM route. Xcode test counts come from
`xcresulttool get test-results summary` including failed/skipped/expected failures.
Zero executed tests, missing/invalid receipts, nonzero command exit, or changed
source never passes. Build-only success is explicitly distinct from tests passing.

Validation:

```sh
cd server
.venv/bin/pytest tests/test_swift_worker.py tests/test_swift_worker_connection.py tests/test_swift_dispatch.py -q
```

The suite includes actual compilation and one XCTest on a temporary package when
Xcode is present. The connection test uses real authenticated ASGI API endpoints
and temporary databases, without a production registration. Xcode command construction and result decoding are tested with
fixtures; an actual app build and device/simulator execution remain integration
checks. No production worker registration or deployment is performed here.

## Approved generated project snapshots

A separate, explicit launch mode accepts source only from the authenticated
`POST /agents/{agent_id}/jobs/{job_id}/project-source` endpoint. Create an
operator-owned `0700` staging directory outside the credential directory, then
replace `--workspace` and `--approved-source-sha256` with:

```sh
--project-staging-root /PRIVATE/SWIFT-PROJECT-STAGING
```

The two modes are mutually exclusive. Snapshot mode does not accept a startup
source pin; the endpoint's current user consent and live job lease authorize one
exact persisted project revision and compiler target. A job hash alone does not
provide that authority. The server must support and validate this contract before
snapshot mode can execute anything; deploying the worker alone does not enable it.

The existing Swift payload gains `project_revision`, with exactly
`validation_id`, `project_id`, `revision_id` and `sha256`. The final hash identifies
the canonical project JSON; the existing `source_sha256` identifies the filesystem
snapshot. The worker checks both, all target fields and the full response shape.
Only the source endpoint allows an 8 MB JSON response (escaping allowance); its
network timeout is five seconds. Standard claim, heartbeat and result limits do
not change. Files remain capped at 80 entries, 64,000 UTF-8 bytes each and
1,000,000 total bytes. Canonical relative paths, duplicate/case/path collisions,
reserved metadata/build/artifact paths, symlinks and nonregular source are checked
before compilation. Every operation gets a fresh private directory; existing
snapshots are never reused or overwritten.

The worker rechecks authenticated source authorization immediately before each
compiler command, approximately every five seconds while it runs, and before
returning evidence. Revocation, stale leases, changed revision/target/digests or an
unavailable validation endpoint stop work without submitting stale success.
Receipts include `project_revision`; `request_sha256` covers the full claimed
payload. Compiler environment filtering and unsigned fixed commands remain the
same. Swift manifests/plugins/build phases are still executable code, so the
staging account must remain isolated: this opt-in is not an OS sandbox.

Additional worker coverage:

```sh
cd server
.venv/bin/pytest tests/test_swift_project_worker.py -q
```
