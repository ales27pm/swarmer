# TestFlight startup crash: missing application routes

The TestFlight build `20260923215000` aborted about 0.42 seconds after launch on
an iPhone17,1 running iOS 26.7. Its application UUID matches the archived binary
and dSYM: `A1C0D74A-ABB2-3A2B-81CB-77D6526B3C8B`.

The native stack reports a React Native fatal JavaScript exception. The crash
report does not contain its JavaScript message. The `ggml_uncaught_exception`
frame is an exception termination handler, not proof of a model inference crash.

A subsequent physical-device reproduction on the installed TestFlight build
confirmed the exact exception in its console:
`RCTFatalException: Unhandled JS Exception: Error: No routes found`, followed by
signal 6. CoreDevice's network tunnel was connected and developer services were
available for this capture. No app installation or data reset was needed.

## Reproduction from the distributed artifact

The archived Metro source contains an empty Expo Router context (`keys() => []`).
Executing that exact context and Expo's compiled `getRoutes` returns `null`.
The corresponding production RouterStore branch throws `No routes found`.
Recompiling the archived source with its original Hermes compiler and options
produces byte-for-byte identical bytecode to the published app bundle.

| Artifact | SHA-256 |
| --- | --- |
| Archived pre-Hermes JavaScript | `4196f6279a4fd55fc49cbace36dfd0e7833858f48944dd7e482af3839d4625e7` |
| Published Hermes bundle | `fc61f4fde5466eb274e6357817a9b41a9c3f952b26eb935304f91308d63d3cd5` |

The broken bundle has 1,010 Metro modules and no application routes. The previous
build and fresh exports of the same source contain all 13 routes and 1,170
modules. Neither changing `CI=1` nor re-running the original export reproduces
the missing routes. The upstream reason the first Metro invocation produced an
empty context remains unproven. Both release snapshots shared a `node_modules`
directory by symlink; that fact alone does not establish causality.

## Packaging correction

- Metro now rejects an iOS production graph missing any required route, the
  application API bridge, or the live-sync provider. Checking source files alone
  would miss this failure because they already existed in the failed snapshot.
- iOS Release snapshots must contain their own dependency directory; the guard
  rejects sharing the entire directory through a symlink. This isolates release
  packaging from development and other snapshots.
- The existing IPA verifier now disassembles the actual `main.jsbundle` and
  checks its Hermes string table against a shared route/native API contract.
  Resource files or approximate byte-size thresholds cannot satisfy this check.
- The existing EAS completion hook invokes the enhanced verifier automatically.

## Verification

- The previous verifier accepted both the broken and working IPA.
- The enhanced verifier rejects `20260923215000` with all 13 routes and four
  native API literals missing; it accepts `20260922233500`.
- Metro graph regression tests cover complete, empty, partial, wrong-checkout,
  and missing-bootstrap graphs, plus dependency isolation and unaffected
  development/web/Android/server graphs.
- A real iOS production export with the Metro guard produces 1,170 modules and
  all application routes. Archive inspection and route discovery are packaging
  evidence, not substitutes for launching the replacement on a device.
- The 25 archive-gate tests, seven Metro-guard tests, and 19 existing bootstrap,
  network-bridge and live-sync tests pass. The latter mock native integrations;
  their success alone did not and cannot prove a packaged app launches.

Private logs, reproducible compiled-context harnesses, raw source, and receipts
are retained outside Git under `~/Library/Logs/SwarmerCrash/20260923215000`.
No backend goal, generated project, model runtime, or user data was changed.

## Replacement archive: 20260924001000

Source commit: `e6ed0070e83fc226962b49fea50a276fcb19801a`. The isolated Release
archive succeeded in 1,439.301 seconds with all 189 tracked mobile source files
unchanged. The dependency directory is physical, production environment flags
are explicit, and the archive's source map is retained outside Git.

The distribution IPA passes both the independent release audit and the enhanced
Mach-O/Hermes verifier. The actual archived route context also passes the
13-route discovery smoke. Its hashes are:

| Artifact | SHA-256 |
| --- | --- |
| Distribution IPA | `319f0a461896c8173d931aea1b90d2b05cd0c750c36c09d01151d582dd5f906b` |
| Bundled Hermes bytecode | `7282798a22e73ce26340a82326b194408dd69da343c1ccc9602952cb3b0ef1ae` |
| Archive source map | `c2fa09122670e1ef5a5d321b23437e51f2b7533bbe2317758aaf96948bf803af` |

A development-signed derivative was exported from the same immutable archive
and independently checked for identical JavaScript, valid signatures, a matching
certificate/profile, and preserved application/team/keychain identifiers. Its
installation did not proceed: the CoreDevice network connection became
unavailable before an installation session could be established. The user then
requested TestFlight delivery without waiting for the physical test.

**The replacement's startup has not been tested on the physical iPhone.** The
device reproduction above concerns the broken installed build, while the new
build's evidence covers compilation, packaging, route discovery and signatures.

## TestFlight delivery

The distribution IPA was uploaded once, successfully, in 80.600 seconds. App
Store Connect readback at `2026-09-24T00:56:30.547Z` confirms build
`20260924001000` is `VALID`, non-expired and `IN_BETA_TESTING`, with verified
membership in the existing internal group `27pm`. The French Canadian test
notes were applied and read back successfully. Their SHA-256 is
`ed6f6ccdee5d345d6cd540ad224febced170e49223e78efefe0032e9c18e6c66`.

The notes explicitly ask for startup verification on the user's iPhone. Apple
processing and internal TestFlight availability do not establish device runtime
success. Upload, processing, group membership and notes receipts are retained
under `~/Library/Developer/Xcode/SwarmerTestFlight/20260924001000`.
