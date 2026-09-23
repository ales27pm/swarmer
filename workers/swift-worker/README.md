# Swift/iMac worker

Real execution skills: `code.swift.build` and `code.swift.test`. This worker uses
Apple's installed `xcrun swift`, `xcodebuild` and `xcresulttool`; it does not accept
shell commands, arbitrary compiler flags, destinations or signing settings from
model output. Run in an operator-approved checkout under a dedicated macOS account.
Swift package manifests, plugins and Xcode build phases execute code. A fixed argv
is **not** an OS sandbox: review the source and isolate the account before use.

Enroll both skills with the local control-plane administration command after the
operator explicitly enables their persisted permission policy. Adding code or
editing the YAML does not enable a previously absent durable policy rule:

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
Connecting this worker does not make generated project snapshots automatically
approved, staged on the iMac, compiled or validated. The native-project guard must
remain until a separate approved snapshot-to-workspace dispatch and receipt-binding
contract is implemented and tested.

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
