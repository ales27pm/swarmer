# Memory-context mobile build — 3 October 2026

An embedded-JavaScript DEBUG iPhone app was built and signed from commit
`243e913c3126a1cb15d290fbbac36e6f5024b7fe`, including the local-memory-context
changes in `c56e7eeacf80ab5f856d541a50b9b787abcf99d5`. The complete signed app was
also packaged as an IPA without rebuilding or signing it again. This report
establishes local build and package evidence; the candidate was not installed
or launched on an iPhone.

The behavior and earlier mocked/native-bridge tests are described in
[Memory evidence in local iPhone generation](memory-local-inference-2026-10-03.md).
This build does not add a physical inference or retrieval-quality result.

## Source and build

The private build input is under:

`/Users/ales27pm/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/transport-capability-20261003/mobile-build-243e913/`

The [preparation receipt][preparation] and [build command handoff][operator]
record an isolated `git archive` of the exact commit. All **271 tracked mobile
files** matched that archive and the prepared mobile snapshot before building,
and remained unchanged afterward. Dependencies and the existing generated iOS
project/Pods were copied into the private input with APFS clones. The mobile
`node_modules` is a real directory, not the development-tree symlink. The
primary checkout, model settings and existing build product were not changed.

The package lock SHA-256 is
`89b53989678f5f916f84d21f7038077dcea19140411b752a6fecbf72168010ec`.
The Podfile and installed Pods locks match at
`b527d9feca649b30a7aeae6dceb3c911357dc0fd46dfe809ddb88c521f244aa0`.
The resolved Swift-package file is pinned at
`7b37f3830d8d0f1ddaf4e1fec438f4c04b10f72e0cb0807f16bd31e5e6b97bdb`.

One build ran from **2026-10-04 00:08:03.996 to 00:35:36.724 UTC**
(**3 October, 20:08–20:35 America/Montreal**). It ended with **exit 0 / BUILD
SUCCEEDED**, in **1,652.735 seconds**. The [build intent][intent] contains the
exact command and the [exit receipt][exit] records the result. The build used
Xcode **26.3**, iPhoneOS SDK **26.2**, Node **22.23.2**, two compiler jobs, private
DerivedData and the existing resolved Swift-package cache. It did not use a
clean prebuild, install dependencies, download models or retry the build.

The existing `build-automation-iphone.sh` wrapper fixes `Debug`,
`GCC_OPTIMIZATION_LEVEL=3` and `SWIFT_OPTIMIZATION_LEVEL=-Onone`. The actual Cmlx
compiler response files contain `-O3`; native SwarmerLocalInference compilation
retains `DEBUG`. The MLX macro had already been authorized. No new global Xcode
trust setting was introduced.

The delegate loads `main.jsbundle` from the application bundle. The final Debug
bundle phase forces bundling with `--dev false`, and the generated Podfile
excludes Expo's development launchers. The build embedded **1,329 JavaScript
modules** and does not require a Metro server.

## Verification and preserved generated inputs

The [post-build verification][verification] completed at
**2026-10-04 00:42:38 UTC**. `codesign --verify --deep --strict` passed. The app
retains bundle identifier `org.27pm.mongars`, version **0.1.0** and build
**20260930031000**. These same version strings were used by earlier builds and
do not uniquely identify this candidate; use the source and artifact hashes.

The Hermes bundle is **5,856,236 bytes**, bytecode version **96**. Its inspected
string table contains all **14 required routes**, native automation markers,
`/memory/local-context`, `local-context-v1`, `symbolic-context-v1` and
`local_context_receipt`. This is packaging evidence, not proof that those flows
execute successfully on the device. Native ExpoAudio and SwarmerLocalInference
symbols are present, and the seven embedded Mach-O images have no unresolved
required dependency or bundled test-library issue in the repository audit.

The existing **10 Core ML fixtures / 88 files** are byte-identical in the app.
Their manifest remains
`eebf6c018d6d7781d542d3c7c3b39a0ed465caa5b58bbb8ecdccd968871492f7`.
The separate diagnostic-import manifest remains
`a038a68a621ce188ed74022244efb6c37596efd6cc2eba2fe4d5c5d7c4e88c95`.

Of **9,695 compared native input files**, **9,694** remained unchanged. React
Codegen regenerated only the relative React dependency path in
`ios/build/generated/ios/Package.swift`; the exact change is retained in
`build-01-generated-package.diff`. Independent review confirmed that this
generated SwiftPM manifest is not consumed by this Xcode build: none of the
twelve resolved Swift packages is React, the projects contain no local Swift
package reference, and the actual ReactCodegen/ReactAppDependencyProvider
targets come from Pods. The build log explicitly records generation of the
file. This difference is preserved in the verification receipt, rather than
reported as an entirely unchanged native tree.

