#!/usr/bin/env bash
set -euo pipefail
MOBILE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE_DIR="$MOBILE_DIR/modules/swarmer-local-inference"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/swarmer-background-generation-tests.XXXXXX")"
trap 'rm -rf -- "$BUILD_DIR"' EXIT
xcrun swiftc -swift-version 6 -strict-concurrency=complete -warnings-as-errors -parse-as-library \
  "$MODULE_DIR/ios/BackgroundGenerationController.swift" \
  "$MODULE_DIR/ios-tests/BackgroundGenerationTests.swift" -o "$BUILD_DIR/tests"
"$BUILD_DIR/tests"
xcrun swiftc -swift-version 6 -strict-concurrency=complete -warnings-as-errors -typecheck \
  -target arm64-apple-ios18.0 -sdk "$(xcrun --sdk iphoneos --show-sdk-path)" \
  "$MODULE_DIR/ios/BackgroundGenerationController.swift"
