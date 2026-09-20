#!/usr/bin/env bash
# Build embedded-JS automation with optimized C/C++ kernels and DEBUG intact.
set -euo pipefail

MOBILE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
workspace="$MOBILE_DIR/ios/monGARSSwarm.xcworkspace"
scheme="monGARSSwarm"
destination="generic/platform=iOS"
forwarded=()

fail() { printf '%s\n' "$1" >&2; exit 2; }
while (( $# )); do
  case "$1" in
    -workspace|-scheme|-destination)
      (( $# >= 2 )) && [[ -n "$2" ]] || fail "Missing value for $1."
      case "$1" in
        -workspace) workspace="$2" ;;
        -scheme) scheme="$2" ;;
        -destination) destination="$2" ;;
      esac
      shift 2 ;;
    -derivedDataPath|-clonedSourcePackagesDirPath|-resultBundlePath|-resultStreamPath)
      (( $# >= 2 )) && [[ -n "$2" ]] || fail "Missing value for $1."
      forwarded+=("$1" "$2")
      shift 2 ;;
    -configuration)
      (( $# >= 2 )) && [[ "$2" == Debug ]] || fail "Automation requires -configuration Debug."
      shift 2 ;;
    CONFIGURATION=Debug|GCC_OPTIMIZATION_LEVEL=3|SWIFT_OPTIMIZATION_LEVEL=-Onone)
      shift ;;
    -configuration=*|CONFIGURATION=*|CONFIGURATION\[*|GCC_OPTIMIZATION_LEVEL=*|GCC_OPTIMIZATION_LEVEL\[*|SWIFT_OPTIMIZATION_LEVEL=*|SWIFT_OPTIMIZATION_LEVEL\[*)
      fail "Automation fixes Debug, GCC_OPTIMIZATION_LEVEL=3 and SWIFT_OPTIMIZATION_LEVEL=-Onone; remove conflicting overrides." ;;
    build)
      shift ;;
    clean|test|test-without-building|build-for-testing|analyze|archive|install|installhdrs|installsrc|docbuild|-exportArchive|-exportNotarizedApp|-create-xcframework|-runFirstLaunch|-list|-showBuildSettings|-showBuildSettingsForIndex|-showdestinations|-showsdks|-version|-checkFirstLaunchStatus|-license|-downloadPlatform|-downloadAllPlatforms|-importPlatform|-exportLocalizations|-importLocalizations)
      fail "This wrapper supports only the build action." ;;
    -xcconfig|-xcconfig=*|SWIFT_ACTIVE_COMPILATION_CONDITIONS*|GCC_PREPROCESSOR_DEFINITIONS*|OTHER_CFLAGS*|OTHER_CPLUSPLUSFLAGS*|OTHER_SWIFT_FLAGS*)
      fail "Custom xcconfig, compilation conditions and extra compiler flags are not supported by this fixed Debug build." ;;
    *) forwarded+=("$1"); shift ;;
  esac
done

# This environment variable can override command-line build settings in Xcode.
[[ -z "${XCODE_XCCONFIG_FILE:-}" ]] || fail "Unset XCODE_XCCONFIG_FILE for this fixed Debug build."

# CLI settings apply to Swift-package targets such as Cmlx as well as CocoaPods.
# C/C++ optimization preserves DEBUG; Release would omit the automation listener.
# Keep Swift unoptimized: Swift 6.2.4 crashes compiling ExpoModulesCore with -O.
exec xcodebuild \
  -workspace "$workspace" -scheme "$scheme" -destination "$destination" \
  ${forwarded[@]+"${forwarded[@]}"} \
  -configuration Debug GCC_OPTIMIZATION_LEVEL=3 SWIFT_OPTIMIZATION_LEVEL=-Onone build
