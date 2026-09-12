#!/usr/bin/env bash
set -euo pipefail

MOBILE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE_DIR="$MOBILE_DIR/modules/swarmer-local-inference"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/swarmer-coreml-dolphin-tests.XXXXXX")"
trap 'rm -rf -- "$BUILD_DIR"' EXIT

xcrun swiftc \
  -swift-version 6 \
  -strict-concurrency=complete \
  -warnings-as-errors \
  -parse-as-library \
  "$MODULE_DIR/ios/CoreMLDolphinSupport.swift" \
  "$MODULE_DIR/ios-tests/CoreMLDolphinSupportTests.swift" \
  -o "$BUILD_DIR/coreml-dolphin-tests"

"$BUILD_DIR/coreml-dolphin-tests"
