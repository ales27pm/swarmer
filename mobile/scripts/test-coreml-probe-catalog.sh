#!/usr/bin/env bash
set -euo pipefail
MOBILE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODULE_DIR="$MOBILE_DIR/modules/swarmer-local-inference"
TEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/swarmer-coreml-probe-catalog.XXXXXX")"
trap 'rm -rf -- "$TEST_DIR"' EXIT
for variant in debug release; do
  flags=(-swift-version 6 -strict-concurrency=complete -warnings-as-errors -parse-as-library)
  if [[ "$variant" == debug ]]; then flags+=(-D DEBUG); fi
  xcrun swiftc "${flags[@]}" \
    "$MODULE_DIR/ios/CoreMLProbeCatalog.swift" \
    "$MODULE_DIR/ios-tests/CoreMLProbeCatalogTests.swift" -o "$TEST_DIR/$variant"
  "$TEST_DIR/$variant"
done
