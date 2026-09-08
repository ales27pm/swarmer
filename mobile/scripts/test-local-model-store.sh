#!/usr/bin/env bash
set -euo pipefail

MOBILE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE_DIR="$MOBILE_DIR/modules/swarmer-local-inference"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/swarmer-local-model-store-tests.XXXXXX")"
TEST_BINARY="$BUILD_DIR/local-model-store-tests"

cleanup() {
  if [ -n "${BUILD_DIR:-}" ] && [ -d "$BUILD_DIR" ]; then
    rm -rf -- "$BUILD_DIR"
  fi
}
trap cleanup EXIT

xcrun swiftc \
  -swift-version 6 \
  -strict-concurrency=complete \
  -warn-concurrency \
  -warnings-as-errors \
  -parse-as-library \
  "$MODULE_DIR/ios-tests/LocalModelStoreTestSupport.swift" \
  "$MODULE_DIR/ios/LocalModelStore.swift" \
  "$MODULE_DIR/ios-tests/LocalModelStoreTests.swift" \
  -o "$TEST_BINARY"

SWIFT_DETERMINISTIC_HASHING=1 "$TEST_BINARY"
