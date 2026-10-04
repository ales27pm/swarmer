#!/usr/bin/env bash
set -euo pipefail
MOBILE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE_DIR="$MOBILE_DIR/modules/swarmer-local-inference"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/swarmer-hf-download-tests.XXXXXX")"
trap 'rm -rf -- "$BUILD_DIR"' EXIT
xcrun swiftc -swift-version 6 -strict-concurrency=complete \
  -warn-concurrency -warnings-as-errors -parse-as-library \
  "$MODULE_DIR/ios-tests/LocalModelStoreTestSupport.swift" \
  "$MODULE_DIR/ios/EmbeddingValidation.swift" \
  "$MODULE_DIR/ios/LocalModelPurpose.swift" \
  "$MODULE_DIR/ios/ANEMLLModelProfile.swift" \
  "$MODULE_DIR/ios/LocalModelStore.swift" \
  "$MODULE_DIR/ios/LocalModelDownload.swift" \
  "$MODULE_DIR/ios-tests/HuggingFaceModelDownloadTests.swift" \
  -o "$BUILD_DIR/tests"
SWIFT_DETERMINISTIC_HASHING=1 "$BUILD_DIR/tests"
