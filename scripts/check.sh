#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MOBILE_DIR="$ROOT/mobile"
SERVER_DIR="$ROOT/server"
SERVER_BIN="$SERVER_DIR/.venv/bin"
preflight_failed=0

fail_preflight() {
  printf 'check: %s\n' "$1" >&2
  preflight_failed=1
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    fail_preflight "required command '$1' is not available on PATH"
  fi
}

require_executable() {
  if [ ! -x "$1" ]; then
    fail_preflight "required executable is missing: ${1#"$ROOT/"}"
  fi
}

require_command node
require_command npm
require_command npx

if command -v node >/dev/null 2>&1; then
  if ! node -e '
    const [major, minor] = process.versions.node.split(".").map(Number);
    process.exit((major === 22 && minor >= 13) || major >= 24 ? 0 : 1);
  '; then
    fail_preflight "Node $(node --version) is unsupported; use Node 22.13+ (excluding 23) or 24+"
  fi
fi

if [ ! -f "$MOBILE_DIR/package-lock.json" ]; then
  fail_preflight "mobile/package-lock.json is missing; run 'npm install' in mobile"
fi

if [ ! -d "$MOBILE_DIR/node_modules" ]; then
  fail_preflight "mobile/node_modules is missing; run 'npm install' in mobile"
else
  require_executable "$MOBILE_DIR/node_modules/.bin/tsc"
  require_executable "$MOBILE_DIR/node_modules/.bin/expo"
  require_executable "$MOBILE_DIR/node_modules/.bin/eslint"
  require_executable "$MOBILE_DIR/node_modules/.bin/expo-doctor"
  require_executable "$MOBILE_DIR/node_modules/.bin/jest"
fi

require_executable "$SERVER_BIN/python"
require_executable "$SERVER_BIN/ruff"
require_executable "$SERVER_BIN/mypy"
require_executable "$SERVER_BIN/pytest"
require_executable "$SERVER_BIN/bandit"

if [ -x "$SERVER_BIN/python" ]; then
  if ! "$SERVER_BIN/python" -c 'import sys; raise SystemExit(sys.version_info < (3, 12))'; then
    fail_preflight "server virtual environment must use Python 3.12 or newer"
  fi
  if ! "$SERVER_BIN/python" -c 'import jsonschema, openapi_spec_validator'; then
    fail_preflight "server contract-validation dependencies are missing; install server[dev]"
  fi
fi

if [ "$preflight_failed" -ne 0 ]; then
  printf 'check: prerequisite validation failed; no quality gates were run\n' >&2
  exit 1
fi

printf 'check: validating installed mobile dependencies\n'
(
  cd "$MOBILE_DIR"
  npm ls --depth=0
)

printf 'check: running mobile type, lint, test, and Expo health gates\n'
(
  cd "$MOBILE_DIR"
  npm run typecheck
  npm run lint
  npm test -- --runInBand
  npx --no-install expo-doctor
)

printf 'check: running server format, lint, type, test, and security gates\n'
(
  cd "$SERVER_DIR"
  "$SERVER_BIN/ruff" format --check . "$ROOT/scripts/validate_openapi.py"
  "$SERVER_BIN/ruff" check . "$ROOT/scripts/validate_openapi.py"
  "$SERVER_BIN/mypy" app
  MYPYPATH="$SERVER_DIR" "$SERVER_BIN/mypy" --strict "$ROOT/scripts/validate_openapi.py"
  "$SERVER_BIN/pytest" -q
  "$SERVER_BIN/bandit" -r app
)

printf 'check: validating the committed OpenAPI contract against FastAPI\n'
"$SERVER_BIN/python" "$ROOT/scripts/validate_openapi.py"

printf 'check: all quality gates passed\n'
