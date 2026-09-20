# iPhone application API client

`iphone-api.py` uses Python 3.9+, OpenSSL and Xcode's `devicectl` on the Mac.
It talks directly to the application's **Debug-only** HTTPS API; a TestFlight
or other Release build does not expose this endpoint. The app must already be
installed, foregrounded on a paired, unlocked physical iPhone with DDI available.
The client does not install apps or repair pairing. Use `launch --console` to
keep the launch's CoreDevice connection open while testing over a device tunnel.

For an installed development build, first prepare the generated iOS project as
shown in [the build instructions](../../docs/25-application-api.md#compilation-dédiée).
Then build with the C/C++ optimized Debug wrapper; it does not install or launch:

```sh
bash mobile/scripts/build-automation-iphone.sh \
  -derivedDataPath /private/tmp/swarmer-automation-build \
  -clonedSourcePackagesDirPath /private/tmp/swarmer-automation-packages \
  -allowProvisioningUpdates
```

The wrapper fixes `Debug`, `GCC_OPTIMIZATION_LEVEL=3`, and
`SWIFT_OPTIMIZATION_LEVEL=-Onone` after forwarded Xcode arguments. This optimizes
the C/C++ CPU inference kernels while retaining the native `DEBUG` API. Swift
stays unoptimized because Swift 6.2.4 crashes in ExpoModulesCore with global `-O`.
Only the `build` action is supported; an explicit `build` is normalized to one
final action. Contradictory configuration/optimization overrides, `-xcconfig`,
`XCODE_XCCONFIG_FILE`, compilation-condition overrides
(`SWIFT_ACTIVE_COMPILATION_CONDITIONS`, `GCC_PREPROCESSOR_DEFINITIONS`) and extra
compiler flags (`OTHER_CFLAGS`, `OTHER_CPLUSPLUSFLAGS`, `OTHER_SWIFT_FLAGS`) are
rejected, including conditional build-setting variants. Cache and signing
options remain forwarded.
Development signing must already cover the selected device. Check the real build
commands for Cmlx `-O3` and retained `DEBUG`; optimization alone is not device
performance evidence. Wrapper tests invoke only a fake Xcode executable:

```sh
node --test mobile/scripts/test-build-automation-iphone.cjs
```

Rediscover the exact UDID/CoreDevice UUID before launch. `launch` requests fresh
device details and checks the identity, paired/connected/booted state and DDI.
Choose **`--device-tunnel`** for a CoreDevice connection: after the held launch,
the client reads details again, verifies the same UUID and UDID, and uses its
fresh tunnel address before HTTPS. If the first read-only health fails, it can
rediscover that same device again within the readiness deadline while the
session is still unbound. It never relaunches or repeats a command to recover
an address. Once bound, that session's endpoint is not silently replaced.

Alternatively, **`--host`** selects an explicit IPv6 or IPv4 address which is
never changed. The two address options are mutually exclusive. No hostname
lookup or DNS override is used; a wrong address fails TLS verification rather
than receiving a token. Do not feed a previously captured tunnel address to
`--host`: CoreDevice can replace it during launch.

```sh
xcrun devicectl device info details --device YOUR_UDID --json-output /tmp/iphone-details.json
python3 mobile/scripts/iphone-api.py launch --console --device YOUR_UDID --device-tunnel --ttl 900
```

`launch` **terminates any running instance of the selected app exactly once**
to inject the temporary credentials, then waits up to roughly 30 seconds for
HTTPS health after the fresh device read. There is no launch or command retry.
Save the returned session path; do not display its contents. An optional
`--session /private/path/session.json` must name a new file. Existing sessions
are never overwritten. The default session directory is private (0700), the
session file is 0600, and the TLS key/P12 directory is 0700 and deleted after
launch. No private key or P12 password is retained in the session file.

While the Debug API is ready in the foreground, the app temporarily disables auto-lock.
It restores the previous idle-timer value on stop, expiry, or backgrounding. This does
not keep the app running after you switch away or lock the phone manually.

On iOS 26, a foreground-started MLX generation may obtain a finite continued
processing task. Devices without background GPU support use local CPU inference;
the GPU capability remains false. Read `models.status.backgroundExecution`:
`executionDevice` identifies CPU/GPU and `supported` describes
OS/hardware support, while only `active` confirms the current task's admission.
CPU execution reuses the default CPU stream, avoiding a new retained MLX worker
per generation. Native progress counts completed preparation/token steps;
admission or an increasing work counter alone does not prove decoded output.
During an admitted calculation, the existing HTTPS listener permits GET requests,
`models.status`, `inference.cancel`, and recovery of existing idempotency receipts.
New unrelated work requires foreground. The listener closes when the calculation
finishes or expires while backgrounded; return to the app to retrieve
the existing job. Do not resend a generation after losing the connection.

With `--console`, leave this first terminal running. A flushed JSON line with
`event: "ready"`, the session path and verified health indicates that a **second
terminal** can issue the commands below. The first terminal supervises its
single child until the console exits or the session expires; a final JSON line
reports closure. The child PID/owner/start time are recorded in the private
session for diagnostics, never used to control a process found by an old PID.
Raw app/devicectl console output is discarded; the supported devicectl JSON
output goes to a separate private temporary directory removed on exit.

Apple's console mode forwards catchable signals to the app. The supervisor
therefore isolates the child's process group and, on TTL expiry, Ctrl-C or
interruption, kills **only its local devicectl child with SIGKILL** and waits
for it. It never issues an iPhone process signal/terminate command or sends
SIGTERM/SIGINT to that child. The phone app's behavior when its console
connection disappears is not yet physically qualified; this is not a promise
that the app survives disconnection. No automatic relaunch follows a failure.
The native API independently expires at its short TTL.

Without `--console`, launch returns after readiness and does not hold a tunnel.
That mode requires a separately stable device connection. No unverified Darwin
notification keeper or persistent background service is started by the client.

```sh
python3 mobile/scripts/iphone-api.py health --session SESSION_PATH
python3 mobile/scripts/iphone-api.py catalog --session SESSION_PATH
python3 mobile/scripts/iphone-api.py call app.status --session SESSION_PATH --wait
python3 mobile/scripts/iphone-api.py call COMMAND --session SESSION_PATH --input-file /private/path/input.json --idempotency-key YOUR_UNIQUE_KEY --wait
python3 mobile/scripts/iphone-api.py job job_INSTANCE_COUNTER --session SESSION_PATH
```

Choose actual command names/input schemas from `catalog`. Use `--input-file -`
to read a JSON object from stdin. Requests and credentials are not printed;
response JSON is printed and can contain application data, so protect captured
output. The client performs only the selected command; `--wait` sends subsequent
GETs for its job. No screenshot, tap, fallback service or automatic command retry
is involved.

Each launch generates a temporary CA and TLS identity with SAN
`mongars-automation.local`, a random bearer, and expiry of at most 3600 seconds.
The client verifies the CA, SAN and exact certificate DER SHA256 **before**
sending the bearer. Credentials travel to `devicectl` only through its child
environment; the P12 export password travels to OpenSSL over stdin, never argv.

The first successful health binds `instanceId` in the session file. Every POST
includes that instance; a JavaScript reload fences old commands and job IDs.
The client never silently adopts another instance. Job receipts/idempotency are
held only in that JavaScript session, not durably on the device. A backgrounded
or expired server may be unavailable; opening a fresh session does **not** make
it safe to repeat an earlier action whose outcome was unknown.

After an uncertain POST response, inspect the application state first. The
diagnostic includes the original idempotency key; only an **explicit** same-session
call with that key and identical input may recover its retained receipt.
Never automatically repeat it after a relaunch/reload. `uncertain` is a terminal
non-success result; `succeeded` with `resultOmitted` means execution finished but
the response was too large to retain—do not run it again to retrieve the result.
Exit codes: 0 successful response (or accepted job without `--wait`), 1 failure
or uncertain outcome, 2 wait deadline reached with the last running receipt.
Delete the session file after use; it contains the bearer until its short expiry.

```sh
python3 -m unittest discover -s mobile/scripts -p test_iphone_api.py
```

Tests use mocked device/network operations, local OpenSSL certificate validation
and a synthetic HTTPS endpoint bound only to Mac loopback. They do not launch an
app, contact an iPhone, or run a business action.
