# TestFlight launch crash: ExpoContacts test framework dependency

Build `0.1.0 (20260912225400)` terminated at launch with `DYLD 1 Library missing`. Its `ExpoContacts.framework` requires `@rpath/Testing.framework/Testing`, which is not installed on the device. The crash's ExpoContacts UUID, `0FA206C4-5A55-3F0C-822A-15B62E3B37BA`, matches both the distributed IPA and the release prebuilt in `expo-contacts@57.0.5`.

The prebuilt binary contains a required `LC_LOAD_DYLIB` for the Swift Testing framework. The installed CocoaPods podspec excludes `Tests/`, and the shipped production sources have no Swift Testing import. The fix configures `expo.autolinking.apple.buildFromSource` for `expo-contacts`, so the app compiles the production Contacts sources instead of linking the defective prebuilt. Other packages keep their existing build configuration. This uses Expo's supported [per-module source-build setting](https://docs.expo.dev/guides/prebuilt-expo-modules/).

`eas-build-on-success` verifies the exported IPA's Mach-O dependencies. The same check runs independently on the downloaded artifact before a replacement is submitted to Apple. A successful archive or App Store Connect `VALID` state alone did not detect the original problem.

Regression evidence must include the old IPA failing specifically on the Testing dependency, and the replacement IPA passing the same check. Physical-device launch remains a separate validation step.

The old IPA was scanned across all 12 embedded Mach-O images. It failed with exactly one issue: ExpoContacts' required Testing framework link, matching the crash UUID. The production CocoaPods source selection contains 73 files and no test files or Testing/XCTest imports. The verifier's regression tests run in `scripts/check.sh` on both macOS and Linux.
