# Mobile HF / Core ML delivery — 2026-10-03

The DEBUG iPhone app is built, signed, packaged and installed. Runtime validation is waiting for the phone to be unlocked. No TestFlight submission or new production qualification jobs/images were performed for this delivery.

## Included behavior

- Source commit `1dd455b6051f290deac06a457a7476af7af83e19` is committed and pushed. Hugging Face downloads now retain their original operation/result across screen navigation, reattach without starting another download, and deliver a completed import once to the current screen. Cancellation remains tied to that operation; failed progress reads do not imply completion. Import results wait for initial screen state before selection, and an already loaded generation model remains selected. This is in-process navigation recovery, not a claim of recovery after an app-process restart.
- DEBUG accepts fixture `dolphin-attention-int4-perchannel-cache28-two-blocks-independent`, with ten fixtures packaged. The fixture changes only the second block's four input bindings; the nine previous fixtures are preserved. CPU parity and negative controls are documented in [the fixture report](coreml-two-blocks-independent-2026-10-03.md). This does not prove ANE execution or resolution of full Dolphin's `-14` loading error.
- The built source also retains the media-status UI: blocked, skipped, permission/capability waiting, failed and cancelled states have distinct explanations, preserve useful errors and do not falsely promise automatic execution of a terminal step. Native audio symbols are present; this audit is not a playback test.

## Focused validation and build

The recorded focused checks passed: 148 Hugging Face JavaScript tests, 12 native HF tests, 101 Core ML/API tests, TypeScript and ESLint. Independent review passed. These are local tests; the report adds no new test execution.

The build used `mobile/scripts/build-automation-iphone.sh`, existing `SwarmerAPIBuild` DerivedData and `SwarmerAPIPackages`, locked package resolution (`-disableAutomaticPackageResolution -onlyUsePackageVersionsFromResolvedFile`), `-jobs 2` and `-allowProvisioningUpdates`. Xcode was selected through `DEVELOPER_DIR=/Users/ales27pm/Downloads/Xcode.app/Contents/Developer`.

Build ran from 10:57:02 to 10:59:30 UTC on 2026-10-03: exit **0**, **147.778 seconds**. Version `0.1.0`, build `20260930031000`, bundle `org.27pm.mongars`; these unchanged version strings alone do not identify this artifact. Source was uncommitted at build start and committed during the build; the receipt subsequently verified the exact **254 source files** against the intended source. **49 protected native files** were unchanged; the allowed generated Xcode-project change points the DEBUG resource-copy phase to fixture bundle v6. Pod locks and existing Core ML/HF native work were preserved.

## Artifact evidence

Private receipt directory: `/Users/ales27pm/Library/Logs/SwarmerQualification/Media/mobile-coreml-hf-20261003/`. No API session data or credentials are included here.

| Artifact | SHA-256 |
| --- | --- |
| `build-intent.json` | `254a8950159e8440b3d8202fa81dd372b048afc2278bbae109158b57043cb221` |
| `build-exit.json` | `6e66f2f532e8ff7ed61cd25538d0bfe198e2971e6aee1e78dd851321fd7fa118` |
| `build-receipt.json` | `0f71e10905236c3989d6334f87a32a5c57c3db53529c5b5300e6bbdb967f3b04` |
| `ipa-receipt.json` | `163293054b697a0f513ea30154af20c5ca70357b5b121d19b2b1d024bb2da302` |
| `ipa-audit.json` | `69d3b99c9aea92303ab31fbca7d10d3f82e50691de87787a409941ae3f12125a` |
| `monGARSSwarm-coreml-hf-20261003.ipa` | `beee4de61391a9712bc7cdef214e0a56c5cf4209ef74b503822b96c98b56dac9` |
| App executable | `7d2fa283486e7ba95a33a82baafba4d31596fc542dfcc1aff129e9d366a8e8fc` |
| DEBUG library | `c7e0198cfa3b83539796117ec14222a47824468fa633d67b6532bd991e133fa5` |
| Hermes JavaScript bundle | `571f755a130a6ace9ec26acb738602218022eb91f38ee83c23e452ae99b3e297` |
| Embedded ten-fixture manifest | `eebf6c018d6d7781d542d3c7c3b39a0ed465caa5b58bbb8ecdccd968871492f7` |

The IPA is **106,323,402 bytes**, streamed from the complete signed app without changing its bundle. Code signing passed. The IPA audit passed for seven Mach-O images with no reported issues; required routes/native automation symbols and the 5,805,730-byte Hermes bundle (bytecode version 96) were verified. All 88 fixture files are represented in the build receipt. The separate diagnostic-import manifest remains `a038a68a621ce188ed74022244efb6c37596efd6cc2eba2fe4d5c5d7c4e88c95`.

## Physical delivery — installed, runtime pending

The complete IPA was installed successfully through CoreDevice on the paired iPhone, bundle `org.27pm.mongars`. One fresh device-tunnel API launch then ended with “Console exited before HTTPS readiness”; no automatic relaunch followed and no Core ML probe command was sent. A subsequent read-only check confirms the tunnel is connected, the device is paired and booted with DDI services available, `passcodeRequired` is true, and no monGARS process is running. The user was asked to unlock the phone and open monGARS.

The installation receipt is `CoreML/two-attention-independent-20261003/device/install.json` under the private qualification root; the combined sanitized receipt is `delivery-attempt.json`, SHA-256 `a476e116272261bf4e4cd4fbdea3b7f8b1df9c689c33366bbe3a96176d356dd7`. Its hashes bind the installation, lock-state, connection and process observations. The failed launch’s temporary session file was removed; no credentials are committed.

This confirms installation only. Hugging Face download/navigation on-device, media rendering/playback and fixture 10 predictions remain unqualified on this build. Full Dolphin/ANE compatibility is still unresolved.
