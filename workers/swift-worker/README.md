# Swift/iMac worker

Real execution skills: `code.swift.build` and `code.swift.test`. This worker uses
Apple's installed `xcrun swift`, `xcodebuild` and `xcresulttool`; it does not accept
shell commands, arbitrary compiler flags, destinations or signing settings from
model output. Run in an operator-approved checkout under a dedicated macOS account.
Swift package manifests, plugins and Xcode build phases execute code. A fixed argv
is **not** an OS sandbox: review the source and isolate the account before use.

Register the skills through the normal worker administration/permission policy.
Launch with `SWARMER_WORKER_TOKEN` in the environment and the sibling file-worker
protocol included in the release:

```sh
python3 workers/swift-worker/swift_worker.py --base-url https://control.example \
  --agent-id APPROVED_AGENT_ID --workspace /srv/approved-checkouts/PROJECT_ID \
  --destinations /srv/worker-config/apple-destinations.json
```

The operator-owned destination file maps stable names to allowed destinations,
for example `{"simulator":"platform=iOS Simulator,id=DEVICE-UUID"}` with a real
hexadecimal simulator identifier. `platform=macOS` is also accepted. A physical
`platform=iOS,id=DEVICE-UDID` is supported only when explicitly configured by the
operator; tests may install and launch the test app as part of Xcode's normal
workflow. This module itself has not installed or run anything on a physical device.

SwiftPM payload:

```json
{"kind":"swiftpm","source_sha256":"APPROVED_SOURCE_DIGEST"}
```

Xcode payload:

```json
{"kind":"xcode","source_sha256":"APPROVED_SOURCE_DIGEST","project":"App.xcodeproj","scheme":"App","destination":"simulator"}
```

`source_digest(approved_root)` is the SHA-256 snapshot function used by the worker.
Compute it from the approved source revision before dispatch. It hashes relative
paths, file sizes and file bytes; excludes `.git`, `.build` and
`.swarmer-swift-runs`; rejects source symlinks. The root is operator-owned and must
not be concurrently modified by another worker. A changed source digest after
execution invalidates success. The caller must associate the approval with this
hash, not accept a model's unsupported claim of approval.

SwiftPM uses a fresh scratch directory per run, dependency resolution disabled,
credential helpers disabled and two compiler jobs. Resolve pinned dependencies
in the approved workspace before dispatch. Xcode uses Debug configuration and a
fresh DerivedData/result bundle, disables automatic package resolution, and uses
`CODE_SIGNING_ALLOWED=NO` for iOS Simulator. No automatic provisioning is enabled.

Receipts include source hash, source-unchanged check, command exit status, duration,
artifact directory, actual tests observed and failure count. Artifacts are in
`.swarmer-swift-runs/<run-id>/`. Logs are limited to 1 MB and commands to 900 seconds
(default), with lease-cancellation checks and process-group termination. Worker
credentials are not passed to build/test subprocesses.

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
server/.venv/bin/pytest --noconftest server/tests/test_swift_worker.py -q
```

The suite includes actual compilation and one XCTest on a temporary package when
Xcode is present. Xcode command construction and result decoding are tested with
fixtures; an actual app build and device/simulator execution remain integration
checks. No production worker registration or deployment is performed here.