## Signed artifacts and receipts

The app is retained at
`DerivedData/Build/Products/Debug-iphoneos/monGARSSwarm.app` beneath the private
input directory. `build-01.xcresult` and its file manifest are retained there.

The complete IPA, `monGARSSwarm-memory-context-243e913.ipa`, is
**99,513,702 bytes**. It was streamed from the already signed app; all **169
regular files** were checked against the app manifest after reading them from
the archive. The source app remained unchanged. The existing repository tool
`mobile/scripts/verify-ios-archive.py`, using the build's Hermes compiler,
returned **exit 0 / passed**, **7 Mach-O images**, and **no issues**. The
[IPA receipt][ipa-receipt] records the exact audit command and
[audit result][ipa-audit]. No Xcode distribution archive/export or new signature
was performed for this packaging step.

| Artifact | SHA-256 |
| --- | --- |
| App file manifest | `358702a45060a63d11d05a3051e812c295b519b7eecb3a263050ec08d4dc6662` |
| App executable | `33a8644f48265b71a5db4af49eb0111685cb46f144b40aad80925f1e900acd87` |
| DEBUG library | `de985fee74666157b113cc079e618c8003e68d55fc857b5f78ce8353f485b91b` |
| Embedded Hermes bundle | `051fe724f48e26f2aedde8876321dbab0482b928bf3ed88fd5df81ac1b51225d` |
| Complete IPA | `82c433e4534212b1f50454315de3c7b80dd81927bc82f872a281af7f2293a2ef` |
| Post-build verification receipt | `c6588a35278d1b8849025fe837fd73eac9637a6903a90eccccbb17fb12c4d9e9` |
| IPA receipt | `6774816200fd8952809b0f6770708583811a061647bc06cdec31451aef7863a1` |
| IPA audit | `8f462771ad9f1d48f295aff84e86b2e372be0c91399d666b701c07bf401ddab6` |
| Xcode log | `cf26854681f611223a5ca185421e7c85e75a742687c08548892568cb0453dc9f` |
| `.xcresult` file manifest | `b683fc3fffb10daabb654b801229d64a91d150c5c8332ac00a1f87536f49d8c6` |
| Installation handoff | `eee833cb6687a0366347505b8938331085eac3552594c0792a6ff66860d59ed5` |

## Device and release boundary

The [read-only device check][device] at **2026-10-04 00:38:09 UTC** did not
establish a usable iPhone connection: no iPhone USB transport was enumerated in
the VM, and the targeted CoreDevice details call timed out after five seconds.
The lock state was not established. This check must not be reported as proof
that the phone was merely locked or ready for installation.

The [installation handoff][installation] retains the app path and an unexecuted
install command. Installation requires fresh exact-device connection and DDI
readiness. Subsequent local-plan/tool qualification also requires the compatible
backend exposing `/memory/local-context`, a foreground unlocked app and a fresh
bounded application-API session. This report does not establish backend
activation.

No app installation, launch or termination, model inference, ANE execution,
TestFlight upload or App Store submission occurred in this build/package task.
There is no new on-device usability, model-quality or end-to-end success claim.

[preparation]: /Users/ales27pm/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/transport-capability-20261003/mobile-build-243e913/preparation-receipt.json
[operator]: /Users/ales27pm/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/transport-capability-20261003/mobile-build-243e913/operator-handoff.json
[intent]: /Users/ales27pm/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/transport-capability-20261003/mobile-build-243e913/build-01-intent.json
[exit]: /Users/ales27pm/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/transport-capability-20261003/mobile-build-243e913/build-01-exit.json
[verification]: /Users/ales27pm/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/transport-capability-20261003/mobile-build-243e913/build-01-verification.json
[ipa-receipt]: /Users/ales27pm/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/transport-capability-20261003/mobile-build-243e913/ipa-receipt.json
[ipa-audit]: /Users/ales27pm/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/transport-capability-20261003/mobile-build-243e913/ipa-audit.json
[device]: /Users/ales27pm/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/transport-capability-20261003/mobile-build-243e913/device-readiness-after-build.json
[installation]: /Users/ales27pm/Library/Logs/SwarmerQualification/Memory/goal-model-admission-20261003/transport-capability-20261003/mobile-build-243e913/installation-handoff.json
