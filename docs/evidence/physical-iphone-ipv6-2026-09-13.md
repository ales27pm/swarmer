# Physical iPhone validation over IPv6

## Environment and installation

- Built on the local Intel iMac, macOS 15.7.9, Xcode 26.3 (17C529), iOS SDK 26.2.
- Physical iPhone 16 Pro, iOS 26.6.1, Developer Mode enabled.
- Refreshed the existing trusted pairing, then acquired an actual CoreDevice
  network tunnel. Developer disk image services, installation and launch all worked.
- The device tunnel and the active UI runner connection used native IPv6.
  Tunnel addresses changed during the session; each endpoint was discovered live.
- The authenticated backend also returned HTTP 200 over its native Tailscale IPv6
  address, with the HTTPS hostname preserved for certificate validation and SNI.

The first device build re-exported the validated SDK 55 archive for development:
`org.27pm.mongars`, version `0.1.0`, build `20260913004000`, mobile tree
`2459df321aa4b732bf708681653d3e2333e0fad8`. It includes bundled JavaScript and needs
no Metro server. Its development profile includes the target iPhone, and
`get-task-allow=true`. Strict signatures and the five-image dependency guard pass.
It was installed as an update, retaining authentication and existing app data.

## Observed on the phone before the keyboard correction

- The app opened without the previous missing-framework launch crash, restored
  authentication and reached the connected realtime state.
- Core ML, MLX and llama.cpp were available in the native runtime selector;
  switching runtimes did not load a model automatically.
- Saved 128 output tokens and temperature 0; both values survived leaving and
  reopening the model screen. A zero-token limit was rejected. French decimal
  input `0,1` saved successfully and reopened as `0.1`.
- Restored the original MLX Dolphin preset, immutable revision, 256-token limit
  and temperature 0.1 after the settings checks.
- Loaded `mlx-community/dolphin3.0-llama3.2-3B-4Bit` at revision
  `cdc777b578ff86a69f1b05c9bc00df0cdc2f52d1` on the physical device.
- Two local generations each produced a valid 43-token `workspace.list_dir`
  proposal with `path: "."`. No proposal was submitted or executed. The warm
  result was observed within 22 seconds, including automation overhead; this is
  not an engine latency benchmark.
- A third generation was cancelled through the UI, and the controls became
  available again. Explicit model unload then returned its success state.
- Existing task and swarm screens remained accessible. No existing project
  reply, approval, cancellation or replan was sent by these tests.

## Reproduced keyboard issue

An unsent six-line local prompt (87 characters) left only the first line visible
above the keyboard. The caret and generation button were covered. The numeric
settings keyboard could also cover the save button, particularly after a
validation banner changed the content height. These are physical observations,
not failures inferred from component tests.

The correction groups each input with its nearby controls and measures that
group against the visible keyboard viewport. It follows multiline layout,
selection and keyboard-frame changes, ignores stale measurements after blur or
keyboard dismissal, and adds an explicit keyboard-close action for numeric
settings. Oversized groups reveal the active input without oscillating between
opposite edges.

Source commit `667fd997ec3a4769e4523de2b8f1b169cdc11294` reserves build
`20260913203646`; its mobile tree is
`e7b0ef5e775a4e0b0725815f15757958c5bb278a`.
The independent review's two findings (oversized-group oscillation and stale
keyboard-frame metrics) were corrected before this commit. Focused tests pass
93/93, TypeScript and targeted ESLint pass, and the full mobile suite passes
398/398 across 32 suites in 27.913 seconds.

## Corrected development build

The corrected Release development build succeeded locally in 697.7 seconds.
The build used the frozen source commit and verified 112 mobile source hashes.
It reused the existing native dependency cache without cleaning it. Two initial
attempts encountered generated archive-product symlinks; repairing only those
generated outputs allowed the build to finish. No source compilation error was
reported.

- Build: `20260913203646`; bundle: `org.27pm.mongars`; minimum iOS: `18.0`.
- Development IPA: 23,703,939 bytes, SHA-256
  `4de13baf5089f809484deb49f14c1ba94d49fe32afce4f67a78942b8a18f589a`.
- Bundled JavaScript: 3,230,798 bytes, SHA-256
  `38d8bab1dab97e9157d6c4afb47affcbac4769033e60bd5ebcb29da0a6fa7c76`.
- Development signature verified, target iPhone included in the provisioning
  profile, and `get-task-allow=true`.
- The five-image native dependency guard passes. Contacts, Core ML, MLX and
  llama.cpp components remain present.
- Application and llama dSYM UUIDs match their packaged binaries. A host macro
  dSYM was also retained. Matching React, ReactNativeDependencies and Hermes
  dSYMs were not available in these build outputs.
- The temporary builder was restored; the previous release archive and IPA
  remained unchanged.

The corrected app is ready at
`/private/tmp/swarmer-ios-development-20260913203646/installable/Payload/monGARSSwarm.app`.
The audit receipt, source manifests, signing checks and dSYMs are retained in the
same build directory.

The iPhone became unavailable on both the local network and Tailscale before
this build finished, then returned during archive preparation. Initial control
channel attempts reset the connection, but subsequent fresh diagnostics and
device details confirmed a connected native IPv6 tunnel and available developer
disk image services. CoreDevice installed the corrected app successfully as an
update. The app opened with authentication and the original Dolphin MLX preset
retained. The earlier MLX generation observations above apply to build
`20260913004000`.

On corrected build `20260913203646`, physical screenshots confirm:

