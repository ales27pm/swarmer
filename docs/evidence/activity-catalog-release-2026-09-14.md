# Activity catalogue — 14 September 2026 UTC

The catalogue extends discovery beyond software development to professional and
personal activities. Source commit `d2fd900244a52c88bdfa739000fd418604ab09db` contains
13 domains, 26 role profiles and 79 skills. Nine skills reference real worker
targets and six reference native iPhone capabilities; the remaining 64 have
`execution.kind: planned` and a null target. Catalogue metadata does not register
workers, extend an execution allowlist or launch jobs.

The mobile entry is **Swarm → Explorer le catalogue d’agents**. Search, domain
filters and expandable profiles expose requirements, inputs and outputs. The
authenticated read-only `GET /catalog/activities` endpoint adds current server
availability and sends `Cache-Control: no-store`. The app neutralizes availability
when refresh fails or timestamps are stale, without discarding readable profiles.

The canonical catalogue JSON SHA256 is
`e28a1392cc22f6162e8d5e502288839753fef4db438964d1c201d70b233a9db5`.
The [catalogue reference](../activity-catalog.md) describes execution boundaries
and all availability states.

## Source validation

- Full server suite: **1,200 passed, nine skipped**, 226.05 seconds. Two existing
  dependency deprecation warnings remain. Ruff formatting and lint, strict mypy
  for 62 application sources, Bandit and the OpenAPI contract check pass.
- Full mobile suite: **539 passed in 38 suites**, 38.181 seconds. TypeScript and
  lint pass; Expo Doctor passes all 20 checks.
- Targeted tests cover authentication, read-only state, current policy, stale and
  incompatible workers, native capability boundaries, the 250-agent display bound,
  malformed metadata, searching, filtering and failed/stale refreshes.
- Independent review found three issues that were corrected before the final
  gates: ignored package data, receipt-only freshness and unbounded agent IDs.
  Targeted review confirmed the corrections.

The source was pushed without force to GitHub and Vibecode `main`; independent
`ls-remote` reads returned the same full commit on both. The unrelated untracked
`swarmer-complete-source.txt` was preserved.

Private gate receipts and logs are under
`/private/tmp/swarmer-activity-catalog-*`. The server gate receipt also records
unchanged SHA256 values for 158 source, test and contract files.

