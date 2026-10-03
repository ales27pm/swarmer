# Mobile media release readiness — 2026-10-03

## Scope and result

The mobile media integration passes its focused JavaScript verification and a new native iOS Debug build has been produced and audited. A native rebuild was required: `GoalMediaArtifacts` imports `expo-audio` eagerly, while the existing generated iOS project initially did not register `ExpoAudio`. An old binary cannot be qualified by a JavaScript update alone. No iPhone installation or physical media-preview result is established by this report yet.

The initial inspection was read-only. The release coordinator subsequently authorized a lock-preserving CocoaPods update and one embedded-JavaScript Debug build, preserving existing Core ML diagnostics and Hugging Face download work. No TestFlight upload is part of this lane.

## Local checks

| Check | Result |
| --- | --- |
| Four focused Jest suites: media transport, cache, component, goal-detail integration | **105 passed**, 25.069 seconds |
| `npm run typecheck` | Passed |
| ESLint on ten changed media/integration source and test files | Passed |
| Existing automation-build and preparation script tests | **15 passed**; mock Xcode only |
| Installed packages | Expo 55.0.31, expo-audio 55.0.18, expo-asset 55.0.20, @noble/hashes 1.8.0 |

Commands executed from `mobile/`:

```sh
npm test -- --runInBand src/lib/api/goal-media.test.ts src/lib/goal-media-cache.test.ts src/components/goal-media-artifacts.test.tsx src/screens/goal-detail.test.tsx
npm run typecheck
./node_modules/.bin/eslint src/components/goal-media-artifacts.tsx src/components/goal-media-artifacts.test.tsx src/lib/goal-media-cache.ts src/lib/goal-media-cache.test.ts src/lib/api/goal-media.ts src/lib/api/goal-media.test.ts src/lib/api/client.ts src/screens/goal-detail-content.tsx src/screens/goal-detail.test.tsx src/testing/goal-media-fixtures.ts
node --test scripts/test-build-automation-iphone.cjs scripts/test-prepare-automation-build.cjs
```

These checks use mocked media/network/native playback. They do not prove image rendering or audio playback on an iPhone. The initial shell resolved Node 20.20.2; the actual native preparation/build uses the project's existing Node 22.23.2 path, satisfying the declared engine requirement.

## Transport and lifecycle inspected

- The Results panel mounts media only for an authoritative server connection, a focused screen and a foreground app. Backgrounding, leaving the panel or changing projects disposes the preview and aborts outstanding reads.
- Artifacts are filtered to completed media jobs belonging to the displayed goal. Pairing changes fence the old response and require reopening the project.
- Bytes use an authenticated same-origin fetch, disallow redirects, enforce media type and bounded length, and verify SHA-256 and container metadata. The player receives a private local URI, not a bearer-bearing server URL.
- Native files live in a disposable cache; web previews use revoked-on-dispose object URLs. Audio is stopped on unmount and only one preview plays at a time.
- The Expo audio config explicitly disables microphone permission, Android recording and background playback/recording. The pre-existing generated Info.plist still contains its earlier microphone description, originating before this change; this preparation does not enable a recording feature.

## Findings and limits

1. **Native linkage required before launch.** `expo-audio/src/AudioModule.ts` calls `requireNativeModule('ExpoAudio')` during import. The existing Podfile.lock, Pods/Manifest.lock and ExpoModulesProvider initially omitted it. A binary containing the new JavaScript but old Pods would fail before an image-only goal could be evaluated.
2. **Status copy is incomplete.** `GoalMediaArtifacts` distinguishes failed, cancelled and running, but renders `blocked`, `skipped`, `waiting_permission` and `waiting_capability` as generic “en attente.” This can make a terminal or permission-blocked result look as though generation will continue. Add explicit labels and component coverage; no UI source was edited during readiness inspection.
3. There is no media save/share action in this preview. Display and playback are the current scope.
4. Historical device metadata from `CoreML/cache-slot1-20261002/device/post-probe-details.json` reports a paired, booted iPhone on the local network, tunnel connected, DDI available, iOS 26.7. Its file timestamp is **2026-10-02 07:28:36 UTC**. This is not current connectivity proof, and no device command has been sent by this readiness pass.

## Native preparation and build plan

Existing generated project is already prepared for embedded Debug JavaScript, retains `DEBUG`, and includes the nine Core ML diagnostic fixtures plus the candidate manifest. Do not run a clean prebuild over those local additions.

Private evidence directory:

`~/Library/Logs/SwarmerQualification/Media/mobile-20261003/`

The `before/` copy and `before-hashes.json` preserve Podfile, locks, PBX, delegate, local Xcode environment, package/config and 41 local-inference module files. Existing build receipts identify the reusable caches:

- Derived data: `~/Library/Developer/Xcode/SwarmerAPIBuild`
- Swift packages: `~/Library/Developer/Xcode/SwarmerAPIPackages`
- Xcode: `~/Downloads/Xcode.app/Contents/Developer`
- Node: `/usr/local/Cellar/node@22/22.23.2/bin/node`

The first CocoaPods invocation was refused because the executing process was root, before dependency installation. It was rerun as the existing developer user, without `--allow-root` or package updates:

