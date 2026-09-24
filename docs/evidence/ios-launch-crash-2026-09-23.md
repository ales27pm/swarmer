# TestFlight startup crash: missing application routes

The TestFlight build `20260923215000` aborted about 0.42 seconds after launch on
an iPhone17,1 running iOS 26.7. Its application UUID matches the archived binary
and dSYM: `A1C0D74A-ABB2-3A2B-81CB-77D6526B3C8B`.

The native stack reports a React Native fatal JavaScript exception. The crash
report does not contain its JavaScript message. The `ggml_uncaught_exception`
frame is an exception termination handler, not proof of a model inference crash.

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

Private logs, reproducible compiled-context harnesses, raw source, and receipts
are retained outside Git under `~/Library/Logs/SwarmerCrash/20260923215000`.
No backend goal, generated project, model runtime, or user data was changed.