[GitHub run 34804118818](https://github.com/ales27pm/swarmer/actions/runs/34804118818)
could not start its required job because the account is locked for a billing
issue. It has no executed steps; the passing local gates are separate evidence.

## Package inclusion correction

The first wheel built from `d2fd900` omitted the JSON despite its presence in Git.
The exact-package guard stopped preparation before any staging or live change.
Commit `fc966a1a5a6172850c33f5bef5929c85c0b7432c` adds only a targeted Hatch
`force-include` mapping in `server/pyproject.toml`. Dependencies, application code
and the mobile tree are unchanged. Both remote `main` references were independently
verified at that commit.

The [packaging commit CI run](https://github.com/ales27pm/swarmer/actions/runs/34804401564)
also stopped before executing any required-job steps, with the same account
billing-lock annotation.

A new wheel was built and installed into a private directory. All 62 Python
sources and the one JSON file matched the checkout byte for byte. Loading the
catalogue from that installed package returned 13 domains, 26 profiles and 79
skills. The wheel SHA256 is
`7f1aab3f827dc68c14e3adf9f15d44623fc4fda518f701565b225c27d13b5f9e`;
the receipt is `/private/tmp/swarmer-activity-catalog-packaging-proof.json`.

The packaging fix used an isolated worktree while the iOS archive continued from
its immutable `d2fd900` snapshot. Both commits have the same mobile tree,
`00c3c0cb51367b96bb3bc85f1e81a67269922039`.

## Release and device boundary

Ubuntu cutover completed at **04:02:43.003434 UTC**, running source `fc966a1`
from `/home/ales27pm/.local/share/swarmer-control-plane/releases/fc966a1a5a6172850c33f5bef5929c85c0b7432c-7f1aab3f827d`.
The deployment manifest SHA256 is
`1a4c7f0df011d625480e31e5019dda900d2df81531b2999eddec76e76bcadcb4`;
the transfer bundle SHA256 is
`9155116e8d39ef6562815113bce0fe88f3a81448f4a6af89b1ff0740a4657650`.
Staging compared Git, wheel and installed application files exactly. Canonical
initialization of a private coherent SQLite copy preserved all 57 tables and
schema 24. The installed catalogue projected the expected counts without changing
that copy. No live migration or database restoration was performed.

After restart, protected-table comparison found no changes. The API and project
worker kept the expected identity, source and model bindings; the legacy worker
stayed disabled. Local and HTTPS health returned 200, authenticated worker GET
returned 200, and unauthenticated catalogue requests returned 401. The OpenAPI
routes match the frozen contract. A successful catalogue HTTP request using a
paired-device credential is not established by the worker-authenticated probe.

The stopped-state backup is
`/home/ales27pm/.local/state/swarmer-control-plane/backups/20260914T040236Z-api-hotfix-fc966a1a`,
SQLite SHA256 `372b946c397aafe82d01c963c1c4507b46a43c486a013dfd0d3fbc18919774f7`.
Private stage and cutover receipts are retained under
`/private/tmp/swarmer-activity-catalog-backend-packaging-20260914/backend-kit/release-fc966a1a5a61/`.

A separate read-only live verification passed at **04:06:08 UTC**. It checked the
process executable, working directory, module origin, installed source hashes,
manifest, wheel, schema, worker freshness, both health endpoints and catalogue
authentication rejection. Calling the installed `ActivityCatalogService` against
the live database with its read-only transaction returned 13/26/79 entries:
`code.build_project` was the sole `goal_ready` skill, eight worker skills were
unavailable, six required native iPhone requests and 64 remained planned. No
initialization, inference or business action was performed. The receipt is
`/private/tmp/swarmer-activity-catalog-live-independent/result.json`.

The iOS build is `20260914035141`, version `0.1.0`, for
`org.27pm.mongars`. This source does not change native modules, plugins or mobile
dependencies relative to the previously qualified distribution build.

The first archive attempt reached native compilation, React Native bundling and
framework embedding, then blocked while signing `React.framework`. The exact
temporary signing keychain reported status flags `2` (locked); `codesign` was
waiting on SecurityServer and a SecurityAgent process was present. The security
daemon had restarted after the initial successful signing probes. No retained
password existed for that temporary keychain.

Only the blocked signing process was terminated. Xcode exited 65 after 2,245.64
seconds; the builder restoration receipt reports success with no errors. The
temporary keychain was removed while preserving the other search-list entries
and login-keychain ACLs. The failed attempt and its immutable source manifest
remain under `/private/tmp/swarmer-activity-catalog-release-20260914/ios-kit/`.
No IPA from this attempt was uploaded.

The retry kit retains the exact source/build and copies all 124 frozen mobile
files only after matching their Git blobs, hashes and modes. `copy2` also preserves
their modification times for the existing compiler cache. Its manifest SHA256 is
`0c92b4f8fad25d5ac9f0f5637fb1d1a9e45102b46d622ed36696bf783c198536`.

A temporary signing keeper holds its random password only in process memory,
checks its own keychain every 15 seconds and supplies unlock input through stdin.
A strong initial probe showed that unlocking does not reliably resume a signature
already waiting on macOS. That failed probe is preserved. The qualified sequence
is therefore **lock → periodic unlock → fresh signature → verification**; it passed
in 15.458 seconds. The handoff explicitly marks in-flight signature resumption as
unverified. Process identity, helper hashes, fresh status and real unlock state
are checked before release phases. Independent review covered these boundaries
and the cleanup path.

A retry preflight also stopped before starting Xcode because less than 2 GiB was
free. Reproducible caches from separate, inactive Core ML compile checks were
removed, preserving their sources/logs and the current release cache. Free space
rose to 2.507 GiB. The exact cleanup list is retained at
`/private/tmp/swarmer-activity-catalog-disk-recovery.json`.

The retry completed an official Xcode Release archive successfully in **1,822.534
seconds**. The archive's source and manifest matched the frozen candidate. Strict
deep signature verification, distribution profile and entitlements, native-runtime
markers and dependency checks passed; `get-task-allow` is false. The JavaScript
bundle SHA256 is `4b9ca2b15fd84dc8db441464cea886afeadf55bb658be4bb0764fd0b480e2d4a`.
The application dSYM matches UUID `CA402010-10E0-3CD3-8DB3-8CF55B977131` and the
llama dSYM matches `C616FD22-F140-33B5-8C3C-95CFA0DA989B`. React,
ReactNativeDependencies and Hermes prebuilts do not supply dSYMs; that limitation
remains explicit in the archive receipt. Builder restoration passed with no
errors. Receipts are under
`/private/tmp/swarmer-activity-catalog-ios-retry-20260914/ios-kit/build-20260914035141/`.

Official `xcodebuild -exportArchive` export also passed. The exported IPA is
19,890,796 bytes, SHA256
`97e5d117751767e40a6e5240f119abba00b96a8f42d022817e1d414a3f4cad8a`.
Export preserved the application UUID and JavaScript bundle hash. Strict deep
signature, distribution entitlements, embedded profile, signing certificate,
native markers and IPA dependency verification all passed. File sharing and
opening Documents in place remain enabled in the exported metadata.

An independent read-only audit compared all 124 frozen source files with Git and
repeated signature, metadata, native-marker, Mach-O dependency, hash and dSYM UUID
checks against the actual archive. Those checks passed. This separate audit did
not cover the concurrently exported IPA or device execution.

The single `altool` upload exited successfully and its log contains
`UPLOAD SUCCEEDED`; the initial ASC lookup had not yet indexed the build. No upload
retry was performed. The signing keeper was then stopped and its temporary
keychain removed. Cleanup verified that other keychain search entries and
login-keychain ACLs were preserved and the temporary P12 was absent.

At **05:27:48 UTC**, App Store Connect returned build
`cdfee42f-d2c1-430d-ba2a-e73009805af6`, version `20260914035141`, as **VALID**,
not expired, with internal state **IN_BETA_TESTING**. A separate group lookup at
**05:27:50 UTC** confirmed this exact build in the internal **27pm** group, which
has access to all builds. The external state is `READY_FOR_BETA_SUBMISSION`;
external beta review and public App Store release are not established. These
responses are retained as `asc-final-status.json` and `asc-final-groups.json` in
the retry build directory.

The physical iPhone identity was read again during release preparation and after
the successful export (`/private/tmp/swarmer-activity-catalog-devices-final.json`).
It remains
paired with Developer Mode enabled, but its development tunnel was disconnected.
Automated component tests do not establish that the new screen works on that
physical device. No catalogue activity or user job was started by these checks.

After the user installed this TestFlight build, the IPv6 development tunnel was
reconnected and catalogue navigation was exercised on the physical iPhone. The
observations and a related agent-status correction are recorded in the
[subsequent device verification](activity-catalog-iphone-2026-09-14.md).