- Focusing the numeric output-token field reveals both inputs and the
  **Enregistrer les réglages** and **Fermer le clavier** buttons fully above
  the keyboard, without a manual scroll after focus.
- Pressing **Fermer le clavier** dismisses the keyboard. The original values
  remain 256 tokens and temperature 0.1; no settings write was needed.
- Filling the same unsent 87-character, six-line local prompt reveals every
  line, the final caret and the generation button above the keyboard, without
  a manual scroll after typing. Generation remains disabled because the model
  is unloaded; no proposal was sent or executed.
- Filling the initially empty project composer with a 94-character, six-line
  draft reveals all six lines, the final caret, the `94/4000` counter and the
  **Envoyer au projet** button entirely above the keyboard, without a manual
  scroll after typing. No project message was sent. The test draft was then
  erased; a fresh scoped snapshot verified an empty input, `0/4000` and a
  disabled send button before closing the test session.

The evidence images are `keyboard-corrected-numeric.png`,
`keyboard-corrected-multiline.png` and `keyboard-corrected-project.png` in the
thread's September 13 visualization directory.

## Distribution and TestFlight publication

The source correction was pushed without force to both `origin/main` and
`vibecode/main`; direct remote queries confirmed commit `667fd997` on both.
App Store Connect was queried before publication and did not contain build
`20260913203646`.

A matching, unexpired App Store provisioning profile and existing distribution
identity were found. A new archive/export/upload workflow was prepared and
independently reviewed. Its inputs match the frozen mobile tree and its guards
preserve the previous release and prevent a duplicate upload.

Two bounded signing probes for the login-keychain distribution identity timed
out after 30 seconds. The second followed a successful interactive unlock of
the login keychain. A third probe observed an actual codesign permission dialog
and granted that request once, but still expired after 120 seconds while other
macOS keychain dialogs were present. The probe retained its original Apple
signature, so no successful distribution signing is inferred from the UI
action. All probe processes were terminated by their wrappers. No login
keychain access rules, certificates or profiles were changed.

The existing EAS temporary-signing workflow provided a separate recovery path.
Its distribution certificate was present and unexpired in App Store Connect;
the profile was active, matched the EAS bytes and the exact app bundle, and
expired on September 8, 2027. A dedicated temporary keychain successfully
signed a control binary; strict verification and the signing certificate match
passed. The temporary P12 was removed and the login keychain access rules were
left unchanged.

A new official archive succeeded from the same frozen 112-file source
manifest. Its independent native audit passes: distribution signature,
provisioning profile, entitlements, five packaged Mach-O images and matching
application/llama dSYMs. No Testing/XCTest dependency is present.

The archive and development Hermes bundle hashes differ. Comparing their
complete disassemblies shows exactly one changed debug-filename entry:
`/tmp/.../main.jsbundle` becomes `/private/tmp/.../main.jsbundle`. Source hash,
instructions, functions, string tables and other metadata are identical. The
archive bundle was not edited or replaced. This checked semantic equivalence
permits export of the original archive.

The same textual cache-path change caused avoidable native recompilation;
future development and archive commands should use the same path spelling.

The official archive finished in 24 minutes 3 seconds, and distribution export
passed. One upload completed successfully; App Store Connect independently
reported `VALID`, nonexpired, and internal `IN_BETA_TESTING`.

- App Store Connect app: `6809592768`; version: `0.1.0`; build: `20260913203646`.
- Delivery/build ID: `36d9dad2-1511-42e1-aa26-8ac2d27cf566`.
- Distribution IPA: 19,806,387 bytes, SHA-256
  `d6f76ae08fcd5eba155f10b2676f340ea235bda62478bda3b6266f9700ab5b01`.
- French Canadian TestFlight notes were published and read back exactly,
  including the three physical keyboard checks. Localization ID:
  `7a8b42bb-a606-4c7a-b9e2-4e78f0d8fbc3`.
- External status remains `READY_FOR_BETA_SUBMISSION`; neither external beta
  review nor public App Store review was submitted.
- After upload, the temporary signing keychain and its search-list entry were
  removed. Other current entries and login access rules were preserved, and
  the temporary P12 was absent. The previous release archive and IPA remain
  unchanged.

The release kit, native audits, upload result and final App Store Connect receipt
are retained at `/private/tmp/swarmer-ios-release-20260913203646`, particularly
`release-receipt.json` and `release-evidence.md`. The distribution IPA is
`swarmer-20260913203646.ipa` in that directory.

## Evidence boundaries

The initial native UI runner later lost its session while inspecting the swarm
screen. A fresh CoreDevice process query confirmed that the app was still running.
The final failed snapshot could not connect before sending its command; recovery
started a runner whose listener then failed with `EADDRINUSE`. Earlier snapshots
had succeeded despite warnings about the system DragUI process, so those warnings
do not establish an app crash or the cause of the failed session. Earlier
individual UI results remain valid. An interrupted automation runner is not a
passing XCTest suite.

During the corrected-build retest, one snapshot deadline expired while the
runner was still starting. A later Back action exceeded its wrapper deadline
while XCTest waited for UI inactivity, then completed after the timeout; a
fresh screenshot confirmed the resulting Settings screen. These are automation
timing failures, not evidence of an application crash. Fresh snapshots and
screenshots subsequently established the three keyboard results above.

Core ML and GGUF generation, sustained memory/thermal behavior and broad model
quality are not established by the earlier MLX smoke test. Inference was not
repeated on the corrected build. Physical UI results apply to the development
artifact; the separately signed distribution artifact passed archive, dependency,
upload and App Store Connect processing checks.
