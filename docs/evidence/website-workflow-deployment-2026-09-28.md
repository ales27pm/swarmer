# Website workflow and requirement evidence deployment

## Scope

The rollout follows the requested pull of PR #3 (`9d5e4f1`). The server candidate
`c48f40352cb03eb363f5636cd1ef44f1d9f97197` includes that main revision, preservation
of the live `/memory/status` `Cache-Control: no-store` behavior, and compatibility
with existing sparse permission policies without enabling additional capabilities. The iOS release
is prepared separately because the website screen requires a new binary.

The server is qualified as a complete main release, including previously
undeployed context/specialist modules. Existing model selections, worker
executables, permissions, credentials and feature configuration are preserved.
Optional context/compaction, extended calendar, browser, provider and publishing
features are not inferred active merely because their code is packaged.

## Artifacts and migration

- Server source archive: `aa736ff5c759074632814afa3ac02cdd124510e065fa6b1f96f9d056f923d128`.
- Server wheel: `0a2a2a5597f7a9029617323bd0c6a282b25a2930b61d3449313ab939a4fab52a`.
- All 98 application files match the frozen source byte for byte.
- Dependency manifest: `f074aca2dd7a41d076aa7065f250c11cf55931234921e9d7f8bc12a0e62dc96d`.
  Added packages are defusedxml, Pillow, Playwright, pyee and greenlet;
  existing distribution versions remain pinned during staging.
- Compatibility rollback source: `51691df7f2358af4d6d7f750456938cb39fdb224`, which
  changes only the previous release's supported schema version from 26 to 27.
  It retains the new additive tables without interpreting or deleting them.
  Rollback must not restore an older database over later accepted writes.

Central state migrates from schema 26 to 27 with additive project context,
compaction and requirement-evidence storage. The website workflow additionally
owns a private sibling SQLite database and artifact directory. Its active jobs
must participate in deployment admission and its writer must be fenced alongside
the central database. Saved website artifacts remain private; this deployment
does not publish a generated website.

## Qualification

- 277 affected server tests passed with real existing Chromium fixture execution;
  no skips. Three memory-status/API parity tests also passed.
- The migration fixture preserved all 57 existing table fingerprints, passed
  SQLite integrity and foreign-key checks, and was idempotent.
- A further 330 tests across 21 suites passed with no skips, covering the full
  release's specialist dispatch, context/compaction, Swift, agenda and catalog
  changes. The schema-compatible rollback reopened populated evidence, context
  and completed-compaction records twice while preserving all 60 table
  fingerprints, immutable evidence triggers and SQLite integrity.
- Staging against a private copy of the real schema-26 database caught a legacy
  permission policy compatibility failure before activation. The scoped fix
  passed 14 new regressions and four existing policy-fencing tests. The exact
  live YAML and its 12-rule durable snapshot retained identical bytes, digest
  and epoch 6 across repeated initialization/reload; missing new capabilities
  remain unavailable. No live permission configuration was changed.
- Mobile validation passed 63 suites / 1,091 tests, TypeScript and ESLint.
  Release qualification caught the new website route missing from the bundle
  contract. Commit `36537f2721a28ff2c43e50e6624c413fd6b48925` corrects the contract
  and archive verifier regression fixture. The new archive is pinned to that
  revision; the subsequent server-only permission compatibility fix does not
  change the mobile source or API contract.
- Ubuntu's non-root user/network namespace probe failed. Browser rendering must
  remain disabled until isolation can be qualified; no non-isolated fallback.
- Infographic Artist endpoint and public hosting destination are unconfigured.
  Their capabilities must remain explicitly unavailable.

- All 191 deployment-helper tests pass. The staged candidate preserves 58 real
  pre-existing SQLite table contents and adds only the three expected tables;
  its compatible rollback also initializes successfully against the migrated copy.
- The installed wheel passed an isolated API smoke: authentication required,
  no-store responses, owner isolation, and website metadata preserved across
  restart. This test performed no model calls, external content fetches or
  production project mutations. Its first attempt failed before any request
  because the fixture had not configured the module-level app before import;
  the fixture was corrected and the isolated run passed.

Private artifacts and receipts are under
`~/Library/Logs/SwarmerDeploy/website-workflow-20260928/`.
## Production activation

- Guarded cutover completed successfully on Ubuntu at approximately 03:32 UTC
  on 2026-09-28. Active API release is `c48f40352cb03eb363f5636cd1ef44f1d9f97197`,
  service health reports `0.14.2`, and central state is schema 27.
- Reviewed baseline SHA: `61dd73545f997b58491a2d05df880ae6fa493d79448df681ad265189587d702e`.
- The independent verification at 03:33:23 UTC confirms all 44 protected
  fingerprints unchanged, six agents online with fresh heartbeats, and eight
  work-admission categories idle. Worker sources, bindings, credentials, model
  selections, environment and permission policy are unchanged.
- No database restoration, agent re-enrollment or user project retry occurred.
- Independent receipt SHA: `06cd7dc47025ccacbb3fbfa4969b64a9d38c49346ca90f21aef444145236840e`.
- A separate read-only production check confirms both website API routes in
  OpenAPI and HTTP 401 without authentication. Authenticated persistence and
  owner isolation were exercised in the installed private fixture, not a live
  user project.

A final read-only check at 03:50:31 UTC confirms the same active release, healthy
API and six online agents with heartbeat ages below six seconds. This later
check does not recompare project history against the cutover snapshot, since
subsequent user work is permitted. Receipt SHA:
`1104359d0b37cafc942240f14043a2a1a3879cbe37f2243e070e853644d0c366`.

## iOS distribution

The archive uses mobile source `36537f2721a28ff2c43e50e6624c413fd6b48925` and
build `20260928030500`. Archive and distribution export both completed with exit
code 0. All 216 frozen mobile source files were unchanged.

The exported IPA is 45,698,075 bytes with SHA-256
`b979c29d2de81e38de2f814be13b64e7d39d60a79e58c4dbccf9641a7172b83d`.
The release audit confirms 14 routes including `website.tsx`, exact contents of
the 22 required source-map files, distribution signing and the required native
runtime checks. No physical-device execution is claimed. Precompiled React,
ReactNativeDependencies and Hermes frameworks still lack their vendor dSYMs;
the release archive and app symbols are retained.

Apple upload completed successfully in a single attempt (`altool` exit 0).
At 03:49:55 UTC, App Store Connect confirms **0.1.0 (20260928030500)**:
`VALID`, not expired, `IN_BETA_TESTING`, and available to the existing internal
`27pm` group. French Canadian test notes were applied and read back identically.
External beta distribution was not requested or submitted; its state is
`READY_FOR_BETA_SUBMISSION`.

The full private release summary and archive/export/upload/audit receipts are
under `~/Library/Developer/Xcode/SwarmerTestFlight/20260928030500/`.
The temporary signing keychain was cleaned up after successful export.
