# UI and activity release — 24 September 2026

## Source and verification

Mobile source: `b4d5e47a0f4e821f1be768747a5bfb3119eb91cc`, pushed to GitHub and Vibecode `main`.

- 873 mobile tests passed across 56 suites; TypeScript, ESLint and diff checks passed.
- Browser inspection of the real Expo app at 393 × 852 verified navigation, illustrations, grouped settings, filters and disconnected states. No production browser pairing or user task was created.
- A separate cross-language test generated six activity pages from a temporary Python/SQLite backend and parsed them with the actual mobile TypeScript parser: eleven rows, all five operation kinds, no production access.
- The [design and contract note](../design/ui-activity-refactor-2026-09-24.md) describes behavior and limitations. The [asset note](../design/ui-assets-2026-09-24.md) preserves exact Imagegen prompts.

## Backend activated

The runtime backport is `5cc1e3d5c86a735a65be0f78641e073ed0d163cb`, retained on `release/activity-evaluator-20260924` on both remotes. Its parent is the deployed `0a4c9e0151dc1282ce19fec2761d0035b22fc1e3`. Only four runtime files changed: the activity routes, activity contracts, activity projection and evaluator consistency guard. This is not a deployment of every pending server change on main.

Activation waited until the existing work became idle; no active goal was cancelled. The supervised cutover completed successfully. Independent verification at `2026-09-24T06:54:08.082773+00:00` confirmed the candidate release, healthy API version 0.14.2, six fresh agents and exact preservation of all 37 protected history fingerprints. Database schema 26, model settings and worker sources remain unchanged. The exact predecessor rollback is retained. A later check at 07:05:47 UTC confirmed stable processes, six fresh agents and no 5xx or exceptions from the current API process. The predecessor logged one `database is locked` exception during the guarded cutover at 06:52:59 UTC; this transient error is retained in the release evidence, not counted as a clean uninterrupted rollout.

The candidate projection was checked read-only against twenty existing goals and twenty tasks, including fourteen second pages, with no errors. Both public activity routes correctly returned 401 without authentication. A production HTTP 200 using an existing device token was **not** tested because no reusable device credential was available; no device was paired for this check. Authenticated route behavior has isolated test coverage.

The isolated backport suite passed 254 tests and reproduced two preexisting `test_evaluator_wire_schema_preserves_local_string_limits` failures with the original live evaluator. The 69 deployment-helper tests passed. No inference call was made by deployment checks. The new evaluator grammar has not yet been exercised with a real Ollama response; the earlier semantic model qualification failures remain unresolved and no model role was switched.

Private receipts are retained in `~/Library/Logs/SwarmerDeploy/activity-evaluator-20260924/`: `baseline.json`, `cutover.json`, `independent-verification.json`, `helper-manifest.json`, `activity-smoke.json` and `late-read-only-20260924T070546Z.json`. These paths are evidence locations, not credential exports.

## iOS delivery

Build `20260924063000` archived successfully from the pinned mobile source. The wrapper exited 0 after 1,818.969 seconds; all 199 pinned source files were unchanged. The final source map contains 8,144,581 bytes with SHA-256 `6534e385af47075285bc561d7efa354f106085c1586549e64c118733b078d749`. The private `archive-receipt.json` is retained under `~/Library/Developer/Xcode/SwarmerTestFlight/20260924063000/`.

The signed export passed in 23.173 seconds. The IPA is 45,640,231 bytes with SHA-256 `3cb74e8b82c171e69daef894b1c7e0fcb5bc402c1c858769acccf09f24ab408e`. The independent archive/IPA audit passed at 07:17:09 UTC: thirteen routes, Hermes bundle/bootstrap, exact source content for all four main screens, exact bytes for both generated illustrations, strict signatures, release-only native compilation and matching app/llama dSYMs. The three prebuilt React, ReactNativeDependencies and Hermes frameworks still have no matching dSYMs; their symbolication coverage remains incomplete. Receipts: `export-receipt.json` and `independent-ipa-audit.json` in the same private lane.

Apple accepted the single upload at 07:19:13 UTC. The uploader exited 0 after 67.441 seconds; the success marker and uploaded IPA hash are retained in `upload-receipt.json` and `upload-evidence.json`.

App Store Connect verification at `2026-09-24T07:21:33.247Z` confirmed `VALID`, not expired, and `IN_BETA_TESTING`, with membership in the internal **27pm** group. Build **0.1.0 (20260924063000)** is available for internal TestFlight testing. French Canadian release notes were applied and read back with SHA-256 `2400cb3648838261ed4f75cc3804ded2bd57c302bcdbbcaa19296f72eae14836`. The retained receipt is `status-20260924063000.receipt.json`. External beta review was not submitted; the external state is `READY_FOR_BETA_SUBMISSION`.

The temporary signing keychain was cleaned up after upload. The compilation required clearing explicitly scoped obsolete compilation caches; source files, prior archives, IPAs, dSYMs and source packages were preserved. Physical iPhone execution of this new UI has **not** been performed.

## Physical acceptance checks still to perform

1. Visit Assistant, Activité, Équipe and Réglages; verify labels, safe areas, scrolling and return navigation on the physical phone.
2. Open an existing task or project and expand “Opérations détaillées”. Verify recorded model/tool/check rows, loading another page and refresh after leaving and returning to the app. This check does not start a new task.
3. Open the grouped connection settings, edit a draft, collapse and reopen the group, and confirm the draft remains intact before deliberately saving any change.
4. Expand and collapse a long project objective, inspect additional task filters and check the illustrations with the device's current text-size setting.

These are acceptance checks for the new interface and activity contract. They do not qualify a different local model or sustained background inference.
