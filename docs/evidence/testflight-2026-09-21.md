# TestFlight release — 21 September 2026 (Montréal)

**monGARS Swarm 0.1.0 (20260921232500)** is available to the internal **27pm**
TestFlight group. At **2026-09-22T00:04:57.866Z**, App Store Connect reports
`VALID`, `expired=false` and `IN_BETA_TESTING`; the exact build is present in that
group. The external state remains `READY_FOR_BETA_SUBMISSION`.
No external beta review or public App Store submission was performed.

Source: `aa67956d81b70bf593a64d90abcdaecac1b3cd39`, bundle `org.27pm.mongars`.
This commit advances the build number after the
[local web research release](local-web-research-2026-09-21.md).

## Validation

- Mobile: **723 tests in 47 suites passed**, none skipped; TypeScript and ESLint
  passed; Expo Doctor **20/20** under the actual build user.
- Archive-verifier regression suite: **10 passed**. Two isolated helper tests
  also confirm that an unsuccessful IPA audit blocks upload and an export timeout
  does not print authentication arguments.
- A separate worktree at the exact source commit was prepared with the normal
  Expo native project. The existing Debug automation project was preserved.
  All 169 tracked mobile source files matched Git before and after archive/audit.
- Xcode 26.3, iOS SDK 26.2, deployment target iOS 18.0. CocoaPods and the 12
  SwiftPM revisions were verified against the selected dependency snapshots.
- Release archive succeeded in **1,148.110 seconds**. Official App Store export
  succeeded in **11.269 seconds** using the installed manual signing assets.

## Exported artifact

The IPA is **43,163,821 bytes**, SHA256
`bf3258caa7ba4aea84f480014eec68a9bfc249806fb368c79f10f5a1795a2e07`.

The actual archive and exported app both passed strict deep signature verification,
matching certificate/profile/application checks and expiry checks. Both carry
`get-task-allow=false`, the background GPU entitlement and
`ITSAppUsesNonExemptEncryption=false`. Their JavaScript bundle hashes match.

The canonical IPA audit checked all **5 Mach-O images** with no unresolved bundled
or test-only dependencies. Core ML, MLX and GGUF runtime markers are present, as is
the MLX Metal library. The app and native inference module were compiled optimized
without `DEBUG`; no Debug listener markers or debug dylibs are present.
The TestFlight build does not expose the development automation API.

The app and llama dSYM UUIDs match their binaries. Prebuilt React,
ReactNativeDependencies and Hermes frameworks lack matching dSYMs; symbolication
for those frameworks remains limited.

## Delivery and notes

One `altool` upload returned **UPLOAD SUCCEEDED with no errors** in **74.176 seconds**.
Delivery UUID: `ab38cdbf-e64f-4c0c-8c9d-ff99a8744300`. No upload retry occurred.
Apple subsequently confirmed `VALID`, and a separate App Store Connect lookup
confirmed the internal group membership and testing state above.

The receipt dated **2026-09-22T00:04:43.000Z** confirms that the **fr-CA** What to
Test notes were patched and read back exactly. They describe local SearXNG research, linked sources,
agent selection, project resumption and local model loading. Notes SHA256:
`841fe71377a56454d8c6b227468c2542dd47cc5e6243af7586edd1e544498849`.

The first export attempt waited in Xcode's version-analysis request and was stopped
before any upload after 366 seconds. Its logs are retained. Exporting the same
archive with the same manual-signing plist, without provisioning-update/API flags,
succeeded; API authentication was used for the subsequent upload and metadata.

The first audit stopped on an invalid certificate-extraction argument in the audit
helper. Correcting it to `--extract-certificates=<prefix>` allowed the complete
audit to pass on the unchanged IPA. The failed receipt is retained.

The signing keeper exited cleanly. Its temporary keychain and P12 were removed;
other keychains/search entries, original credentials and the installed profile
were preserved. No physical iPhone installation, launch or background inference
measurement was performed for this Store build. This release did not restart the
backend or change an existing user goal.

## Private receipts

Artifacts and detailed receipts are retained outside Git under
`~/Library/Developer/Xcode/SwarmerTestFlight/20260921232500/`.
No signing material, IPA, archive or distribution logs are committed.

| Receipt | SHA256 |
| --- | --- |
| `archive-receipt.json` | `e2a9493b13d154bab83a2f72bbb8860cf0f6fa2323be0283dc303f61707e340b` |
| `export-receipt.json` | `a66ceff57dfcf8454f900c91cbcf7d325ae2c4306da009ae4a568e77ed92e465` |
| `independent-ipa-audit.json` | `502a67a1fe5fab2425863f3c2225be9394d4c0527294bfea35973d6fcd12d520` |
| `apple-upload-confirmation.json` | `7c51825e1876df181d39a9d57c5db909308672e0c32b867144e4e8adef7c58ab` |
| `status-20260921232500.receipt.json` | `9fa3e18d2ff7bc9bcf81c87b32f402042c56aed175011d33d4f5935f0780c0a4` |
| `notes-20260921232500.receipt.json` | `972a194b4ca65524de37a7d02a308d386d83e9375d583ff4d9e56510f8fbb545` |
| `signing-cleanup-verification.json` | `2c1b335521962d6c78bef4cb4558cf77b003ee9d7a8f5e9a73979d388e10940e` |

The mobile gate receipt is
`~/Library/Logs/SwarmerDeploy/testflight-aa67956-mobile-gates/receipt.json`, SHA256
`0366f4f9a32c1c6bbc364f98a0243cea7df672f63fa4f4cec4975c7a0b555736`.
The unrelated untracked `swarmer-complete-source.txt` was preserved.
