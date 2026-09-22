# TestFlight release — 22 September 2026 (Montréal)

**monGARS Swarm 0.1.0 (20260922181000)** is available to the internal **27pm**
TestFlight group. At **2026-09-22T22:44:31.300Z**, App Store Connect reported
`VALID`, `expired=false`, `IN_BETA_TESTING` and membership of the exact build in
that group. External state remains `READY_FOR_BETA_SUBMISSION`; no external beta
review or public App Store submission was performed.

Mobile source: `7c51785b3b316d3ed88f29b5afc95854227335c2`.
Bundle: `org.27pm.mongars`.

## Scope and validation

The build adds the experimental E5 local-embedding controls in Settings, native
agenda client operations and compatibility with older servers that do not expose
the optional durable-project-context endpoint. E5 vectors do not automatically
replace the server's shared memory index. The new backend capabilities need their
separate deployment and activation.

The exact mobile source passed TypeScript, ESLint, 750 tests in 50 Jest suites,
Expo Doctor's 20 checks, eight compiled embedding-input validation cases, ten
archive-verifier regression tests and seven App Store metadata helper tests.

A separate worktree used the normal Expo Release project. All 179 tracked mobile
files matched Git before and after compilation and the IPA audit. The 12 SwiftPM
revisions stayed unchanged; MLXEmbedders was added from the already pinned
MLX Swift LM 3.31.4 package. The existing Debug project was preserved.

Xcode 26.3, iOS SDK 26.2, minimum iOS 18.0. The signed archive succeeded in
1,318.042 seconds; App Store export succeeded in 14.437 seconds.

## Export and delivery

IPA size: **43,392,772 bytes**. SHA256:
`924e25c4e2cf0450ff54b088c063521faa9bc1565a44e3864124ad9fae718383`.

The archive and IPA passed strict deep code-signature verification with matching
distribution certificate/profile, `get-task-allow=false`, the background GPU
entitlement and `ITSAppUsesNonExemptEncryption=false`. Their JavaScript bundle
hashes match. The independent audit passed all five Mach-O images, found the
embedding/Core ML/MLX/GGUF runtimes and MLX Metal library, and found no unresolved
bundled dependencies, test-only libraries, Debug listener markers or debug dylibs.
The development automation server is intentionally unavailable in this Release.

The app and llama dSYM UUIDs match. Prebuilt React, ReactNativeDependencies and
Hermes still lack matching supplied dSYMs; their symbolication coverage is limited.

A single `altool` upload completed in **68.836 seconds**, with
`UPLOAD SUCCEEDED` and exit code zero. Delivery UUID:
`3687332f-138c-4ba7-95ad-fdc12e7ed256`. No upload retry was performed.

The temporary signing keychain was removed successfully. Original credentials,
the installed profile and existing keychain search entries were preserved.
No physical iPhone installation or E5 inference test was performed: the user is
away from the development network and requested this TestFlight build to test it.

## Related work

Backend context, specialist tools and staged feature flags are committed separately
as `934ca84`; the CRM endpoint is `a78f799` on the existing CRM branch. Their
implementation qualification and remaining deployment gates are documented in
[the personal-assistant report](personal-assistant-2026-09-22.md). This TestFlight
upload did not restart Ubuntu, deploy CRM migrations or alter an existing goal.

Detailed receipts and artifacts remain outside Git under
`~/Library/Developer/Xcode/SwarmerTestFlight/20260922181000/`.
The unrelated untracked `swarmer-complete-source.txt` was preserved.

## Notes and receipts

The **fr-CA** What to Test notes were applied and read back exactly. They cover
E5 loading, local vector generation, unloading, persistence after relaunch and
existing app workflows. Notes SHA256: `9cb585f81361f27840b381f574d400c8085d7efa597bfff458ed39cda4639963`.

| Receipt | SHA256 |
| --- | --- |
| `source-qualification.json` | `68404abcab4affc1a9917f6b43afb8ba5916f49b4f6acc0825666a735ad1db12` |
| `archive-receipt.json` | `cf1564a39afd1e8d0aa226e4774da34f17753698538144de64b1e9ba3d536458` |
| `export-receipt.json` | `68c12d23f28c9370abe9bdcf9fc8a4c94e8aaa6857c2e8650e58c88a6f38f082` |
| `independent-ipa-audit.json` | `2d0d859892f48cf1f31d6fd2f7185470af570f0da25a9d2337a32e9a61f43a4c` |
| `apple-upload-confirmation.json` | `c0d55f0b1d874847b4fbc8f99a9d4d87e4a31639bed62292918a9b9711bc0f70` |
| `status-20260922181000.receipt.json` | `aadf87174a2ab45b27e459529b9e65d7bb6a60d44077d0c83abcd39430e80268` |
| `notes-20260922181000.receipt.json` | `b26d5789cc6bcf12170f2b2673e14567ee5877d40c3d38bd3d5d8640504b84ee` |
| `signing-cleanup-verification.json` | `32f9c91a391fd67bc38068212b0c449aa1a6b8add90cfc0d83b8be5d96125886` |
