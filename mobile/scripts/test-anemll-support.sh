#!/usr/bin/env bash
set -euo pipefail
MOBILE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE_DIR="$MOBILE_DIR/modules/swarmer-local-inference"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/swarmer-anemll-tests.XXXXXX")"
trap 'rm -rf -- "$BUILD_DIR"' EXIT
xcrun swiftc -swift-version 6 -strict-concurrency=complete -warnings-as-errors -parse-as-library \
  "$MODULE_DIR/ios/ANEMLLSupport.swift" "$MODULE_DIR/ios-tests/ANEMLLSupportTests.swift" \
  -o "$BUILD_DIR/anemll-tests"
"$BUILD_DIR/anemll-tests"
