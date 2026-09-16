#!/usr/bin/env bash
set -euo pipefail
umask 077

MOBILE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE_DIR="$MOBILE_DIR/modules/swarmer-local-inference"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/swarmer-automation-tests.XXXXXX")"
trap 'rm -rf -- "$BUILD_DIR"' EXIT

xcrun swiftc -swift-version 6 -strict-concurrency=complete -warnings-as-errors \
  -D DEBUG -parse-as-library \
  "$MODULE_DIR/ios/AutomationProtocol.swift" "$MODULE_DIR/ios/AutomationServer.swift" \
  "$MODULE_DIR/ios-tests/AutomationProtocolTests.swift" -o "$BUILD_DIR/automation-tests"
"$BUILD_DIR/automation-tests"

xcrun swiftc -swift-version 6 -strict-concurrency=complete -warnings-as-errors \
  -D DEBUG -parse-as-library \
  "$MODULE_DIR/ios/AutomationProtocol.swift" "$MODULE_DIR/ios/AutomationServer.swift" \
  "$MODULE_DIR/ios-tests/AutomationTransportHost.swift" -o "$BUILD_DIR/transport-host"
python3 "$MODULE_DIR/ios-tests/AutomationTransportTests.py" "$BUILD_DIR/transport-host"

# Release must not expose a parser, listener, configuration or dispatch implementation.
xcrun swiftc -swift-version 6 -parse-as-library -emit-library \
  "$MODULE_DIR/ios/AutomationProtocol.swift" "$MODULE_DIR/ios/AutomationServer.swift" \
  -o "$BUILD_DIR/release.dylib"
if nm -g "$BUILD_DIR/release.dylib" | grep -E 'Automation(HTTPParser|Server|Configuration|Request)'; then
  echo 'FAIL: automation implementation present in Release' >&2
  exit 1
fi
echo 'PASS: Release excludes automation transport and parser'

# Check the actual deployment target and Swift concurrency against the installed iOS SDK.
IOS_SDK="$(xcrun --sdk iphoneos --show-sdk-path)"
xcrun swiftc -swift-version 6 -strict-concurrency=complete -warnings-as-errors \
  -D DEBUG -parse-as-library -typecheck -sdk "$IOS_SDK" -target arm64-apple-ios18.0 \
  "$MODULE_DIR/ios/AutomationProtocol.swift" "$MODULE_DIR/ios/AutomationServer.swift"
echo 'PASS: iOS 18 Debug transport typecheck'