```sh
sudo -n -u ales27pm env PATH=/usr/local/Cellar/node@22/22.23.2/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin COCOAPODS_DISABLE_STATS=1 DEVELOPER_DIR=/Users/ales27pm/Downloads/Xcode.app/Contents/Developer /usr/local/bin/pod install --no-repo-update
```

After CocoaPods finishes, compare locks and protected source hashes, confirm ExpoAudio registration and retain the Core ML bundle phases. Reapply only the existing idempotent Debug preparation if CocoaPods changes the bundling setup. Then build once with the existing wrapper, fixed Debug/C++ optimization, two compiler jobs and resolved Swift package pins. Verify code signature, embedded bundle, ExpoAudio linkage and unchanged nine-fixture manifest before producing a complete IPA.

### Preparation result

CocoaPods completed successfully in **47 seconds**. The lock diff adds only ExpoAudio 55.0.18 and changes the existing ExpoAsset 55.0.20 local path to its newly hoisted package. All other pod versions and checksums are retained. The generated provider now imports ExpoAudio. The PBX, delegate, Debug/Core ML bundle phases and all local-inference source hashes are unchanged. The idempotent preparation script passed again. The before/after lock copies and logs are retained in the evidence directory.

The single build was started using the existing caches, without a clean prebuild:

```sh
sudo -n -H -u ales27pm env PATH=/usr/local/Cellar/node@22/22.23.2/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin DEVELOPER_DIR=/Users/ales27pm/Downloads/Xcode.app/Contents/Developer /bin/bash /Users/ales27pm/swarmer/mobile/scripts/build-automation-iphone.sh -derivedDataPath /Users/ales27pm/Library/Developer/Xcode/SwarmerAPIBuild -clonedSourcePackagesDirPath /Users/ales27pm/Library/Developer/Xcode/SwarmerAPIPackages -disableAutomaticPackageResolution -onlyUsePackageVersionsFromResolvedFile -jobs 2 -allowProvisioningUpdates
```

Output is captured in `xcodebuild.log`; `build-intent.json` records 242 source hashes, native preservation hashes and the retained fixture/candidate manifests. The command is an embedded-JavaScript **Debug development build**, not a distribution archive or TestFlight submission. Its result and any installation must be recorded separately when available.

### Build and package result

The build completed with **exit 0 / BUILD SUCCEEDED**, approximately **570 seconds** after the intent was recorded. Independent local verification at **2026-10-03 08:50:24 UTC** confirmed:

- Signature verifies with `codesign --verify --deep --strict`.
- Native ExpoAudio and SwarmerLocalInference symbols exist in `monGARSSwarm.debug.dylib`. The small launcher executable delegates to this Debug library; checking only that launcher would incorrectly report missing symbols.
- All **242 source files** frozen at build start remain byte-identical.
- All **48 protected files** outside the two intentionally changed CocoaPods locks remain byte-identical.
- The complete **nine-fixture / 73-file** Core ML tree is byte-identical, including its manifest; the separate diagnostic candidate manifest is unchanged.
- The complete signed IPA passes `mobile/scripts/verify-ios-archive.py`: **7 Mach-O images**, no issues, native dependency resolution and the embedded Hermes application contract verified.

Artifact:

`~/Library/Logs/SwarmerQualification/Media/mobile-20261003/monGARSSwarm-media-20261003.ipa`

Size: **96,820,907 bytes**. SHA-256:

`4d7024461af1f657e8a284fbb4e4079557c5a936c6a4ca91dbb38df724ff4b5f`

The IPA is owned by the existing developer user with mode 0600. Receipts are `build-exit.json`, `build-receipt.json`, `ipa-receipt.json`, and `ipa-audit.json` in that same private evidence directory. It remains version 0.1.0 / build 20260930031000, so qualification should use the artifact and JavaScript hashes rather than the unchanged marketing/build strings. This is not a Store release.

The coordinator owns subsequent device discovery, installation and API qualification; this build task sent no commands to the iPhone.

Installation follows only after a fresh device-availability check. The physical qualification must open an owner-authorized generated image in Results, exercise retry/offline handling and background/foreground disposal, and confirm the same artifact digest as the backend. Audio playback remains a separate runtime check and does not imply enabling an audio-generation server capability. No historical session token or tunnel address should be reused as current connectivity evidence.

## Physical iPhone installation and startup — root follow-up

The complete signed IPA (SHA-256
`4d7024461af1f657e8a284fbb4e4079557c5a936c6a4ca91dbb38df724ff4b5f`)
was installed successfully through CoreDevice on the paired iPhone 16 Pro.
A fresh device-tunnel launch produced verified HTTPS health, foreground access
and zero active API jobs. At `2026-10-03T08:52:50.081Z`, `app.status`
succeeded with platform iOS, appState active, pairedCredentialStored true
and localInferenceAvailable true. No local model was loaded.

This confirms physical installation and startup of the Debug build, retaining
the existing native build number `20260930031000`. It is not a TestFlight
upload, Core ML/ANE qualification, or proof of image rendering/audio playback.
Private installation and API session receipts are under
`~/Library/Logs/SwarmerQualification/Media/device-20261003/`; the session
contains short-lived credentials and must never be committed.
